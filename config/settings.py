"""
config/settings.py
──────────────────
Loads all configuration from environment variables (populated by .env).
Import `cfg` anywhere in the project to access typed settings.

Usage:
    from config.settings import cfg
    print(cfg.apify_dataset_id)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env relative to the project root (one level above config/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _require(name: str) -> str:
    """Return env var value or raise a clear error if it is missing."""
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            f"Copy .env.example to .env and fill in the value."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # ── Apify ────────────────────────────────────────────────
    apify_token: str
    apify_dataset_id: str          # Dataset from your successful run
    apify_task_id: str             # Saved Task ID (optional, for Phase 1b)

    # ── Google Sheets ────────────────────────────────────────
    google_service_account_json: Path
    google_spreadsheet_id: str
    google_worksheet_name: str

    # ── AI (Phase 2+) ────────────────────────────────────────
    ai_api_key: str
    ai_model: str

    # ── Runtime flags ────────────────────────────────────────
    dry_run: bool = False          # Set via CLI flag, not .env

    @classmethod
    def from_env(cls) -> "Settings":
        service_account_path = _PROJECT_ROOT / _optional(
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "credentials/google_service_account.json",
        )
        return cls(
            # Optional at load time — the client will raise if actually needed
            apify_token=_optional("APIFY_TOKEN"),
            apify_dataset_id=_optional("APIFY_DATASET_ID"),
            apify_task_id=_optional("APIFY_TASK_ID"),
            google_service_account_json=service_account_path,
            google_spreadsheet_id=_optional("GOOGLE_SPREADSHEET_ID"),
            google_worksheet_name=_optional("GOOGLE_WORKSHEET_NAME", "Jobs"),
            ai_api_key=_optional("AI_API_KEY"),
            ai_model=_optional("AI_MODEL", "gpt-4o-mini"),
        )


# Module-level singleton – lazy so tests can monkeypatch env before import
_cfg: Settings | None = None


def get_settings(dry_run: bool = False) -> Settings:
    """Return the singleton Settings, constructing it on first call."""
    global _cfg
    if _cfg is None:
        _cfg = Settings.from_env()
    # dry_run can be toggled per CLI invocation without rebuilding
    if dry_run and not _cfg.dry_run:
        import dataclasses
        _cfg = dataclasses.replace(_cfg, dry_run=True)
    return _cfg
