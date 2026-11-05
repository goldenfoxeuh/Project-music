"""Serveur web : interface + API de transfert de playlists."""

from __future__ import annotations

import asyncio
import secrets
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .links import resolve_link
from .models import AuthRequired, Playlist, PlaylistRef, ProviderError
from .providers import build_providers
from .providers.apple import AppleMusicProvider
from .storage import Database, SyncStore, TokenStore
from .sync import MODES, Side, Sync, SyncRunner, track_key
from .transfer import JobStore, run_matching, unique_ids

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class AppleToken(BaseModel):
    music_user_token: str

class ReadBody(BaseModel):
    url: str

class MatchBody(BaseModel):
    playlist_key: str
    target: str

class CreateBody(BaseModel):
    job_id: str
    name: str
    description: str = ""
    track_ids: list[str]


class SyncOptions(BaseModel):
    mode: str = "one_way"
    interval_hours: float = 6
    propagate_deletions: bool = True


class SyncFromJob(SyncOptions):
    job_id: str


class SyncFromLinks(SyncOptions):
    url_a: str
    url_b: str


class SyncUpdate(BaseModel):
    mode: str | None = None
    interval_hours: float | None = None
    propagate_deletions: bool | None = None
    enabled: bool | None = None


def _check_options(mode: str | None, interval: float | None) -> None:
    if mode is not None and mode not in MODES:
        raise ProviderError("Mode de synchronisation inconnu.")
    if interval is not None and not (0.25 <= interval <= 24 * 30):
        raise ProviderError("L'intervalle doit être compris entre 15 minutes et 30 jours.")


def create_app(
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
    db: Database | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.sync_enabled:
            app.state.sync_runner.start()
        yield
        await app.state.sync_runner.stop()
        await app.state.http.aclose()

    app = FastAPI(title="Playlist Transfer", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        max_age=365 * 24 * 3600,  # les synchronisations sont liées à cette session
        https_only=settings.base_url.startswith("https://"),
    )
    app.state.settings = settings
    app.state.http = http or httpx.AsyncClient(timeout=30, headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    app.state.providers = build_providers(settings, app.state.http)
    app.state.db = db or Database(Path(settings.data_dir) / "app.db")
    app.state.tokens = TokenStore(app.state.db)
    app.state.syncs = SyncStore(app.state.db)
    app.state.jobs = JobStore()
    app.state.playlists: dict[str, tuple[str, Playlist, PlaylistRef]] = {}
    app.state.tasks: set[asyncio.Task] = set()

    providers = app.state.providers
    tokens: TokenStore = app.state.tokens
    syncs: SyncStore = app.state.syncs
    jobs: JobStore = app.state.jobs
    runner = SyncRunner(
        syncs, providers, tokens, settings.match_threshold, check_every=settings.sync_check_seconds
    )
    app.state.sync_runner = runner

    def sid(request: Request) -> str:
        if "sid" not in request.session:
            request.session["sid"] = secrets.token_urlsafe(24)
        return request.session["sid"]

    def provider_or_404(platform: str):
        p = providers.get(platform)
        if not p:
            raise ProviderError("Plateforme inconnue.", 404)
        return p

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError):
        body = {"error": exc.message}
        if isinstance(exc, AuthRequired):
            body["auth_required"] = next(
                (k for k, p in providers.items() if p.name == exc.platform), exc.platform
            )
        return JSONResponse(body, status_code=exc.status_code)

    # ----------------------------------------------------------- interface
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # Servi à la racine pour pouvoir gérer toute l'application.
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ----------------------------------------------------------- plateformes
    @app.get("/api/platforms")
    async def platforms(request: Request):
        s = sid(request)
        return [
            {
                "key": key,
                "name": p.name,
                "can_write": p.can_write(),
                "setup_hint": "" if p.can_write() else p.write_setup_hint(),
                "connected": tokens.get(s, key) is not None,
                "oauth": p.uses_oauth_redirect(),
            }
            for key, p in providers.items()
        ]

    # ----------------------------------------------------------- connexion
    @app.get("/auth/{platform}/login")
    async def login(platform: str, request: Request):
        p = provider_or_404(platform)
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        sid(request)
        try:
            return RedirectResponse(p.authorize_url(state))
        except ProviderError as exc:
            return RedirectResponse(f"/?error={quote(exc.message)}")

    @app.get("/auth/{platform}/callback")
    async def callback(platform: str, request: Request, code: str = "", state: str = "",
                       error: str = "", error_reason: str = ""):
        p = provider_or_404(platform)
        if error or error_reason or not code:
            return RedirectResponse(f"/?error={quote(f'Connexion {p.name} annulée')}")
        if not state or state != request.session.pop("oauth_state", None):
            return RedirectResponse(f"/?error={quote('Session expirée, réessaie la connexion')}")
        try:
            token = await p.exchange_code(code)
        except ProviderError as exc:
            return RedirectResponse(f"/?error={quote(exc.message[:200])}")
        tokens.set(sid(request), platform, token)
        return RedirectResponse(f"/?connected={platform}")

    @app.post("/auth/{platform}/logout")
    async def logout(platform: str, request: Request):
        tokens.delete(sid(request), platform)
        return {"ok": True}

    @app.get("/api/apple/developer-token")
    async def apple_developer_token():
        apple: AppleMusicProvider = providers["apple"]
        token = apple.developer_token()
        if not token:
            raise ProviderError("Apple Music n'est pas configuré : " + apple.write_setup_hint(), 503)
        return {"token": token}

    @app.post("/api/auth/apple")
    async def apple_login(body: AppleToken, request: Request):
        apple: AppleMusicProvider = providers["apple"]
        storefront = await apple.user_storefront(body.music_user_token)
        tokens.set(sid(request), "apple", {"music_user_token": body.music_user_token, "storefront": storefront})
        return {"ok": True, "storefront": storefront}

    # ------------------------------------------------------------- lecture
    @app.post("/api/playlist")
    async def read_playlist(body: ReadBody, request: Request):
        s = sid(request)
        ref = await resolve_link(body.url, app.state.http)
        p = provider_or_404(ref.platform)
        playlist = await p.read_playlist(ref, tokens.get(s, ref.platform))
        key = secrets.token_urlsafe(12)
        # On garde la playlist lue pour l'étape suivante (bornée pour la mémoire).
        if len(app.state.playlists) > 500:
            app.state.playlists.pop(next(iter(app.state.playlists)))
        app.state.playlists[key] = (s, playlist, ref)
        return {"key": key, "playlist": playlist.to_dict()}

    # ---------------------------------------------------------- recherche
    @app.post("/api/match")
    async def start_match(body: MatchBody, request: Request):
        s = sid(request)
        stored = app.state.playlists.get(body.playlist_key)
        if not stored or stored[0] != s:
            raise ProviderError("Playlist expirée, colle à nouveau le lien.", 404)
        playlist, ref = stored[1], stored[2]
        target = provider_or_404(body.target)
        source = provider_or_404(playlist.platform)
        if not target.can_write():
            raise ProviderError(f"{target.name} n'est pas configuré : {target.write_setup_hint()}", 503)
        target_token = tokens.get(s, target.key)
        if target_token is None:
            raise AuthRequired(target.name)
        job = jobs.create(s, target.key, playlist, source_ref=ref)
        task = asyncio.create_task(
            run_matching(
                job, source, target, tokens.get(s, source.key), target_token,
                settings.match_threshold,
            )
        )
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return job.to_dict(include_results=False)

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str, request: Request, results: bool = True):
        job = jobs.get(job_id, sid(request))
        if not job:
            raise ProviderError("Transfert introuvable.", 404)
        return job.to_dict(include_results=results)

    # ------------------------------------------------------------ création
    @app.post("/api/create")
    async def create(body: CreateBody, request: Request):
        s = sid(request)
        job = jobs.get(body.job_id, s)
        if not job:
            raise ProviderError("Transfert introuvable.", 404)
        if job.status not in ("matched", "error", "done"):
            raise ProviderError("La recherche des morceaux n'est pas terminée.", 409)
        ids = unique_ids(body.track_ids)
        if not ids:
            raise ProviderError("Aucun morceau à ajouter.")
        allowed = {r.match.platform_id for r in job.results if r and r.match}
        ids = [i for i in ids if i in allowed]
        target = provider_or_404(job.target)
        token = tokens.get(s, target.key)
        if token is None:
            raise AuthRequired(target.name)
        job.status = "creating"
        try:
            playlist_id, url = await target.create_empty(
                body.name.strip() or job.playlist.name, body.description, token
            )
            job.created_id, job.playlist_url = playlist_id, url
            await target.add_tracks(playlist_id, ids, token)
        except ProviderError as exc:
            job.status = "error"
            job.error = exc.message
            raise
        job.status = "done"
        job.added_ids = ids
        job.created_name = body.name.strip() or job.playlist.name
        return {"url": url, "added": len(ids), "can_sync": job.source_ref is not None}

    # ------------------------------------------------------ synchronisation
    def require_ready(s: str, platform: str, role: str) -> None:
        p = provider_or_404(platform)
        if not p.can_write():
            raise ProviderError(f"{p.name} n'est pas configuré : {p.write_setup_hint()}", 503)
        if tokens.get(s, platform) is None:
            raise AuthRequired(p.name, f"Connecte ton compte {p.name} ({role}) pour synchroniser.")

    def own_sync(sync_id: str, s: str) -> Sync:
        sync = syncs.get(sync_id)
        if not sync or sync.sid != s:
            raise ProviderError("Synchronisation introuvable.", 404)
        return sync

    @app.get("/api/syncs")
    async def list_syncs(request: Request):
        return [x.public() for x in syncs.for_session(sid(request))]

    @app.post("/api/syncs")
    async def create_sync(body: SyncFromJob, request: Request):
        """Synchronise l'original et la copie qui vient d'être créée."""
        s = sid(request)
        _check_options(body.mode, body.interval_hours)
        job = jobs.get(body.job_id, s)
        if not job or not job.created_id or not job.source_ref:
            raise ProviderError("Crée d'abord la copie de la playlist.", 409)
        require_ready(s, job.target, "copie")
        if body.mode == "two_way":
            require_ready(s, job.source_ref.platform, "original")
        added = set(job.added_ids)
        keys_a = list(dict.fromkeys(track_key(t) for t in job.playlist.tracks))
        pairs = [
            [track_key(r.source), r.match.platform_id]
            for r in job.results
            if r and r.match and r.match.platform_id in added
        ]
        sync = Sync.new(
            s,
            Side(job.source_ref.platform, job.source_ref.playlist_id, job.playlist.url or job.source_ref.url,
                 job.playlist.name, job.source_ref.storefront),
            Side(job.target, job.created_id, job.playlist_url, job.created_name or job.playlist.name),
            mode=body.mode,
            interval_hours=body.interval_hours,
            propagate_deletions=body.propagate_deletions,
            snapshot_a=keys_a,
            snapshot_b=list(job.added_ids),
            pairs=pairs,
        )
        syncs.save(sync)
        return sync.public()

    @app.post("/api/syncs/link")
    async def link_syncs(body: SyncFromLinks, request: Request):
        """Relie deux playlists qui existent déjà. La 1re vérification les fusionne."""
        s = sid(request)
        _check_options(body.mode, body.interval_hours)
        ref_a = await resolve_link(body.url_a, app.state.http)
        ref_b = await resolve_link(body.url_b, app.state.http)
        if (ref_a.platform, ref_a.playlist_id) == (ref_b.platform, ref_b.playlist_id):
            raise ProviderError("Les deux liens pointent vers la même playlist.")
        require_ready(s, ref_b.platform, "copie")
        if body.mode == "two_way":
            require_ready(s, ref_a.platform, "original")
        pa, pb = provider_or_404(ref_a.platform), provider_or_404(ref_b.platform)
        # Vérifie tout de suite que les deux playlists sont lisibles.
        a = await pa.read_owned(ref_a, await pa.valid_token(tokens.get(s, ref_a.platform)))
        b = await pb.read_owned(ref_b, await pb.valid_token(tokens.get(s, ref_b.platform)))
        sync = Sync.new(
            s,
            Side(ref_a.platform, ref_a.playlist_id, a.url or ref_a.url, a.name, ref_a.storefront),
            Side(ref_b.platform, ref_b.playlist_id, b.url or ref_b.url, b.name, ref_b.storefront),
            mode=body.mode,
            interval_hours=body.interval_hours,
            propagate_deletions=body.propagate_deletions,
        )
        sync.next_run = 0  # première vérification dès que possible
        syncs.save(sync)
        return sync.public()

    @app.patch("/api/syncs/{sync_id}")
    async def update_sync(sync_id: str, body: SyncUpdate, request: Request):
        s = sid(request)
        sync = own_sync(sync_id, s)
        _check_options(body.mode, body.interval_hours)
        if body.mode == "two_way":
            require_ready(s, sync.a.platform, "original")
        for f in ("mode", "interval_hours", "propagate_deletions", "enabled"):
            v = getattr(body, f)
            if v is not None:
                setattr(sync, f, v)
        if body.interval_hours is not None or body.enabled:
            base = sync.last_run or time.time()
            sync.next_run = base + sync.interval_hours * 3600
        syncs.save(sync)
        return sync.public()

    @app.post("/api/syncs/{sync_id}/run")
    async def run_sync_now(sync_id: str, request: Request):
        own_sync(sync_id, sid(request))
        sync = await runner.run_one(sync_id)
        return sync.public()

    @app.delete("/api/syncs/{sync_id}")
    async def delete_sync(sync_id: str, request: Request):
        own_sync(sync_id, sid(request))
        syncs.delete(sync_id)
        return {"ok": True}

    return app


app = create_app()
