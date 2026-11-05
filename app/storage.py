"""Stockage persistant (SQLite) : connexions aux comptes et synchronisations.

Les synchronisations tournent en arrière-plan, même quand personne n'a la page ouverte,
et doivent survivre à un redémarrage du serveur : on enregistre donc les jetons
de connexion et l'état des synchronisations sur le disque.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    sid TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (sid, platform)
                );
                CREATE TABLE IF NOT EXISTS syncs (
                    id TEXT PRIMARY KEY,
                    sid TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                """
            )

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self._conn.close()


class TokenStore:
    """Jetons de connexion par session (jamais envoyés au navigateur)."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, sid: str, platform: str) -> dict | None:
        rows = self.db.execute(
            "SELECT data FROM tokens WHERE sid = ? AND platform = ?", (sid, platform)
        )
        return json.loads(rows[0][0]) if rows else None

    def set(self, sid: str, platform: str, token: dict) -> None:
        self.db.execute(
            "INSERT INTO tokens (sid, platform, data) VALUES (?, ?, ?) "
            "ON CONFLICT(sid, platform) DO UPDATE SET data = excluded.data",
            (sid, platform, json.dumps(token)),
        )

    def delete(self, sid: str, platform: str) -> None:
        self.db.execute("DELETE FROM tokens WHERE sid = ? AND platform = ?", (sid, platform))


class SyncStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, sync) -> None:
        self.db.execute(
            "INSERT INTO syncs (id, sid, data) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (sync.id, sync.sid, json.dumps(sync.to_json())),
        )

    def get(self, sync_id: str):
        from .sync import Sync

        rows = self.db.execute("SELECT data FROM syncs WHERE id = ?", (sync_id,))
        return Sync.from_json(json.loads(rows[0][0])) if rows else None

    def for_session(self, sid: str) -> list:
        from .sync import Sync

        rows = self.db.execute("SELECT data FROM syncs WHERE sid = ?", (sid,))
        syncs = [Sync.from_json(json.loads(r[0])) for r in rows]
        return sorted(syncs, key=lambda s: s.created_at)

    def all(self) -> list:
        from .sync import Sync

        return [Sync.from_json(json.loads(r[0])) for r in self.db.execute("SELECT data FROM syncs")]

    def delete(self, sync_id: str) -> None:
        self.db.execute("DELETE FROM syncs WHERE id = ?", (sync_id,))
