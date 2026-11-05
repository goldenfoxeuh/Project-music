"""Transfert complet Deezer -> Spotify à travers l'API web, avec HTTP simulé."""

import json
import time

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage import Database

DEEZER_TRACKS = [
    {"id": 1, "type": "track", "title": "Alors on danse", "duration": 206,
     "artist": {"name": "Stromae"}, "album": {"title": "Cheese"}},
    {"id": 2, "type": "track", "title": "Song That Does Not Exist", "duration": 100,
     "artist": {"name": "Nobody"}, "album": {"title": "X"}},
]
SPOTIFY_HIT = {
    "type": "track", "id": "sp1", "uri": "spotify:track:sp1", "name": "Alors on danse - Radio Edit",
    "artists": [{"name": "Stromae"}], "album": {"name": "Cheese"}, "duration_ms": 207000,
    "external_ids": {"isrc": "BEX1"},
}


def handler(created: dict):
    def _h(req: httpx.Request):
        host, path = req.url.host, req.url.path
        if host == "api.deezer.com":
            if path == "/playlist/42":
                return httpx.Response(200, json={"title": "Ma playlist", "link": "https://www.deezer.com/playlist/42"})
            if path == "/playlist/42/tracks":
                return httpx.Response(200, json={"data": DEEZER_TRACKS, "total": 2})
            if path == "/track/1":
                return httpx.Response(200, json=DEEZER_TRACKS[0] | {"isrc": "BEX1"})
            if path == "/track/2":
                return httpx.Response(200, json=DEEZER_TRACKS[1])
        if host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "app", "expires_in": 3600})
        if host == "api.spotify.com":
            if path == "/v1/search":
                q = req.url.params["q"]
                items = [SPOTIFY_HIT] if ("BEX1" in q or "danse" in q.lower()) else []
                return httpx.Response(200, json={"tracks": {"items": items}})
            if path == "/v1/me/playlists":
                created["body"] = json.loads(req.content)
                return httpx.Response(201, json={"id": "np", "external_urls": {"spotify": "https://open.spotify.com/playlist/np"}})
            if path == "/v1/playlists/np/items":
                created["uris"] = json.loads(req.content)["uris"]
                return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    return _h


def test_full_transfer_flow():
    s = Settings()
    s.spotify_client_id, s.spotify_client_secret = "id", "secret"
    s.secret_key = "test"
    created: dict = {}
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler(created)))
    app = create_app(s, http, Database(":memory:"))

    with TestClient(app) as c:
        platforms = {p["key"]: p for p in c.get("/api/platforms").json()}
        assert platforms["spotify"]["can_write"] and not platforms["spotify"]["connected"]

        r = c.post("/api/playlist", json={"url": "https://www.deezer.com/fr/playlist/42"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["playlist"]["name"] == "Ma playlist"
        assert len(data["playlist"]["tracks"]) == 2

        # Pas encore connecté à Spotify -> demande de connexion.
        r = c.post("/api/match", json={"playlist_key": data["key"], "target": "spotify"})
        assert r.status_code == 401 and r.json()["auth_required"] == "spotify"

        # Simule une connexion Spotify réussie (identifiant de session = celui de la playlist lue).
        session_id = app.state.playlists[data["key"]][0]
        app.state.tokens.set(session_id, "spotify", {"access_token": "user", "expires_at": time.time() + 3600})

        job = c.post("/api/match", json={"playlist_key": data["key"], "target": "spotify"}).json()
        for _ in range(100):
            job = c.get(f"/api/jobs/{job['id']}").json()
            if job["status"] != "running":
                break
            time.sleep(0.05)
        assert job["status"] == "matched"
        assert job["found"] == 1 and job["missing"] == 1
        first = job["results"][0]
        assert first["match"]["platform_id"] == "spotify:track:sp1"
        assert first["method"] == "isrc"

        r = c.post("/api/create", json={
            "job_id": job["id"], "name": "Copie", "track_ids": ["spotify:track:sp1", "spotify:track:forged"],
        })
        assert r.status_code == 200, r.text
        assert r.json() == {"url": "https://open.spotify.com/playlist/np", "added": 1, "can_sync": True}
        assert created["uris"] == ["spotify:track:sp1"]
        assert created["body"]["name"] == "Copie"

        # Active la synchronisation de l'original vers la copie.
        r = c.post("/api/syncs", json={"job_id": job["id"], "mode": "one_way", "interval_hours": 6})
        assert r.status_code == 200, r.text
        sync = r.json()
        assert sync["a"]["platform"] == "deezer" and sync["b"]["platform"] == "spotify"
        assert sync["tracks_a"] == 2 and sync["tracks_b"] == 1
        # Deux sens : il faut aussi être connecté à Deezer (non configuré ici).
        r = c.post("/api/syncs", json={"job_id": job["id"], "mode": "two_way"})
        assert r.status_code in (401, 503)
        assert [x["id"] for x in c.get("/api/syncs").json()] == [sync["id"]]
        r = c.patch(f"/api/syncs/{sync['id']}", json={"enabled": False, "interval_hours": 24})
        assert r.json()["enabled"] is False and r.json()["interval_hours"] == 24
        assert c.delete(f"/api/syncs/{sync['id']}").json() == {"ok": True}
        assert c.get("/api/syncs").json() == []


def test_unknown_link_returns_message():
    s = Settings()
    s.secret_key = "test"
    app = create_app(s, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))), Database(":memory:"))
    with TestClient(app) as c:
        r = c.post("/api/playlist", json={"url": "https://example.com/x"})
        assert r.status_code == 400
        assert "non reconnu" in r.json()["error"]
        assert c.get("/").status_code == 200
