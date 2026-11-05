"""Registre des plateformes prises en charge."""

from __future__ import annotations

import httpx

from ..config import Settings
from .apple import AppleMusicProvider
from .base import Provider
from .deezer import DeezerProvider
from .spotify import SpotifyProvider
from .youtube import YouTubeMusicProvider

PROVIDER_CLASSES: list[type[Provider]] = [
    SpotifyProvider,
    DeezerProvider,
    YouTubeMusicProvider,
    AppleMusicProvider,
]


def build_providers(settings: Settings, http: httpx.AsyncClient) -> dict[str, Provider]:
    return {cls.key: cls(settings, http) for cls in PROVIDER_CLASSES}


__all__ = ["Provider", "build_providers", "PROVIDER_CLASSES"]
