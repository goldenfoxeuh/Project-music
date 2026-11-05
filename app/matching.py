"""Comparaison de morceaux entre plateformes.

Chaque plateforme écrit les titres un peu différemment
(« Song - Remastered 2011 », « Song (feat. X) », « Artist - Song (Official Video) »...).
On normalise les chaînes puis on calcule un score de ressemblance
entre le morceau d'origine et chaque résultat de recherche.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from .models import Track

# Morceaux de titre qui ne changent pas l'identité du morceau.
_NOISE = re.compile(
    r"""
    \b(
        remaster(ed)?(\s\d{2,4})?(\sversion)?
      | \d{4}\sremaster(ed)?
      | official\s(music\s)?(video|audio|lyric\svideo|visualizer)
      | lyric(s)?\svideo | audio(\sofficiel)? | clip\sofficiel | video\sofficielle
      | mono | stereo | explicit | clean
      | radio\s(edit|version) | single\s(version|edit) | album\sversion
      | hd | hq | 4k
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_FEAT = re.compile(r"[\(\[]?\s*\b(feat\.?|ft\.?|featuring|avec|with)\s[^\)\]]*[\)\]]?", re.I)
_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")

# Mots qui indiquent une version différente : on ne veut pas confondre
# « Song » et « Song (Live) » ou « Song - Remix ».
VERSION_WORDS = (
    "live",
    "remix",
    "acoustic",
    "acoustique",
    "instrumental",
    "karaoke",
    "cover",
    "sped up",
    "slowed",
    "nightcore",
    "demo",
    "edit",
    "mix",
    "version",
)


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize(text: str) -> str:
    text = strip_accents(text or "").lower()
    text = text.replace("&", " and ").replace("’", "'")
    text = _NON_ALNUM.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def clean_title(title: str) -> str:
    """Titre sans « feat. », mentions de remaster, de clip officiel, etc."""
    t = title or ""
    t = _FEAT.sub(" ", t)
    # « Song - 2011 Remaster » -> on coupe la partie après le tiret si ce n'est que du bruit.
    if " - " in t:
        head, tail = t.rsplit(" - ", 1)
        if not normalize(_NOISE.sub(" ", _BRACKETS.sub(" ", tail))):
            t = head
    t = _BRACKETS.sub(lambda m: m.group(0) if _version_words(m.group(0)) else " ", t)
    t = _NOISE.sub(" ", t)
    return normalize(t)


def _version_words(text: str) -> set[str]:
    n = normalize(text)
    words = set()
    for w in VERSION_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", n):
            words.add(w)
    return words


def version_markers(title: str) -> set[str]:
    """Mots-clés de version présents dans le titre (live, remix...), hors bruit."""
    t = _NOISE.sub(" ", _FEAT.sub(" ", title or ""))
    markers = _version_words(t)
    # « version » / « edit » seuls sont trop vagues s'ils ne qualifient rien d'autre.
    if markers - {"version", "edit"}:
        markers -= {"version", "edit"}
    return markers


def build_query(track: Track, include_album: bool = False) -> str:
    """Requête de recherche texte simple et efficace sur toutes les plateformes."""
    title = clean_title(track.title) or normalize(track.title)
    parts = [track.main_artist, title]
    if include_album and track.album:
        parts.append(track.album)
    return " ".join(p for p in parts if p).strip()


def _artist_score(source: Track, candidate: Track, candidate_title: str) -> float:
    src = [normalize(a) for a in source.artists if a]
    cand = [normalize(a) for a in candidate.artists if a]
    if not src:
        return 70.0
    if not cand:
        # Sur YouTube, l'artiste est parfois seulement dans le titre de la vidéo.
        haystack = normalize(candidate_title)
        return 100.0 if any(a and a in haystack for a in src) else 40.0
    best = 0.0
    for a in src:
        for b in cand:
            best = max(best, fuzz.token_set_ratio(a, b))
    # Artiste présent dans le titre du candidat (« Artiste - Titre »).
    haystack = normalize(candidate_title)
    if any(a and len(a) > 2 and a in haystack for a in src):
        best = max(best, 90.0)
    return best


def score(source: Track, candidate: Track) -> float:
    """Score de 0 à 100 : à quel point `candidate` est le même morceau que `source`."""
    if source.isrc and candidate.isrc and source.isrc.upper() == candidate.isrc.upper():
        return 100.0

    src_title = clean_title(source.title)
    cand_title_raw = candidate.title
    cand_title = clean_title(cand_title_raw)
    # Sur YouTube : « Artiste - Titre » -> on retire l'artiste du titre.
    for artist in source.artists:
        a = normalize(artist)
        if a and cand_title.startswith(a + " "):
            cand_title = cand_title[len(a) + 1 :]
    title_score = max(
        fuzz.ratio(src_title, cand_title),
        fuzz.token_set_ratio(src_title, cand_title) * 0.95,
    )
    artist_score = _artist_score(source, candidate, cand_title_raw)

    total = 0.6 * title_score + 0.4 * artist_score

    # Durée : forte indication quand elle est connue des deux côtés.
    if source.duration_ms and candidate.duration_ms:
        diff = abs(source.duration_ms - candidate.duration_ms) / 1000
        if diff <= 3:
            total += 5
        elif diff <= 10:
            total += 2
        elif diff > 30:
            total -= 15

    # Version différente (live, remix...) : pénalité.
    if version_markers(source.title) != version_markers(cand_title_raw):
        total -= 15

    return max(0.0, min(100.0, total))


def best_match(
    source: Track, candidates: list[Track], threshold: float = 70.0
) -> tuple[Track | None, float]:
    """Meilleur candidat au-dessus du seuil, avec son score."""
    best, best_score = None, 0.0
    for cand in candidates:
        s = score(source, cand)
        if s > best_score:
            best, best_score = cand, s
    if best is None or best_score < threshold:
        return None, best_score
    return best, best_score
