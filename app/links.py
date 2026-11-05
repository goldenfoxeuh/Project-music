"""Reconnaissance des liens de playlists (Spotify, Deezer, YouTube Music, Apple Music)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from .models import PlaylistRef, ProviderError

# Liens courts qu'il faut suivre pour obtenir l'URL réelle.
SHORT_LINK_HOSTS = {
    "spotify.link",
    "spotify.app.link",
    "link.deezer.com",
    "deezer.page.link",
    "dzr.page.link",
}

_SPOTIFY_URI = re.compile(r"^spotify:(?:user:[^:]+:)?playlist:([A-Za-z0-9]+)$")


def parse_link(url: str) -> PlaylistRef:
    """Transforme un lien collé par l'utilisateur en référence de playlist.

    Lève ProviderError si le lien n'est pas reconnu.
    """
    url = (url or "").strip()
    if not url:
        raise ProviderError("Colle le lien d'une playlist.")

    m = _SPOTIFY_URI.match(url)
    if m:
        return PlaylistRef("spotify", m.group(1), url)

    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    # --- Spotify : open.spotify.com/(intl-fr/)playlist/<id>
    if host in ("open.spotify.com", "play.spotify.com"):
        if "playlist" in parts:
            i = parts.index("playlist")
            if i + 1 < len(parts):
                return PlaylistRef("spotify", parts[i + 1], url)
        raise ProviderError("Ce lien Spotify ne pointe pas vers une playlist.")

    # --- Deezer : deezer.com/fr/playlist/<id>
    if host.endswith("deezer.com"):
        if "playlist" in parts:
            i = parts.index("playlist")
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                return PlaylistRef("deezer", parts[i + 1], url)
        raise ProviderError("Ce lien Deezer ne pointe pas vers une playlist.")

    # --- YouTube / YouTube Music : ?list=<id> ou /browse/VL<id>
    if host in ("music.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"):
        if "list" in query and query["list"][0]:
            return PlaylistRef("youtube", query["list"][0], url)
        if len(parts) >= 2 and parts[0] == "browse":
            browse_id = parts[1]
            if browse_id.startswith("VL"):
                browse_id = browse_id[2:]
            return PlaylistRef("youtube", browse_id, url)
        raise ProviderError(
            "Ce lien YouTube ne contient pas de playlist (paramètre « list= »)."
        )

    # --- Apple Music : music.apple.com/fr/playlist/<nom>/pl.xxxxx
    if host in ("music.apple.com", "itunes.apple.com", "embed.music.apple.com"):
        # Playlist de la bibliothèque perso : music.apple.com/library/playlist/p.xxxxx
        if parts[:2] == ["library", "playlist"] and len(parts) >= 3 and parts[2].startswith("p."):
            return PlaylistRef("apple", parts[2], url)
        storefront = parts[0] if parts and len(parts[0]) == 2 else "us"
        playlist_id = next((p for p in reversed(parts) if p.startswith("pl.")), None)
        if "playlist" in parts and playlist_id:
            return PlaylistRef("apple", playlist_id, url, storefront=storefront)
        raise ProviderError("Ce lien Apple Music ne pointe pas vers une playlist.")

    raise ProviderError(
        "Lien non reconnu. Plateformes acceptées : Spotify, Deezer, "
        "YouTube Music et Apple Music."
    )


async def resolve_link(url: str, client: httpx.AsyncClient | None = None) -> PlaylistRef:
    """Comme parse_link, mais suit d'abord les liens courts (spotify.link, link.deezer.com...)."""
    candidate = url.strip()
    if not re.match(r"^(https?://|spotify:)", candidate, re.I):
        candidate = "https://" + candidate
    host = (urlparse(candidate).hostname or "").lower()
    if host in SHORT_LINK_HOSTS:
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=15)
        try:
            resp = await client.get(candidate, follow_redirects=True)
            candidate = str(resp.url)
            # Certains liens courts renvoient une page HTML avec l'URL finale dedans.
            if (urlparse(candidate).hostname or "").lower() in SHORT_LINK_HOSTS:
                m = re.search(
                    r"https://(?:www\.)?(?:open\.spotify\.com|deezer\.com)/[^\s\"'<>]+",
                    resp.text,
                )
                if m:
                    candidate = m.group(0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Impossible d'ouvrir le lien court : {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
    return parse_link(candidate)
