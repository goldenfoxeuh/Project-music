"""Synchronisation périodique entre une playlist et sa copie.

Principe : on garde une « photo » (snapshot) des morceaux de chaque playlist lors de la
dernière vérification. À chaque passage, on relit les deux playlists et on compare :
- un morceau présent maintenant mais absent de la photo a été AJOUTÉ par l'utilisateur ;
- un morceau présent dans la photo mais plus maintenant a été RETIRÉ.
Les ajouts et retraits sont reportés sur l'autre playlist (en retrouvant le morceau
équivalent sur l'autre plateforme), puis on met les photos à jour.

Modes :
- "one_way" : l'original est la référence, la copie suit (ce qu'on ajoute à la main
  dans la copie n'est pas touché) ;
- "two_way" : les changements faits d'un côté comme de l'autre sont reportés.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .matching import normalize
from .models import AuthRequired, Playlist, PlaylistRef, ProviderError, Track
from .providers.base import Provider
from .transfer import match_track

MODES = ("one_way", "two_way")
INTERVALS_HOURS = (1, 3, 6, 12, 24, 72, 168)
HISTORY_SIZE = 20


def track_key(track: Track) -> str:
    """Identifiant stable d'un morceau dans une playlist.

    Normalement l'identifiant de la plateforme ; à défaut (page Apple Music lue sans clé)
    on se rabat sur « artiste - titre » normalisé, préfixé par « ~ ».
    """
    if track.platform_id:
        return track.platform_id
    return "~" + normalize(track.label())


def is_real_id(key: str) -> bool:
    return bool(key) and not key.startswith("~")


@dataclass
class Side:
    platform: str
    playlist_id: str
    url: str | None = None
    name: str | None = None
    storefront: str | None = None

    def ref(self) -> PlaylistRef:
        return PlaylistRef(self.platform, self.playlist_id, self.url or "", self.storefront)


@dataclass
class Sync:
    id: str
    sid: str
    a: Side  # playlist d'origine
    b: Side  # copie
    mode: str = "one_way"
    interval_hours: float = 6
    propagate_deletions: bool = True
    enabled: bool = True
    snapshot_a: list[str] = field(default_factory=list)
    snapshot_b: list[str] = field(default_factory=list)
    # Correspondances connues : [clé du morceau côté A, identifiant côté B]
    pairs: list[list[str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_run: float | None = None
    next_run: float | None = None
    last_error: str | None = None
    history: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, sid: str, a: Side, b: Side, **kw) -> "Sync":
        sync = cls(id=uuid.uuid4().hex[:16], sid=sid, a=a, b=b, **kw)
        sync.next_run = time.time() + sync.interval_hours * 3600
        return sync

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Sync":
        data = dict(data)
        data["a"] = Side(**data["a"])
        data["b"] = Side(**data["b"])
        return cls(**data)

    def public(self) -> dict:
        """Ce qu'on montre dans l'interface (sans les photos complètes)."""
        return {
            "id": self.id,
            "a": asdict(self.a),
            "b": asdict(self.b),
            "mode": self.mode,
            "interval_hours": self.interval_hours,
            "propagate_deletions": self.propagate_deletions,
            "enabled": self.enabled,
            "tracks_a": len(self.snapshot_a),
            "tracks_b": len(self.snapshot_b),
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_error": self.last_error,
            "last_result": self.history[-1] if self.history else None,
        }


@dataclass
class SyncResult:
    added_to_b: list[str] = field(default_factory=list)
    removed_from_b: list[str] = field(default_factory=list)
    added_to_a: list[str] = field(default_factory=list)
    removed_from_a: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"at": time.time()}

    @property
    def changed(self) -> bool:
        return bool(self.added_to_b or self.removed_from_b or self.added_to_a or self.removed_from_a)


def _ordered_keys(playlist: Playlist) -> tuple[list[str], dict[str, Track]]:
    keys: list[str] = []
    by_key: dict[str, Track] = {}
    for t in playlist.tracks:
        k = track_key(t)
        if k not in by_key:
            by_key[k] = t
            keys.append(k)
    return keys, by_key


def _mass_removal(removed: set[str], snapshot: list[str]) -> bool:
    """Garde-fou : une playlist relue presque vide est plus probablement une erreur de lecture."""
    return len(removed) > 5 and len(removed) > 0.5 * len(snapshot)


@dataclass
class _Plan:
    add: list[str] = field(default_factory=list)
    add_labels: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    remove_labels: list[str] = field(default_factory=list)


async def _plan(
    *,
    added: list[str],
    removed: set[str],
    src_tracks: dict[str, Track],
    src_current: set[str],
    dst_tracks: dict[str, Track],
    forward: dict[str, str],
    src: Provider,
    dst: Provider,
    src_token,
    dst_token,
    threshold: float,
    propagate_deletions: bool,
    result: SyncResult,
    dst_label: str,
) -> _Plan:
    """Calcule ce qu'il faut ajouter / retirer côté destination.

    `forward` associe une clé côté source à un identifiant côté destination ; il est
    complété avec les nouvelles correspondances trouvées.
    """
    plan = _Plan()
    for key in added:
        track = src_tracks[key]
        target_id = forward.get(key)
        if not target_id:
            match = await match_track(track, src, dst, src_token, dst_token, threshold)
            if not match.match or not match.match.platform_id:
                result.not_found.append(track.label())
                continue
            target_id = match.match.platform_id
            forward[key] = target_id
        if target_id not in dst_tracks and target_id not in plan.add and is_real_id(target_id):
            plan.add.append(target_id)
            plan.add_labels.append(track.label())

    if propagate_deletions and removed:
        still_wanted = {forward[k] for k in src_current if k in forward}
        for key in removed:
            target_id = forward.get(key)
            if target_id and target_id in dst_tracks and target_id not in still_wanted:
                plan.remove.append(target_id)
                plan.remove_labels.append(dst_tracks[target_id].label())
        if plan.remove and not dst.supports_remove:
            result.warnings.append(
                f"{dst.name} ne permet pas de retirer des morceaux : {len(plan.remove)} "
                f"suppression(s) à faire à la main dans {dst_label} "
                f"({', '.join(plan.remove_labels[:5])}{'…' if len(plan.remove) > 5 else ''})."
            )
            plan.remove, plan.remove_labels = [], []
    return plan


async def run_sync(
    sync: Sync,
    providers: dict[str, Provider],
    tokens,
    threshold: float = 70.0,
) -> SyncResult:
    """Une vérification complète. Modifie `sync` (photos, correspondances)."""
    pa, pb = providers[sync.a.platform], providers[sync.b.platform]
    ta = await pa.valid_token(tokens.get(sync.sid, pa.key))
    tb = await pb.valid_token(tokens.get(sync.sid, pb.key))
    if tb is None:
        raise AuthRequired(pb.name, f"Reconnecte ton compte {pb.name} pour continuer la synchronisation.")
    if sync.mode == "two_way" and ta is None:
        raise AuthRequired(
            pa.name, f"Reconnecte ton compte {pa.name} : la synchronisation dans les deux sens en a besoin."
        )

    result = SyncResult()
    playlist_a = await pa.read_owned(sync.a.ref(), ta)
    playlist_b = await pb.read_owned(sync.b.ref(), tb)
    keys_a, tracks_a = _ordered_keys(playlist_a)
    keys_b, tracks_b = _ordered_keys(playlist_b)
    cur_a, cur_b = set(keys_a), set(keys_b)
    snap_a, snap_b = set(sync.snapshot_a), set(sync.snapshot_b)

    added_a = [k for k in keys_a if k not in snap_a]
    removed_a = snap_a - cur_a
    added_b = [k for k in keys_b if k not in snap_b]
    removed_b = snap_b - cur_b

    for removed, snapshot, name in ((removed_a, sync.snapshot_a, sync.a.name), (removed_b, sync.snapshot_b, sync.b.name)):
        if _mass_removal(removed, snapshot):
            result.warnings.append(
                f"{len(removed)} morceaux semblent avoir disparu de « {name} » : "
                "suppression non reportée par sécurité."
            )
            removed.clear()

    a_to_b: dict[str, str] = {a: b for a, b in sync.pairs}

    # --- Original -> copie
    plan_b = await _plan(
        added=added_a, removed=removed_a, src_tracks=tracks_a, src_current=cur_a,
        dst_tracks=tracks_b, forward=a_to_b, src=pa, dst=pb, src_token=ta, dst_token=tb,
        threshold=threshold, propagate_deletions=sync.propagate_deletions, result=result,
        dst_label=f"« {sync.b.name} »",
    )

    # --- Copie -> original (deux sens)
    plan_a = _Plan()
    if sync.mode == "two_way":
        b_to_a: dict[str, str] = {}
        for a_key, b_id in a_to_b.items():
            if b_id not in b_to_a or a_key in cur_a:
                b_to_a[b_id] = a_key
        plan_a = await _plan(
            added=[k for k in added_b if is_real_id(k)], removed=removed_b, src_tracks=tracks_b,
            src_current=cur_b, dst_tracks=tracks_a, forward=b_to_a, src=pb, dst=pa,
            src_token=tb, dst_token=ta, threshold=threshold,
            propagate_deletions=sync.propagate_deletions, result=result,
            dst_label=f"« {sync.a.name} »",
        )
        for b_id, a_key in b_to_a.items():
            a_to_b.setdefault(a_key, b_id)

    # --- Application des changements
    if plan_b.add:
        await pb.add_tracks(sync.b.playlist_id, plan_b.add, tb)
        result.added_to_b = plan_b.add_labels
    if plan_b.remove:
        await pb.remove_tracks(sync.b.playlist_id, plan_b.remove, tb)
        result.removed_from_b = plan_b.remove_labels
    if plan_a.add:
        await pa.add_tracks(sync.a.playlist_id, plan_a.add, ta)
        result.added_to_a = plan_a.add_labels
    if plan_a.remove:
        await pa.remove_tracks(sync.a.playlist_id, plan_a.remove, ta)
        result.removed_from_a = plan_a.remove_labels

    # --- Nouvelles photos
    gone_a, gone_b = set(plan_a.remove), set(plan_b.remove)
    sync.snapshot_a = [k for k in keys_a if k not in gone_a] + plan_a.add
    sync.snapshot_b = [k for k in keys_b if k not in gone_b] + plan_b.add
    keep_a, keep_b = set(sync.snapshot_a), set(sync.snapshot_b)
    sync.pairs = [[a, b] for a, b in a_to_b.items() if a in keep_a or b in keep_b]
    sync.a.name = playlist_a.name or sync.a.name
    sync.b.name = playlist_b.name or sync.b.name
    return result


class SyncRunner:
    """Lance les synchronisations à l'heure prévue, en tâche de fond."""

    def __init__(self, store, providers, tokens, threshold: float, check_every: float = 60):
        self.store = store
        self.providers = providers
        self.tokens = tokens
        self.threshold = threshold
        self.check_every = check_every
        self._running: set[str] = set()
        self._task: asyncio.Task | None = None

    async def run_one(self, sync_id: str) -> Sync:
        if sync_id in self._running:
            raise ProviderError("Une synchronisation est déjà en cours pour cette playlist.", 409)
        self._running.add(sync_id)
        try:
            sync = self.store.get(sync_id)
            if sync is None:
                raise ProviderError("Synchronisation introuvable.", 404)
            now = time.time()
            try:
                result = await run_sync(sync, self.providers, self.tokens, self.threshold)
                sync.last_error = None
                sync.history = (sync.history + [result.to_dict()])[-HISTORY_SIZE:]
            except ProviderError as exc:
                sync.last_error = exc.message
            except Exception as exc:  # on n'arrête jamais la boucle pour une erreur
                sync.last_error = f"Erreur inattendue : {exc}"
            finally:
                # Les jetons ont pu être renouvelés pendant la vérification.
                for key in {sync.a.platform, sync.b.platform}:
                    tok = self.tokens.get(sync.sid, key)
                    if tok is not None:
                        await self.providers[key].valid_token(tok)
                        self.tokens.set(sync.sid, key, tok)
            sync.last_run = now
            sync.next_run = time.time() + sync.interval_hours * 3600
            # Relit la version stockée au cas où l'utilisateur a modifié les réglages entre-temps.
            current = self.store.get(sync_id)
            if current is None:
                return sync
            for f in ("mode", "interval_hours", "propagate_deletions", "enabled"):
                setattr(sync, f, getattr(current, f))
            self.store.save(sync)
            return sync
        finally:
            self._running.discard(sync_id)

    async def tick(self) -> int:
        """Lance les synchronisations arrivées à échéance. Renvoie leur nombre."""
        now = time.time()
        due = [s for s in self.store.all() if s.enabled and (s.next_run or 0) <= now]
        for s in due:
            if s.id not in self._running:
                await self.run_one(s.id)
        return len(due)

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # pragma: no cover - la boucle ne doit jamais mourir
                pass
            await asyncio.sleep(self.check_every)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
