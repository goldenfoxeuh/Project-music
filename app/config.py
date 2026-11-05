"""Configuration lue depuis les variables d'environnement (ou le fichier .env)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # Adresse publique de l'application : sert à construire les URL de retour OAuth.
    # Spotify refuse « localhost » : utiliser 127.0.0.1 en local.
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://127.0.0.1:8000").rstrip("/"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY") or secrets.token_hex(32))

    spotify_client_id: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_ID"))
    spotify_client_secret: str = field(default_factory=lambda: _env("SPOTIFY_CLIENT_SECRET"))

    deezer_app_id: str = field(default_factory=lambda: _env("DEEZER_APP_ID"))
    deezer_app_secret: str = field(default_factory=lambda: _env("DEEZER_APP_SECRET"))

    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))

    # Apple Music : soit un jeton développeur déjà généré,
    # soit les infos pour le générer (Team ID, Key ID, fichier .p8).
    apple_developer_token: str = field(default_factory=lambda: _env("APPLE_DEVELOPER_TOKEN"))
    apple_team_id: str = field(default_factory=lambda: _env("APPLE_TEAM_ID"))
    apple_key_id: str = field(default_factory=lambda: _env("APPLE_KEY_ID"))
    apple_private_key_path: str = field(default_factory=lambda: _env("APPLE_PRIVATE_KEY_PATH"))
    apple_storefront: str = field(default_factory=lambda: _env("APPLE_STOREFRONT", "fr").lower())

    # Dossier où sont enregistrées les connexions et les synchronisations (SQLite).
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "data"))
    # Synchronisation automatique : active ou non, et fréquence de vérification de l'échéancier.
    sync_enabled: bool = field(default_factory=lambda: _env("SYNC_ENABLED", "1") not in ("0", "false", "no"))
    sync_check_seconds: float = field(default_factory=lambda: float(_env("SYNC_CHECK_SECONDS", "60")))

    # Score minimum (0-100) pour considérer qu'un morceau est trouvé.
    match_threshold: float = field(default_factory=lambda: float(_env("MATCH_THRESHOLD", "70")))

    def redirect_uri(self, platform: str) -> str:
        return f"{self.base_url}/auth/{platform}/callback"

    def apple_private_key(self) -> str | None:
        if self.apple_private_key_path and Path(self.apple_private_key_path).is_file():
            return Path(self.apple_private_key_path).read_text()
        inline = _env("APPLE_PRIVATE_KEY")
        return inline.replace("\\n", "\n") if inline else None
