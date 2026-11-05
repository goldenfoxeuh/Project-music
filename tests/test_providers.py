"""Tests des plateformes avec des réponses HTTP simulées (aucun accès réseau)."""

import json

import httpx
import pytest

from app.config import Settings
from app.models import AuthRequired, PlaylistRef
from app.providers.apple import parse_apple_page, parse_iso_duration, split_artists
from app.providers.deezer import DeezerProvider
from app.providers.spotify import SpotifyProvider


def settings(**kw):
    s = Settings()
    s.spotify_client_id = "id"
    s.spotify_client_secret = "secret"
    s.deezer_app_id = ""
    s.deezer_app_secret = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- Deezer
def deezer_track(i):
    return {
        "id": i,
        "type": "track",
        "title": f"Song {i}",
        "duration": 200,
        "artist": {"name": "Artist"},
        "album": {"title": "Album"},
    }


async def test_deezer_reads_all_pages():
    def handler(req: httpx.Request):
        path = req.url.path
        if path == "/playlist/42":
            return httpx.Response(200, json={"title": "Ma playlist", "creator": {"name": "Moi"}})
        if path == "/playlist/42/tracks":
            index = int(req.url.params.get("index", 0))
            data = [deezer_track(i) for i in range(index, min(index + 100, 150))]
            body = {"data": data, "total": 150}
            if index + 100 < 150:
                body["next"] = "x"
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    async with client(handler) as http:
        p = DeezerProvider(settings(), http)
        pl = await p.read_playlist(PlaylistRef("deezer", "42", "u"), None)
    assert pl.name == "Ma playlist"
    assert len(pl.tracks) == 150
    assert pl.tracks[0].duration_ms == 200000
    assert pl.tracks[149].platform_id == "149"


async def test_deezer_isrc_lookup_and_enrich():
    def handler(req):
        if req.url.path == "/track/isrc:FR123":
            return httpx.Response(200, json=deezer_track(7) | {"isrc": "FR123"})
        if req.url.path == "/track/7":
            return httpx.Response(200, json=deezer_track(7) | {"isrc": "FR123"})
        return httpx.Response(200, json={"error": {"code": 800, "message": "no data"}})

    async with client(handler) as http:
        p = DeezerProvider(settings(), http)
        found = await p.find_by_isrc("FR123", None)
        assert found[0].platform_id == "7"
        assert await p.find_by_isrc("NOPE", None) == []
        t = await p.enrich(DeezerProvider._parse_track(deezer_track(7)), None)
        assert t.isrc == "FR123"


# ---------------------------------------------------------------- Spotify
def sp_track(i, isrc=None):
    return {
        "type": "track",
        "id": f"t{i}",
        "uri": f"spotify:track:t{i}",
        "name": f"Song {i}",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album"},
        "duration_ms": 180000,
        "external_ids": {"isrc": isrc} if isrc else {},
    }


async def test_spotify_reads_new_items_format():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        if req.url.path == "/v1/playlists/abc":
            return httpx.Response(200, json={"name": "Chill", "owner": {"display_name": "Bob"}, "images": []})
        if req.url.path == "/v1/playlists/abc/items":
            return httpx.Response(
                200,
                json={"items": [{"item": sp_track(1, "US1")}, {"item": None}, {"track": sp_track(2)}], "next": None},
            )
        return httpx.Response(404)

    async with client(handler) as http:
        p = SpotifyProvider(settings(), http)
        pl = await p.read_playlist(PlaylistRef("spotify", "abc", "u"), None)
    assert [t.title for t in pl.tracks] == ["Song 1", "Song 2"]
    assert pl.tracks[0].isrc == "US1"
    assert pl.tracks[0].platform_id == "spotify:track:t1"


async def test_spotify_falls_back_to_old_tracks_endpoint():
    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        if req.url.path == "/v1/playlists/abc":
            return httpx.Response(200, json={"name": "Old"})
        if req.url.path == "/v1/playlists/abc/tracks":
            return httpx.Response(200, json={"items": [{"track": sp_track(1)}]})
        return httpx.Response(404)

    async with client(handler) as http:
        pl = await SpotifyProvider(settings(), http).read_playlist(PlaylistRef("spotify", "abc", "u"), None)
    assert len(pl.tracks) == 1


async def test_spotify_asks_login_when_forbidden():
    def handler(req):
        if req.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        if req.url.path == "/v1/playlists/abc":
            return httpx.Response(200, json={"name": "Someone else's"})
        return httpx.Response(403, json={"error": {"status": 403}})

    async with client(handler) as http:
        with pytest.raises(AuthRequired):
            await SpotifyProvider(settings(), http).read_playlist(PlaylistRef("spotify", "abc", "u"), None)


async def test_spotify_create_playlist_in_chunks():
    added = []

    def handler(req):
        if req.url.path == "/v1/me/playlists" and req.method == "POST":
            return httpx.Response(201, json={"id": "new", "external_urls": {"spotify": "https://open.spotify.com/playlist/new"}})
        if req.url.path == "/v1/playlists/new/items":
            added.append(len(json.loads(req.content)["uris"]))
            return httpx.Response(201, json={"snapshot_id": "s"})
        return httpx.Response(404)

    async with client(handler) as http:
        url = await SpotifyProvider(settings(), http).create_playlist(
            "Copie", "desc", [f"spotify:track:{i}" for i in range(250)], {"access_token": "user"}
        )
    assert url.endswith("/new")
    assert added == [100, 100, 50]


# ---------------------------------------------------------------- Apple
def test_parse_iso_duration():
    assert parse_iso_duration("PT3M25S") == 205000
    assert parse_iso_duration("PT1H0M1S") == 3601000
    assert parse_iso_duration(None) is None


def test_split_artists():
    assert split_artists("Daft Punk & Pharrell Williams") == ["Daft Punk", "Pharrell Williams"]
    assert split_artists("Stromae") == ["Stromae"]


def test_parse_apple_page_internal_data():
    data = {
        "data": [
            {
                "sections": [
                    {
                        "items": [
                            {"title": "Papaoutai", "artistName": "Stromae", "duration": 232000,
                             "contentDescriptor": {"identifiers": {"storeAdamID": "111"}}},
                            {"title": "Formidable", "artistName": "Stromae", "duration": 214000,
                             "contentDescriptor": {"identifiers": {"storeAdamID": "222"}}},
                        ]
                    }
                ]
            }
        ]
    }
    ld = {"@type": "MusicPlaylist", "name": "Ma playlist Apple"}
    page = (
        '<html><head><meta property="og:image" content="https://img/x.jpg">'
        f'<script type="application/ld+json" id="schema:music-playlist">{json.dumps(ld)}</script>'
        f'<script type="application/json" id="serialized-server-data">{json.dumps(data)}</script>'
        "</head></html>"
    )
    pl = parse_apple_page(page, PlaylistRef("apple", "pl.x", "https://music.apple.com/fr/playlist/x/pl.x"))
    assert pl.name == "Ma playlist Apple"
    assert [t.title for t in pl.tracks] == ["Papaoutai", "Formidable"]
    assert pl.tracks[0].platform_id == "111"
    assert pl.image == "https://img/x.jpg"


def test_parse_apple_page_schema_fallback():
    ld = {
        "@type": "MusicPlaylist",
        "name": "Fallback",
        "track": [{"@type": "MusicRecording", "name": "One", "duration": "PT3M", "byArtist": {"name": "A"}}],
    }
    page = f'<script id="schema:music-playlist" type="application/ld+json">{json.dumps(ld)}</script>'
    pl = parse_apple_page(page, PlaylistRef("apple", "pl.x", "u"))
    assert pl.tracks[0].title == "One"
    assert pl.tracks[0].artists == ["A"]
    assert pl.tracks[0].duration_ms == 180000
