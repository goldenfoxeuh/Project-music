"""Structures de données communes à toutes les plateformes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Track:
    """Un morceau, décrit de façon indépendante de la plateforme."""

    title: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    # Identifiant du morceau sur la plateforme d'où il vient
    # (id Spotify, id Deezer, videoId YouTube, id Apple Music...).
    platform_id: str | None = None

    @property
    def main_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def label(self) -> str:
        if self.artists:
            return f"{', '.join(self.artists)} - {self.title}"
        return self.title

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Playlist:
    name: str
    tracks: list[Track]
    platform: str
    url: str | None = None
    description: str | None = None
    owner: str | None = None
    image: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "platform": self.platform,
            "url": self.url,
            "description": self.description,
            "owner": self.owner,
            "image": self.image,
            "tracks": [t.to_dict() for t in self.tracks],
        }


@dataclass
class PlaylistRef:
    """Résultat de l'analyse d'un lien : quelle plateforme, quel identifiant."""

    platform: str
    playlist_id: str
    url: str
    # Pays de la boutique (Apple Music : "fr", "us"...).
    storefront: str | None = None


class ProviderError(Exception):
    """Erreur lisible par l'utilisateur, renvoyée telle quelle à l'interface."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthRequired(ProviderError):
    """L'utilisateur doit connecter son compte sur la plateforme concernée."""

    def __init__(self, platform: str, message: str | None = None):
        super().__init__(
            message or f"Connecte ton compte {platform} pour continuer.", 401
        )
        self.platform = platform
