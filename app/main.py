"""Serveur web : interface + API de transfert de playlists."""

from __future__ import annotations

import asyncio
import secrets
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
from .models import AuthRequired, Playlist, ProviderError
from .providers import build_providers
from .providers.apple import AppleMusicProvider
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


class TokenStore:
    """Jetons de connexion, gardés en mémoire côté serveur (jamais dans le cookie)."""

    def __init__(self):
        self._data: dict[str, dict[str, dict]] = {}

    def get(self, sid: str, platform: str) -> dict | None:
        return self._data.get(sid, {}).get(platform)

    def set(self, sid: str, platform: str, token: dict) -> None:
        self._data.setdefault(sid, {})[platform] = token

    def delete(self, sid: str, platform: str) -> None:
        self._data.get(sid, {}).pop(platform, None)


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.http.aclose()

    app = FastAPI(title="Playlist Transfer", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )
    app.state.settings = settings
    app.state.http = http or httpx.AsyncClient(timeout=30, headers={"Accept-Language": "fr-FR,fr;q=0.9"})
    app.state.providers = build_providers(settings, app.state.http)
    app.state.tokens = TokenStore()
    app.state.jobs = JobStore()
    app.state.playlists: dict[str, tuple[str, Playlist]] = {}
    app.state.tasks: set[asyncio.Task] = set()

    providers = app.state.providers
    tokens: TokenStore = app.state.tokens
    jobs: JobStore = app.state.jobs

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
        app.state.playlists[key] = (s, playlist)
        return {"key": key, "playlist": playlist.to_dict()}

    # ---------------------------------------------------------- recherche
    @app.post("/api/match")
    async def start_match(body: MatchBody, request: Request):
        s = sid(request)
        stored = app.state.playlists.get(body.playlist_key)
        if not stored or stored[0] != s:
            raise ProviderError("Playlist expirée, colle à nouveau le lien.", 404)
        playlist = stored[1]
        target = provider_or_404(body.target)
        source = provider_or_404(playlist.platform)
        if not target.can_write():
            raise ProviderError(f"{target.name} n'est pas configuré : {target.write_setup_hint()}", 503)
        target_token = tokens.get(s, target.key)
        if target_token is None:
            raise AuthRequired(target.name)
        job = jobs.create(s, target.key, playlist)
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
            url = await target.create_playlist(
                body.name.strip() or job.playlist.name, body.description, ids, token
            )
        except ProviderError as exc:
            job.status = "error"
            job.error = exc.message
            raise
        job.status = "done"
        job.playlist_url = url
        return {"url": url, "added": len(ids)}

    return app


app = create_app()
