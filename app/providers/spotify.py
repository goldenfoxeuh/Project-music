"""Spotify (API Web officielle).

Notes sur les règles de Spotify depuis février 2026 pour les applis en « Development Mode » :
- lister les morceaux d'une playlist passe par /playlists/{id}/items (l'ancien /tracks disparaît)
  et chaque entrée est rangée sous la clé « item » (anciennement « track ») ;
- ce listing n'est autorisé que pour les playlists dont l'utilisateur connecté est
  propriétaire ou collaborateur ;
- la recherche est limitée à 10 résultats ;
- la création se fait via POST /me/playlists.
Le code essaie la nouvelle forme puis retombe sur l'ancienne, pour fonctionner dans les deux cas.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import urlencode

from ..models import AuthRequired, Playlist, PlaylistRef, ProviderError, Track
from .base import Provider, Tokens, expires_at

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)


class SpotifyProvider(Provider):
    key = "spotify"
    name = "Spotify"
    supports_isrc = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._app_token: Tokens | None = None

    # ------------------------------------------------------------------ état
    def configured(self) -> bool:
        return bool(self.settings.spotify_client_id and self.settings.spotify_client_secret)

    def can_write(self) -> bool:
        return self.configured()

    def write_setup_hint(self) -> str:
        return "Renseigne SPOTIFY_CLIENT_ID et SPOTIFY_CLIENT_SECRET dans le fichier .env."

    def _basic_auth(self) -> str:
        raw = f"{self.settings.spotify_client_id}:{self.settings.spotify_client_secret}"
        return "Basic " + base64.b64encode(raw.encode()).decode()

    # --------------------------------------------------------------- OAuth
    def authorize_url(self, state: str) -> str:
        if not self.configured():
            raise ProviderError(self.write_setup_hint())
        params = {
            "client_id": self.settings.spotify_client_id,
            "response_type": "code",
            "redirect_uri": self.settings.redirect_uri(self.key),
            "scope": SCOPES,
            "state": state,
        }
        return f"{ACCOUNTS}/authorize?{urlencode(params)}"

    async def _token_request(self, data: dict) -> Tokens:
        resp = await self.request(
            "POST",
            f"{ACCOUNTS}/api/token",
            data=data,
            headers={"Authorization": self._basic_auth()},
        )
        if resp.status_code != 200:
            raise ProviderError(f"Spotify a refusé la connexion : {resp.text}", 401)
        body = resp.json()
        return {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_at": expires_at(body.get("expires_in")),
        }

    async def exchange_code(self, code: str) -> Tokens:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri(self.key),
            }
        )

    async def refresh(self, token: Tokens) -> Tokens:
        new = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
        )
        if not new.get("refresh_token"):
            new["refresh_token"] = token["refresh_token"]
        return new

    async def _app_access_token(self) -> str:
        """Jeton « application » (sans utilisateur) pour lire et chercher."""
        if not self.configured():
            raise ProviderError(
                "Spotify n'est pas configuré : " + self.write_setup_hint(), 503
            )
        if self._app_token and self._app_token["expires_at"] - 60 > time.time():
            return self._app_token["access_token"]
        self._app_token = await self._token_request({"grant_type": "client_credentials"})
        return self._app_token["access_token"]

    async def _headers(self, token: Tokens | None) -> dict:
        token = await self.valid_token(token)
        access = token["access_token"] if token else await self._app_access_token()
        return {"Authorization": f"Bearer {access}"}

    async def _get(self, url: str, token: Tokens | None, **params):
        if not url.startswith("http"):
            url = API + url
        resp = await self.request("GET", url, params=params or None, headers=await self._headers(token))
        return resp

    # ------------------------------------------------------------- lecture
    @staticmethod
    def _parse_track(obj: dict | None) -> Track | None:
        if not obj or obj.get("type", "track") != "track" or obj.get("is_local"):
            return None
        if not obj.get("name"):
            return None
        return Track(
            title=obj["name"],
            artists=[a["name"] for a in obj.get("artists", []) if a.get("name")],
            album=(obj.get("album") or {}).get("name"),
            duration_ms=obj.get("duration_ms"),
            isrc=(obj.get("external_ids") or {}).get("isrc"),
            platform_id=obj.get("uri") or (f"spotify:track:{obj['id']}" if obj.get("id") else None),
        )

    async def read_playlist(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        meta_resp = await self._get(f"/playlists/{ref.playlist_id}", token)
        if meta_resp.status_code == 404:
            raise ProviderError("Playlist Spotify introuvable (elle est peut-être privée).", 404)
        if meta_resp.status_code in (401, 403):
            if not token:
                raise AuthRequired("Spotify", "Spotify demande que tu connectes ton compte pour lire cette playlist.")
            raise ProviderError("Spotify refuse l'accès à cette playlist.", 403)
        if meta_resp.status_code != 200:
            raise ProviderError(f"Erreur Spotify ({meta_resp.status_code}) : {meta_resp.text}", 502)
        meta = meta_resp.json()

        tracks: list[Track] = []
        next_url: str | None = None
        for path in ("items", "tracks"):
            resp = await self._get(f"/playlists/{ref.playlist_id}/{path}", token, limit=50)
            if resp.status_code == 200:
                page = resp.json()
                break
            if resp.status_code in (401, 403) and path == "items":
                # Règle 2026 : seules les playlists de l'utilisateur sont lisibles.
                page = None
                if not token:
                    raise AuthRequired(
                        "Spotify",
                        "Spotify ne laisse lire que les playlists dont tu es propriétaire ou "
                        "collaborateur : connecte ton compte Spotify (ou ajoute la playlist "
                        "à ton compte en la copiant).",
                    )
                continue
            page = None
        else:
            raise ProviderError(
                "Spotify refuse de lister les morceaux de cette playlist. Depuis 2026, "
                "les applis en mode développement ne peuvent lire que les playlists "
                "dont tu es propriétaire ou collaborateur.",
                403,
            )

        while page:
            for entry in page.get("items", []):
                track = self._parse_track(entry.get("item") or entry.get("track"))
                if track:
                    tracks.append(track)
            next_url = page.get("next")
            if not next_url:
                break
            resp = await self._get(next_url, token)
            if resp.status_code != 200:
                break
            page = resp.json()

        images = meta.get("images") or []
        return Playlist(
            name=meta.get("name") or "Playlist Spotify",
            description=meta.get("description") or None,
            owner=(meta.get("owner") or {}).get("display_name"),
            image=images[0]["url"] if images else None,
            url=(meta.get("external_urls") or {}).get("spotify") or ref.url,
            platform=self.key,
            tracks=tracks,
        )

    # ----------------------------------------------------------- recherche
    async def _search(self, q: str, token: Tokens | None) -> list[Track]:
        resp = await self._get("/search", token, q=q, type="track", limit=10)
        if resp.status_code != 200:
            return []
        items = (resp.json().get("tracks") or {}).get("items") or []
        return [t for t in (self._parse_track(i) for i in items) if t]

    async def search(self, track: Track, token: Tokens | None) -> list[Track]:
        from ..matching import build_query, clean_title

        results: list[Track] = []
        if track.main_artist:
            title = clean_title(track.title) or track.title
            results = await self._search(f'track:"{title}" artist:"{track.main_artist}"', token)
        if not results:
            results = await self._search(build_query(track), token)
        return results

    async def find_by_isrc(self, isrc: str, token: Tokens | None) -> list[Track]:
        return await self._search(f"isrc:{isrc}", token)

    # ------------------------------------------------------------ écriture
    async def create_playlist(
        self, name: str, description: str, track_ids: list[str], token: Tokens
    ) -> str:
        token = await self.valid_token(token)
        if not token:
            raise AuthRequired("Spotify")
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        body = {"name": name, "description": description[:300], "public": False}

        resp = await self.request("POST", f"{API}/me/playlists", json=body, headers=headers)
        if resp.status_code in (404, 405):
            me = await self.request("GET", f"{API}/me", headers=headers)
            user_id = me.json().get("id")
            resp = await self.request(
                "POST", f"{API}/users/{user_id}/playlists", json=body, headers=headers
            )
        if resp.status_code not in (200, 201):
            raise ProviderError(f"Spotify n'a pas pu créer la playlist : {resp.text}", 502)
        playlist = resp.json()
        playlist_id = playlist["id"]

        path = "items"
        for i in range(0, len(track_ids), 100):
            chunk = track_ids[i : i + 100]
            add = await self.request(
                "POST", f"{API}/playlists/{playlist_id}/{path}", json={"uris": chunk}, headers=headers
            )
            if add.status_code in (404, 405) and path == "items":
                path = "tracks"
                add = await self.request(
                    "POST", f"{API}/playlists/{playlist_id}/{path}", json={"uris": chunk}, headers=headers
                )
            if add.status_code not in (200, 201):
                raise ProviderError(
                    f"Spotify a refusé l'ajout des morceaux : {add.text}", 502
                )
        return (playlist.get("external_urls") or {}).get(
            "spotify", f"https://open.spotify.com/playlist/{playlist_id}"
        )
