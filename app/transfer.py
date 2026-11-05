"""Recherche de chaque morceau d'une playlist sur la plateforme cible."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .matching import best_match
from .models import Playlist, PlaylistRef, ProviderError, Track
from .providers.base import Provider, Tokens


@dataclass
class MatchResult:
    source: Track
    match: Track | None = None
    score: float = 0.0
    method: str | None = None  # "isrc" ou "search"

    def to_dict(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "match": self.match.to_dict() if self.match else None,
            "score": round(self.score, 1),
            "method": self.method,
        }


@dataclass
class Job:
    id: str
    session_id: str
    target: str
    playlist: Playlist
    status: str = "running"  # running | matched | creating | done | error
    total: int = 0
    done: int = 0
    results: list[MatchResult | None] = field(default_factory=list)
    error: str | None = None
    playlist_url: str | None = None
    created_at: float = field(default_factory=time.time)
    # Pour pouvoir ensuite synchroniser l'original et la copie.
    source_ref: PlaylistRef | None = None
    created_id: str | None = None
    created_name: str | None = None
    added_ids: list[str] = field(default_factory=list)

    def to_dict(self, include_results: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "target": self.target,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "playlist_url": self.playlist_url,
            "playlist_name": self.playlist.name,
            "can_sync": bool(self.created_id and self.source_ref),
        }
        finished = [r for r in self.results if r]
        data["found"] = sum(1 for r in finished if r.match)
        data["missing"] = sum(1 for r in finished if not r.match)
        if include_results:
            data["results"] = [r.to_dict() for r in finished]
        return data


async def match_track(
    track: Track,
    source: Provider,
    target: Provider,
    source_token: Tokens | None,
    target_token: Tokens | None,
    threshold: float,
) -> MatchResult:
    # Même plateforme : rien à chercher.
    if source.key == target.key and track.platform_id:
        return MatchResult(track, track, 100.0, "same")

    best: Track | None = None
    best_score = 0.0

    # 1) Par ISRC : identifiant unique de l'enregistrement, le plus fiable.
    if target.supports_isrc:
        if not track.isrc:
            track = await source.enrich(track, source_token)
        if track.isrc:
            try:
                candidates = await target.find_by_isrc(track.isrc, target_token)
            except ProviderError:
                candidates = []
            if candidates:
                # Plusieurs éditions peuvent partager l'ISRC : on garde la plus ressemblante.
                best, best_score = best_match(track, candidates, threshold=0)
                return MatchResult(track, best, 100.0, "isrc")

    # 2) Recherche texte + score de ressemblance.
    try:
        candidates = await target.search(track, target_token)
    except ProviderError:
        candidates = []
    best, best_score = best_match(track, candidates, threshold)
    return MatchResult(track, best, best_score, "search" if best else None)


async def run_matching(
    job: Job,
    source: Provider,
    target: Provider,
    source_token: Tokens | None,
    target_token: Tokens | None,
    threshold: float,
    concurrency: int = 4,
) -> None:
    tracks = job.playlist.tracks
    job.total = len(tracks)
    job.results = [None] * len(tracks)
    sem = asyncio.Semaphore(concurrency)

    async def worker(i: int, track: Track):
        async with sem:
            try:
                res = await match_track(track, source, target, source_token, target_token, threshold)
            except Exception:  # un morceau en erreur ne doit pas arrêter le reste
                res = MatchResult(track)
            job.results[i] = res
            job.done += 1

    try:
        await asyncio.gather(*(worker(i, t) for i, t in enumerate(tracks)))
        job.status = "matched"
    except Exception as exc:  # pragma: no cover - sécurité
        job.status = "error"
        job.error = str(exc)


def unique_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


class JobStore:
    def __init__(self, ttl: float = 6 * 3600):
        self.jobs: dict[str, Job] = {}
        self.ttl = ttl

    def create(
        self, session_id: str, target: str, playlist: Playlist, source_ref: PlaylistRef | None = None
    ) -> Job:
        self._cleanup()
        job = Job(
            id=uuid.uuid4().hex, session_id=session_id, target=target, playlist=playlist,
            source_ref=source_ref,
        )
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str, session_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.session_id == session_id:
            return job
        return None

    def _cleanup(self):
        limit = time.time() - self.ttl
        for k in [k for k, j in self.jobs.items() if j.created_at < limit]:
            del self.jobs[k]
