"""Deezer (API publique + OAuth pour créer des playlists).

La lecture des playlists publiques et la recherche ne demandent aucune clé.
Pour créer une playlist il faut une application Deezer (DEEZER_APP_ID / DEEZER_APP_SECRET).
Attention : Deezer n'accepte plus toujours la création de nouvelles applications
sur developers.deezer.com ; si c'est ton cas, Deezer reste utilisable comme source.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from ..models import AuthRequired, Playlist, PlaylistRef, ProviderError, Track
from .base import Provider, Tokens, expires_at

API = "https://api.deezer.com"
CONNECT = "https://connect.deezer.com/oauth"
PERMS = "basic_access,manage_library,offline_access"


class DeezerProvider(Provider):
    key = "deezer"
    name = "Deezer"
    supports_isrc = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # L'API Deezer accepte ~50 requêtes / 5 secondes.
        self._limiter = asyncio.Semaphore(8)

    def can_write(self) -> bool:
        return bool(self.settings.deezer_app_id and self.settings.deezer_app_secret)

    def write_setup_hint(self) -> str:
        return "Renseigne DEEZER_APP_ID et DEEZER_APP_SECRET dans le fichier .env."

    async def _api(
        self, method: str, path: str, token: Tokens | None = None, _attempt: int = 0, **params
    ):
        if token:
            params["access_token"] = token["access_token"]
        url = path if path.startswith("http") else API + path
        async with self._limiter:
            resp = await self.request(method, url, params=params)
        if resp.status_code != 200:
            raise ProviderError(f"Erreur Deezer ({resp.status_code}).", 502)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError("Réponse Deezer illisible.", 502) from exc
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            code = err.get("code")
            if code == 4 and _attempt < 3:  # Quota dépassé : on patiente puis on réessaie.
                await asyncio.sleep(5)
                return await self._api(method, path, None, _attempt + 1, **params)
            if code in (200, 300):
                raise AuthRequired("Deezer", "Ta connexion Deezer a expiré, reconnecte-toi.")
            if code == 800:
                raise ProviderError("Introuvable sur Deezer.", 404)
            raise ProviderError(f"Deezer : {err.get('message', err)}", 502)
        return body

    # --------------------------------------------------------------- OAuth
    def authorize_url(self, state: str) -> str:
        if not self.can_write():
            raise ProviderError(self.write_setup_hint())
        params = {
            "app_id": self.settings.deezer_app_id,
            "redirect_uri": self.settings.redirect_uri(self.key),
            "perms": PERMS,
            "state": state,
        }
        return f"{CONNECT}/auth.php?{urlencode(params)}"

    async def exchange_code(self, code: str) -> Tokens:
        resp = await self.request(
            "GET",
            f"{CONNECT}/access_token.php",
            params={
                "app_id": self.settings.deezer_app_id,
                "secret": self.settings.deezer_app_secret,
                "code": code,
                "output": "json",
            },
        )
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(f"Deezer a refusé la connexion : {resp.text}", 401)
        if "access_token" not in body:
            raise ProviderError(f"Deezer a refusé la connexion : {body}", 401)
        expires = body.get("expires") or body.get("expires_in")
        return {
            "access_token": body["access_token"],
            # 0 = jeton sans expiration (permission offline_access).
            "expires_at": expires_at(expires) if expires and int(expires) > 0 else None,
        }

    # ------------------------------------------------------------- lecture
    @staticmethod
    def _parse_track(obj: dict) -> Track | None:
        if not obj or obj.get("type", "track") != "track" or not obj.get("title"):
            return None
        artists = []
        if obj.get("contributors"):
            artists = [c["name"] for c in obj["contributors"] if c.get("name")]
        elif (obj.get("artist") or {}).get("name"):
            artists = [obj["artist"]["name"]]
        duration = obj.get("duration")
        return Track(
            title=obj["title"],
            artists=artists,
            album=(obj.get("album") or {}).get("title"),
            duration_ms=int(duration) * 1000 if duration else None,
            isrc=obj.get("isrc"),
            platform_id=str(obj["id"]) if obj.get("id") else None,
        )

    async def read_playlist(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        token = await self.valid_token(token)
        meta = await self._api("GET", f"/playlist/{ref.playlist_id}", token)
        tracks: list[Track] = []
        index = 0
        while True:
            page = await self._api(
                "GET", f"/playlist/{ref.playlist_id}/tracks", token, index=index, limit=100
            )
            data = page.get("data") or []
            for item in data:
                t = self._parse_track(item)
                if t:
                    tracks.append(t)
            if not page.get("next") or not data:
                break
            index += len(data)
        return Playlist(
            name=meta.get("title") or "Playlist Deezer",
            description=meta.get("description") or None,
            owner=(meta.get("creator") or {}).get("name"),
            image=meta.get("picture_medium"),
            url=meta.get("link") or ref.url,
            platform=self.key,
            tracks=tracks,
        )

    async def enrich(self, track: Track, token: Tokens | None) -> Track:
        """La liste d'une playlist Deezer ne contient pas l'ISRC : on le récupère morceau par morceau."""
        if track.isrc or not track.platform_id:
            return track
        try:
            full = await self._api("GET", f"/track/{track.platform_id}")
            track.isrc = full.get("isrc") or None
        except ProviderError:
            pass
        return track

    # ----------------------------------------------------------- recherche
    async def search(self, track: Track, token: Tokens | None) -> list[Track]:
        from ..matching import build_query, clean_title

        results: list[Track] = []
        if track.main_artist:
            title = (clean_title(track.title) or track.title).replace('"', "")
            artist = track.main_artist.replace('"', "")
            body = await self._api("GET", "/search/track", q=f'artist:"{artist}" track:"{title}"', limit=10)
            results = [t for t in (self._parse_track(i) for i in body.get("data") or []) if t]
        if not results:
            body = await self._api("GET", "/search/track", q=build_query(track), limit=10)
            results = [t for t in (self._parse_track(i) for i in body.get("data") or []) if t]
        return results

    async def find_by_isrc(self, isrc: str, token: Tokens | None) -> list[Track]:
        try:
            body = await self._api("GET", f"/track/isrc:{isrc}")
        except ProviderError:
            return []
        t = self._parse_track(body)
        return [t] if t else []

    # ------------------------------------------------------------ écriture
    async def create_playlist(
        self, name: str, description: str, track_ids: list[str], token: Tokens
    ) -> str:
        token = await self.valid_token(token)
        if not token:
            raise AuthRequired("Deezer")
        created = await self._api("POST", "/user/me/playlists", token, title=name)
        playlist_id = created.get("id")
        if not playlist_id:
            raise ProviderError(f"Deezer n'a pas pu créer la playlist : {created}", 502)
        if description:
            try:
                await self._api("POST", f"/playlist/{playlist_id}", token, description=description[:500])
            except ProviderError:
                pass
        for i in range(0, len(track_ids), 100):
            chunk = track_ids[i : i + 100]
            await self._api("POST", f"/playlist/{playlist_id}/tracks", token, songs=",".join(chunk))
        return f"https://www.deezer.com/playlist/{playlist_id}"
