"""YouTube Music.

- Lecture et recherche : bibliothèque ytmusicapi (sans clé, sans quota).
- Création de playlist : API YouTube Data v3 officielle avec connexion Google (OAuth).
  Quota par défaut : 10 000 unités / jour, soit ~190 morceaux ajoutés par jour
  (50 unités par playlist créée + 50 par morceau ajouté).
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from ytmusicapi import YTMusic

from ..models import AuthRequired, Playlist, PlaylistRef, ProviderError, Track
from .base import Provider, Tokens, expires_at

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube"


def _artists(item: dict) -> list[str]:
    return [a["name"] for a in item.get("artists") or [] if a and a.get("name")]


def _parse_item(item: dict) -> Track | None:
    if not item or not item.get("videoId") or not item.get("title"):
        return None
    seconds = item.get("duration_seconds")
    album = item.get("album")
    return Track(
        title=item["title"],
        artists=_artists(item),
        album=album.get("name") if isinstance(album, dict) else None,
        duration_ms=int(seconds) * 1000 if seconds else None,
        platform_id=item["videoId"],
    )


class YouTubeMusicProvider(Provider):
    key = "youtube"
    name = "YouTube Music"

    def __init__(self, *args, ytmusic: YTMusic | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._yt = ytmusic
        self._yt_lock = asyncio.Lock()

    def can_write(self) -> bool:
        return bool(self.settings.google_client_id and self.settings.google_client_secret)

    def write_setup_hint(self) -> str:
        return "Renseigne GOOGLE_CLIENT_ID et GOOGLE_CLIENT_SECRET dans le fichier .env."

    async def _ytmusic(self) -> YTMusic:
        async with self._yt_lock:
            if self._yt is None:
                self._yt = await asyncio.to_thread(YTMusic)
        return self._yt

    # --------------------------------------------------------------- OAuth
    def authorize_url(self, state: str) -> str:
        if not self.can_write():
            raise ProviderError(self.write_setup_hint())
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.redirect_uri(self.key),
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH}?{urlencode(params)}"

    async def _token_request(self, data: dict) -> Tokens:
        data |= {
            "client_id": self.settings.google_client_id,
            "client_secret": self.settings.google_client_secret,
        }
        resp = await self.request("POST", GOOGLE_TOKEN, data=data)
        if resp.status_code != 200:
            raise ProviderError(f"Google a refusé la connexion : {resp.text}", 401)
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
        new["refresh_token"] = new.get("refresh_token") or token["refresh_token"]
        return new

    # ------------------------------------------------------------- lecture
    async def read_playlist(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        yt = await self._ytmusic()
        try:
            data = await asyncio.to_thread(yt.get_playlist, ref.playlist_id, None)
        except Exception as exc:  # ytmusicapi lève des exceptions variées
            token = await self.valid_token(token)
            if token:
                return await self._read_with_data_api(ref, token)
            raise ProviderError(
                "Impossible de lire cette playlist YouTube. Si elle est privée, connecte "
                f"ton compte YouTube puis réessaie. ({exc})",
                404,
            ) from exc
        tracks = [t for t in (_parse_item(i) for i in data.get("tracks") or []) if t]
        thumbs = data.get("thumbnails") or []
        author = data.get("author")
        return Playlist(
            name=data.get("title") or "Playlist YouTube",
            description=data.get("description") or None,
            owner=author.get("name") if isinstance(author, dict) else None,
            image=thumbs[-1]["url"] if thumbs else None,
            url=f"https://music.youtube.com/playlist?list={ref.playlist_id}",
            platform=self.key,
            tracks=tracks,
        )

    async def _read_with_data_api(self, ref: PlaylistRef, token: Tokens) -> Playlist:
        """Lecture via l'API officielle (utile pour les playlists privées)."""
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        meta = await self.request(
            "GET", f"{YT_API}/playlists", params={"part": "snippet", "id": ref.playlist_id}, headers=headers
        )
        items_meta = meta.json().get("items") or []
        if meta.status_code != 200 or not items_meta:
            raise ProviderError("Playlist YouTube introuvable.", 404)
        snippet = items_meta[0]["snippet"]
        tracks: list[Track] = []
        page_token = None
        while True:
            params = {"part": "snippet", "playlistId": ref.playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            resp = await self.request("GET", f"{YT_API}/playlistItems", params=params, headers=headers)
            if resp.status_code != 200:
                break
            body = resp.json()
            for item in body.get("items") or []:
                sn = item.get("snippet") or {}
                video_id = (sn.get("resourceId") or {}).get("videoId")
                channel = (sn.get("videoOwnerChannelTitle") or "").removesuffix(" - Topic")
                if video_id and sn.get("title") not in (None, "Deleted video", "Private video"):
                    tracks.append(
                        Track(title=sn["title"], artists=[channel] if channel else [], platform_id=video_id)
                    )
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return Playlist(
            name=snippet.get("title") or "Playlist YouTube",
            description=snippet.get("description") or None,
            owner=snippet.get("channelTitle"),
            url=f"https://music.youtube.com/playlist?list={ref.playlist_id}",
            platform=self.key,
            tracks=tracks,
        )

    # ----------------------------------------------------------- recherche
    async def search(self, track: Track, token: Tokens | None) -> list[Track]:
        from ..matching import build_query

        yt = await self._ytmusic()
        query = build_query(track)
        try:
            results = await asyncio.to_thread(yt.search, query, "songs", None, 10)
            tracks = [t for t in (_parse_item(r) for r in results) if t]
            if not tracks:
                results = await asyncio.to_thread(yt.search, query, "videos", None, 10)
                tracks = [t for t in (_parse_item(r) for r in results) if t]
        except Exception:
            return []
        return tracks

    # ------------------------------------------------------------ écriture
    async def read_owned(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        # Les playlists créées par l'appli sont privées : on passe par l'API officielle.
        token = await self.valid_token(token)
        if token:
            return await self._read_with_data_api(ref, token)
        return await self.read_playlist(ref, token)

    async def _user_headers(self, token: Tokens | None) -> dict:
        token = await self.valid_token(token)
        if not token:
            raise AuthRequired("YouTube Music")
        return {"Authorization": f"Bearer {token['access_token']}"}

    async def create_empty(self, name: str, description: str, token: Tokens) -> tuple[str, str]:
        headers = await self._user_headers(token)
        resp = await self.request(
            "POST",
            f"{YT_API}/playlists",
            params={"part": "snippet,status"},
            json={
                "snippet": {"title": name[:150], "description": description[:5000]},
                "status": {"privacyStatus": "private"},
            },
            headers=headers,
        )
        if resp.status_code not in (200, 201):
            raise ProviderError(_google_error("créer la playlist", resp), 502)
        playlist_id = resp.json()["id"]
        return playlist_id, f"https://music.youtube.com/playlist?list={playlist_id}"

    async def add_tracks(self, playlist_id: str, track_ids: list[str], token: Tokens) -> int:
        headers = await self._user_headers(token)
        added = 0
        for video_id in track_ids:
            add = await self.request(
                "POST",
                f"{YT_API}/playlistItems",
                params={"part": "snippet"},
                json={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
                headers=headers,
            )
            if add.status_code in (200, 201):
                added += 1
            elif "quotaExceeded" in add.text:
                raise ProviderError(
                    f"Quota YouTube du jour épuisé après {added} morceaux ajoutés. "
                    "Relance demain pour le reste.",
                    429,
                )
            # Une vidéo indisponible ne doit pas bloquer tout le transfert.
        return added

    async def remove_tracks(self, playlist_id: str, track_ids: list[str], token: Tokens) -> int:
        headers = await self._user_headers(token)
        wanted = set(track_ids)
        # YouTube supprime par identifiant d'élément de playlist, pas par vidéo.
        item_ids: list[str] = []
        page_token = None
        while True:
            params = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            resp = await self.request("GET", f"{YT_API}/playlistItems", params=params, headers=headers)
            if resp.status_code != 200:
                raise ProviderError(_google_error("lire la playlist", resp), 502)
            body = resp.json()
            for item in body.get("items") or []:
                vid = ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
                if vid in wanted:
                    item_ids.append(item["id"])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        removed = 0
        for item_id in item_ids:
            resp = await self.request("DELETE", f"{YT_API}/playlistItems", params={"id": item_id}, headers=headers)
            if resp.status_code in (200, 204):
                removed += 1
            elif "quotaExceeded" in resp.text:
                raise ProviderError("Quota YouTube du jour épuisé, la suite se fera à la prochaine vérification.", 429)
        return removed


def _google_error(action: str, resp) -> str:
    if "quotaExceeded" in resp.text:
        return "Quota YouTube du jour épuisé, réessaie demain."
    return f"YouTube n'a pas pu {action} : {resp.text}"
