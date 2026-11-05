"""Apple Music.

- Avec un jeton développeur Apple (compte Apple Developer requis) : lecture complète
  via l'API officielle, recherche, et création de playlists dans la bibliothèque
  de l'utilisateur (connexion via MusicKit JS dans le navigateur).
- Sans jeton : lecture « best effort » de la page publique music.apple.com
  (utile pour copier une playlist Apple Music vers une autre plateforme).
"""

from __future__ import annotations

import html
import json
import re
import time

import jwt

from ..models import AuthRequired, Playlist, PlaylistRef, ProviderError, Track
from .base import Provider, Tokens

API = "https://api.music.apple.com"
TOKEN_LIFETIME = 60 * 60 * 24 * 150  # 150 jours (max autorisé : ~6 mois)


def parse_iso_duration(value: str | None) -> int | None:
    """« PT3M25S » -> 205000 ms."""
    if not value:
        return None
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not m:
        return None
    h, mi, s = (float(x) if x else 0 for x in m.groups())
    return int((h * 3600 + mi * 60 + s) * 1000)


def split_artists(artist_name: str | None) -> list[str]:
    if not artist_name:
        return []
    parts = re.split(r"\s*(?:,|&| et | and | x | feat\. | ft\. )\s*", artist_name, flags=re.I)
    return [p for p in (x.strip() for x in parts) if p] or [artist_name]


class AppleMusicProvider(Provider):
    key = "apple"
    name = "Apple Music"
    supports_isrc = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dev_token: str | None = None
        self._dev_token_exp = 0.0

    # ------------------------------------------------------------------ état
    def can_write(self) -> bool:
        return self.developer_token() is not None

    def write_setup_hint(self) -> str:
        return (
            "Renseigne APPLE_TEAM_ID, APPLE_KEY_ID et APPLE_PRIVATE_KEY_PATH "
            "(clé MusicKit .p8) — ou APPLE_DEVELOPER_TOKEN — dans le fichier .env."
        )

    def uses_oauth_redirect(self) -> bool:
        # Apple utilise MusicKit JS dans le navigateur, pas une redirection OAuth.
        return False

    def developer_token(self) -> str | None:
        s = self.settings
        if s.apple_developer_token:
            return s.apple_developer_token
        if self._dev_token and self._dev_token_exp - 3600 > time.time():
            return self._dev_token
        key = s.apple_private_key()
        if not (s.apple_team_id and s.apple_key_id and key):
            return None
        now = int(time.time())
        self._dev_token_exp = now + TOKEN_LIFETIME
        self._dev_token = jwt.encode(
            {"iss": s.apple_team_id, "iat": now, "exp": self._dev_token_exp},
            key,
            algorithm="ES256",
            headers={"kid": s.apple_key_id},
        )
        return self._dev_token

    def _headers(self, token: Tokens | None = None) -> dict:
        dev = self.developer_token()
        if not dev:
            raise ProviderError("Apple Music n'est pas configuré : " + self.write_setup_hint(), 503)
        headers = {"Authorization": f"Bearer {dev}"}
        if token and token.get("music_user_token"):
            headers["Music-User-Token"] = token["music_user_token"]
        return headers

    def _storefront(self, token: Tokens | None) -> str:
        return (token or {}).get("storefront") or self.settings.apple_storefront

    async def user_storefront(self, music_user_token: str) -> str:
        resp = await self.request(
            "GET", f"{API}/v1/me/storefront", headers=self._headers({"music_user_token": music_user_token})
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            if data:
                return data[0]["id"]
        return self.settings.apple_storefront

    # ------------------------------------------------------------- lecture
    @staticmethod
    def _parse_song(obj: dict) -> Track | None:
        attrs = obj.get("attributes") or {}
        if not attrs.get("name"):
            return None
        return Track(
            title=attrs["name"],
            artists=split_artists(attrs.get("artistName")),
            album=attrs.get("albumName"),
            duration_ms=attrs.get("durationInMillis"),
            isrc=attrs.get("isrc"),
            platform_id=obj.get("id"),
        )

    async def read_playlist(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        if ref.playlist_id.startswith("p."):
            return await self._read_library(ref, token)
        if self.developer_token():
            return await self._read_api(ref, token)
        return await self._read_public_page(ref)

    async def _read_library(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        """Playlist de la bibliothèque de l'utilisateur (ex : une copie créée par l'appli)."""
        if not token or not token.get("music_user_token"):
            raise AuthRequired("Apple Music")
        headers = self._headers(token)
        meta = await self.request("GET", f"{API}/v1/me/library/playlists/{ref.playlist_id}", headers=headers)
        if meta.status_code in (401, 403):
            raise AuthRequired("Apple Music", "Ta connexion Apple Music a expiré, reconnecte-toi.")
        if meta.status_code != 200:
            raise ProviderError("Playlist introuvable dans ta bibliothèque Apple Music.", 404)
        attrs = (meta.json().get("data") or [{}])[0].get("attributes") or {}
        tracks: list[Track] = []
        url = f"{API}/v1/me/library/playlists/{ref.playlist_id}/tracks?limit=100"
        while url:
            resp = await self.request("GET", url, headers=headers)
            if resp.status_code == 404:  # playlist vide
                break
            if resp.status_code != 200:
                raise ProviderError(f"Erreur Apple Music ({resp.status_code}) : {resp.text}", 502)
            body = resp.json()
            for item in body.get("data") or []:
                t = self._parse_song(item)
                if t:
                    # On compare les morceaux par leur identifiant du catalogue.
                    catalog_id = ((item.get("attributes") or {}).get("playParams") or {}).get("catalogId")
                    t.platform_id = catalog_id or t.platform_id
                    tracks.append(t)
            nxt = body.get("next")
            url = f"{API}{nxt}" if nxt else None
        return Playlist(
            name=attrs.get("name") or "Playlist Apple Music",
            description=(attrs.get("description") or {}).get("standard"),
            url=ref.url,
            platform=self.key,
            tracks=tracks,
        )

    async def _read_api(self, ref: PlaylistRef, token: Tokens | None) -> Playlist:
        storefront = ref.storefront or self._storefront(token)
        headers = self._headers(token)
        resp = await self.request(
            "GET", f"{API}/v1/catalog/{storefront}/playlists/{ref.playlist_id}", headers=headers
        )
        if resp.status_code == 404:
            raise ProviderError("Playlist Apple Music introuvable (est-elle partagée publiquement ?).", 404)
        if resp.status_code != 200:
            raise ProviderError(f"Erreur Apple Music ({resp.status_code}) : {resp.text}", 502)
        data = resp.json()["data"][0]
        attrs = data.get("attributes") or {}
        tracks: list[Track] = []
        rel = (data.get("relationships") or {}).get("tracks") or {}
        page = rel
        while True:
            for item in page.get("data") or []:
                if item.get("type") in ("songs", "library-songs"):
                    t = self._parse_song(item)
                    if t:
                        tracks.append(t)
            next_path = page.get("next")
            if not next_path:
                break
            nxt = await self.request("GET", f"{API}{next_path}", headers=headers)
            if nxt.status_code != 200:
                break
            page = nxt.json()
        artwork = attrs.get("artwork") or {}
        image = artwork.get("url", "").replace("{w}", "300").replace("{h}", "300") or None
        return Playlist(
            name=attrs.get("name") or "Playlist Apple Music",
            description=(attrs.get("description") or {}).get("standard"),
            owner=attrs.get("curatorName"),
            image=image,
            url=attrs.get("url") or ref.url,
            platform=self.key,
            tracks=tracks,
        )

    async def _read_public_page(self, ref: PlaylistRef) -> Playlist:
        resp = await self.request(
            "GET",
            ref.url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PlaylistTransfer/1.0)"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            raise ProviderError(
                "Impossible de lire la page Apple Music. Configure un jeton développeur "
                "Apple pour une lecture fiable.",
                502,
            )
        playlist = parse_apple_page(resp.text, ref)
        if not playlist.tracks:
            raise ProviderError(
                "Aucun morceau trouvé sur la page Apple Music (la page a peut-être changé). "
                "Configure un jeton développeur Apple pour une lecture fiable.",
                502,
            )
        return playlist

    # ----------------------------------------------------------- recherche
    async def search(self, track: Track, token: Tokens | None) -> list[Track]:
        from ..matching import build_query

        resp = await self.request(
            "GET",
            f"{API}/v1/catalog/{self._storefront(token)}/search",
            params={"term": build_query(track), "types": "songs", "limit": 10},
            headers=self._headers(token),
        )
        if resp.status_code != 200:
            return []
        songs = ((resp.json().get("results") or {}).get("songs") or {}).get("data") or []
        return [t for t in (self._parse_song(s) for s in songs) if t]

    async def find_by_isrc(self, isrc: str, token: Tokens | None) -> list[Track]:
        resp = await self.request(
            "GET",
            f"{API}/v1/catalog/{self._storefront(token)}/songs",
            params={"filter[isrc]": isrc},
            headers=self._headers(token),
        )
        if resp.status_code != 200:
            return []
        return [t for t in (self._parse_song(s) for s in resp.json().get("data") or []) if t]

    # ------------------------------------------------------------ écriture
    # Apple Music ne permet pas de retirer un morceau d'une playlist via son API.
    supports_remove = False

    async def create_empty(self, name: str, description: str, token: Tokens) -> tuple[str, str]:
        if not token or not token.get("music_user_token"):
            raise AuthRequired("Apple Music")
        resp = await self.request(
            "POST",
            f"{API}/v1/me/library/playlists",
            json={"attributes": {"name": name, "description": description[:500]}},
            headers=self._headers(token),
        )
        if resp.status_code in (401, 403):
            raise AuthRequired("Apple Music", "Ta connexion Apple Music a expiré, reconnecte-toi.")
        if resp.status_code not in (200, 201):
            raise ProviderError(f"Apple Music n'a pas pu créer la playlist : {resp.text}", 502)
        playlist_id = resp.json()["data"][0]["id"]
        return playlist_id, f"https://music.apple.com/library/playlist/{playlist_id}"

    async def add_tracks(self, playlist_id: str, track_ids: list[str], token: Tokens) -> int:
        if not token or not token.get("music_user_token"):
            raise AuthRequired("Apple Music")
        headers = self._headers(token)
        for i in range(0, len(track_ids), 100):
            chunk = track_ids[i : i + 100]
            add = await self.request(
                "POST",
                f"{API}/v1/me/library/playlists/{playlist_id}/tracks",
                json={"data": [{"id": x, "type": "songs"} for x in chunk]},
                headers=headers,
            )
            if add.status_code in (401, 403):
                raise AuthRequired("Apple Music", "Ta connexion Apple Music a expiré, reconnecte-toi.")
            if add.status_code not in (200, 201, 204):
                raise ProviderError(f"Apple Music a refusé l'ajout des morceaux : {add.text}", 502)
        return len(track_ids)


# ---------------------------------------------------------------------------
# Lecture de la page publique (sans jeton développeur)
# ---------------------------------------------------------------------------

_SCRIPT = re.compile(r"<script([^>]*)>(.*?)</script>", re.S | re.I)


def _json_scripts(page: str) -> list[object]:
    """Contenu des balises <script type="application/json"> et "application/ld+json"."""
    out: list[object] = []
    for attrs, raw in _SCRIPT.findall(page):
        if "json" not in attrs.lower():
            continue
        raw = raw.strip()
        for candidate in (raw, html.unescape(raw)):
            try:
                out.append(json.loads(candidate))
                break
            except ValueError:
                continue
    return out


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_apple_page(page: str, ref: PlaylistRef) -> Playlist:
    """Extrait nom et morceaux d'une page music.apple.com/…/playlist/…."""
    name = None
    description = None
    tracks: list[Track] = []
    scripts = _json_scripts(page)

    # 1) Données internes de la page : objets « track-lockup » avec title + artistName.
    seen = set()
    for data in scripts:
        for node in _walk(data):
            title = node.get("title")
            artist = node.get("artistName")
            if isinstance(title, str) and isinstance(artist, str) and (
                "duration" in node or "contentDescriptor" in node or "trackNumber" in node
            ):
                duration = node.get("duration")
                ident = None
                desc = node.get("contentDescriptor") or {}
                ids = desc.get("identifiers") if isinstance(desc, dict) else None
                if isinstance(ids, dict):
                    ident = ids.get("storeAdamID")
                key = (title, artist, ident)
                if key in seen:
                    continue
                seen.add(key)
                tracks.append(
                    Track(
                        title=title,
                        artists=split_artists(artist),
                        album=node.get("albumName") if isinstance(node.get("albumName"), str) else None,
                        duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                        platform_id=str(ident) if ident else None,
                    )
                )

    # 2) Données schema.org (nom de la playlist, et morceaux si rien trouvé au-dessus).
    for data in scripts:
        for node in _walk(data):
            if node.get("@type") == "MusicPlaylist":
                name = name or node.get("name")
                description = description or node.get("description")
                if not tracks:
                    for rec in node.get("track") or []:
                        if not isinstance(rec, dict) or not rec.get("name"):
                            continue
                        by = rec.get("byArtist")
                        if isinstance(by, dict):
                            artists = [by.get("name")] if by.get("name") else []
                        elif isinstance(by, list):
                            artists = [a.get("name") for a in by if isinstance(a, dict) and a.get("name")]
                        else:
                            artists = []
                        tracks.append(
                            Track(
                                title=rec["name"],
                                artists=artists,
                                duration_ms=parse_iso_duration(rec.get("duration")),
                            )
                        )

    if not name:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
        if m:
            name = html.unescape(m.group(1)).split(" - ")[0].split(" by ")[0].split(" par ")[0]
    image = None
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
    if m:
        image = html.unescape(m.group(1))

    return Playlist(
        name=name or "Playlist Apple Music",
        description=description,
        image=image,
        url=ref.url,
        platform="apple",
        tracks=tracks,
    )
