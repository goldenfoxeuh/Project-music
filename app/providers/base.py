"""Interface commune à toutes les plateformes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..config import Settings
from ..models import Playlist, PlaylistRef, ProviderError, Track

Tokens = dict[str, Any]


class Provider:
    key: str = ""
    name: str = ""
    # La plateforme sait retrouver un morceau par son code ISRC
    # (identifiant international unique d'un enregistrement).
    supports_isrc: bool = False

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    # ------------------------------------------------------------------ état
    def can_write(self) -> bool:
        """Les identifiants nécessaires pour créer des playlists sont configurés."""
        return False

    def write_setup_hint(self) -> str:
        return ""

    def uses_oauth_redirect(self) -> bool:
        return True

    # ------------------------------------------------------------- lecture
    async def read_playlist(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        raise NotImplementedError

    async def enrich(self, track: Track, token: Tokens | None) -> Track:
        """Complète un morceau (ISRC...) si la liste de la playlist ne le donne pas."""
        return track

    # ----------------------------------------------------------- recherche
    async def search(self, track: Track, token: Tokens | None) -> list[Track]:
        raise NotImplementedError

    async def find_by_isrc(self, isrc: str, token: Tokens | None) -> list[Track]:
        return []

    # ------------------------------------------------------------ écriture
    async def create_playlist(
        self, name: str, description: str, track_ids: list[str], token: Tokens
    ) -> str:
        """Crée la playlist et renvoie son URL."""
        raise NotImplementedError

    # --------------------------------------------------------------- OAuth
    def authorize_url(self, state: str) -> str:
        raise ProviderError(f"La connexion à {self.name} n'est pas disponible.")

    async def exchange_code(self, code: str) -> Tokens:
        raise NotImplementedError

    async def refresh(self, token: Tokens) -> Tokens:
        return token

    async def valid_token(self, token: Tokens | None) -> Tokens | None:
        """Renouvelle le jeton s'il est expiré (ou presque)."""
        if not token:
            return None
        expires_at = token.get("expires_at")
        if expires_at and expires_at - 60 < time.time() and token.get("refresh_token"):
            refreshed = await self.refresh(token)
            token.update(refreshed)
        return token

    # ------------------------------------------------------------- outils
    async def request(
        self, method: str, url: str, *, retries: int = 4, **kwargs: Any
    ) -> httpx.Response:
        """Requête HTTP qui patiente et réessaie en cas de limite de débit (429) ou d'erreur 5xx."""
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                resp = await self.http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise ProviderError(f"{self.name} ne répond pas : {exc}", 502) from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    return resp
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = min(float(retry_after), 30.0) if retry_after else delay
                except ValueError:
                    wait = delay
                await asyncio.sleep(wait)
                delay *= 2
                continue
            return resp
        return resp  # pragma: no cover


def expires_at(expires_in: Any) -> float | None:
    try:
        return time.time() + float(expires_in)
    except (TypeError, ValueError):
        return None
