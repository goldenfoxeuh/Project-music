"""Synchronisation entre deux playlists, avec deux plateformes simulées en mémoire."""

import time

import pytest

from app.config import Settings
from app.models import Playlist, PlaylistRef, Track
from app.providers.base import Provider
from app.storage import Database, SyncStore, TokenStore
from app.sync import Side, Sync, SyncRunner, run_sync

SONGS = [
    ("Alors on danse", "Stromae"),
    ("Formidable", "Stromae"),
    ("Papaoutai", "Stromae"),
    ("Tous les mêmes", "Stromae"),
    ("Santé", "Stromae"),
    ("Hello", "Adele"),
    ("Skyfall", "Adele"),
    ("Rolling in the Deep", "Adele"),
]


class FakePlatform(Provider):
    """Plateforme jouet : un catalogue (titre -> id) et des playlists modifiables."""

    supports_isrc = False

    def __init__(self, key, prefix, supports_remove=True):
        super().__init__(Settings(), None)
        self.key = self.name = key
        self.prefix = prefix
        self.supports_remove = supports_remove
        self.catalog = {f"{prefix}{i}": Track(t, [a], platform_id=f"{prefix}{i}") for i, (t, a) in enumerate(SONGS)}
        self.playlists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []

    def track(self, i):
        return f"{self.prefix}{i}"

    async def read_playlist(self, ref, token):
        ids = self.playlists[ref.playlist_id]
        return Playlist(name=f"pl-{self.key}", platform=self.key,
                        tracks=[Track(self.catalog[i].title, self.catalog[i].artists, platform_id=i) for i in ids])

    async def search(self, track, token):
        return [t for t in self.catalog.values() if t.title.lower() == track.title.lower()]

    async def add_tracks(self, playlist_id, ids, token):
        self.calls.append(("add", ids))
        self.playlists[playlist_id] += ids
        return len(ids)

    async def remove_tracks(self, playlist_id, ids, token):
        self.calls.append(("remove", ids))
        self.playlists[playlist_id] = [i for i in self.playlists[playlist_id] if i not in ids]
        return len(ids)


@pytest.fixture
def env():
    a, b = FakePlatform("a", "A"), FakePlatform("b", "B")
    a.playlists["pa"] = [a.track(i) for i in range(5)]
    b.playlists["pb"] = [b.track(i) for i in range(5)]
    db = Database(":memory:")
    tokens = TokenStore(db)
    tokens.set("s", "a", {"access_token": "x"})
    tokens.set("s", "b", {"access_token": "y"})
    sync = Sync.new(
        "s", Side("a", "pa", name="A"), Side("b", "pb", name="B"),
        snapshot_a=list(a.playlists["pa"]), snapshot_b=list(b.playlists["pb"]),
        pairs=[[a.track(i), b.track(i)] for i in range(5)],
    )
    return a, b, {"a": a, "b": b}, tokens, sync


async def test_nothing_changed(env):
    a, b, providers, tokens, sync = env
    res = await run_sync(sync, providers, tokens)
    assert not res.changed and a.calls == [] and b.calls == []


async def test_one_way_add_and_remove(env):
    a, b, providers, tokens, sync = env
    a.playlists["pa"].remove(a.track(1))          # retiré de l'original
    a.playlists["pa"].append(a.track(5))          # ajouté à l'original (Hello)
    b.playlists["pb"].append(b.track(7))          # ajout manuel dans la copie : ignoré en sens unique
    res = await run_sync(sync, providers, tokens)
    assert b.playlists["pb"] == [b.track(0), b.track(2), b.track(3), b.track(4), b.track(7), b.track(5)]
    assert res.added_to_b == ["Adele - Hello"]
    assert res.removed_from_b == ["Stromae - Formidable"]
    assert a.calls == []
    # Deuxième passage : plus rien à faire.
    res = await run_sync(sync, providers, tokens)
    assert not res.changed


async def test_two_way(env):
    a, b, providers, tokens, sync = env
    sync.mode = "two_way"
    a.playlists["pa"].append(a.track(5))                       # Hello ajouté dans A
    b.playlists["pb"].append(b.track(6))                       # Skyfall ajouté dans B
    b.playlists["pb"].remove(b.track(0))                       # Alors on danse retiré de B
    res = await run_sync(sync, providers, tokens)
    assert set(a.playlists["pa"]) == {a.track(i) for i in (1, 2, 3, 4, 5, 6)}
    assert set(b.playlists["pb"]) == {b.track(i) for i in (1, 2, 3, 4, 5, 6)}
    assert res.added_to_a == ["Adele - Skyfall"]
    assert res.removed_from_a == ["Stromae - Alors on danse"]
    res = await run_sync(sync, providers, tokens)
    assert not res.changed, res


async def test_deletions_not_propagated_when_disabled(env):
    a, b, providers, tokens, sync = env
    sync.propagate_deletions = False
    a.playlists["pa"].remove(a.track(1))
    await run_sync(sync, providers, tokens)
    assert b.track(1) in b.playlists["pb"]


async def test_mass_removal_guard(env):
    a, b, providers, tokens, sync = env
    a.playlists["pa"] = [a.track(i) for i in range(5)] + [a.track(5), a.track(6), a.track(7)]
    sync.snapshot_a = list(a.playlists["pa"])
    a.playlists["pa"] = []  # lecture « vide » : sûrement une erreur
    res = await run_sync(sync, providers, tokens)
    assert res.warnings and b.calls == []


async def test_platform_without_remove(env):
    a, b, providers, tokens, sync = env
    b.supports_remove = False
    a.playlists["pa"].remove(a.track(2))
    res = await run_sync(sync, providers, tokens)
    assert b.track(2) in b.playlists["pb"]
    assert "à la main" in res.warnings[0]


async def test_linking_existing_playlists_merges(env):
    """Sans photo de départ, la 1re vérification complète la copie sans doublons."""
    a, b, providers, tokens, _ = env
    b.playlists["pb"] = [b.track(0), b.track(7)]
    sync = Sync.new("s", Side("a", "pa", name="A"), Side("b", "pb", name="B"), mode="two_way")
    await run_sync(sync, providers, tokens)
    assert sorted(b.playlists["pb"]) == sorted([b.track(i) for i in (0, 7, 1, 2, 3, 4)])
    assert sorted(a.playlists["pa"]) == sorted([a.track(i) for i in (0, 1, 2, 3, 4, 7)])
    assert not (await run_sync(sync, providers, tokens)).changed


async def test_runner_runs_due_syncs_and_records_errors(env):
    a, b, providers, tokens, sync = env
    store = SyncStore(tokens.db)
    a.playlists["pa"].append(a.track(5))
    sync.next_run = time.time() - 1
    store.save(sync)
    runner = SyncRunner(store, providers, tokens, 70)
    assert await runner.tick() == 1
    saved = store.get(sync.id)
    assert saved.last_error is None and saved.history[-1]["added_to_b"] == ["Adele - Hello"]
    assert saved.next_run > time.time() + 3600
    assert await runner.tick() == 0  # pas encore l'heure

    tokens.delete("s", "b")
    out = await runner.run_one(sync.id)
    assert "Reconnecte" in out.last_error
