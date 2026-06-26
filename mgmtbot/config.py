from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sys

# Re-use BotMother's env loader (no extra dependency)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from botmother.config import load_env_file


def _bool_from_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _owner_id_from_env(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _path_from_env(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class MgmtSettings:
    mgmt_bot_token: str
    mother_bot_token: str
    db_path: Path
    owner_id: int | None
    credits_initial_free: int = 50
    log_level: str = "INFO"
    log_file: Path | None = None

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "MgmtSettings":
        load_env_file(env_file)
        base_dir = Path.cwd()
        log_file_raw = os.getenv("MGMT_LOG_FILE", "").strip()
        return cls(
            mgmt_bot_token=os.getenv("MGMT_BOT_TOKEN", "").strip(),
            mother_bot_token=os.getenv("MOTHER_BOT_TOKEN", "").strip(),
            db_path=_path_from_env(
                os.getenv("BOTMOTHER_DB", "./data/botmother.sqlite3"), base_dir
            ),
            owner_id=_owner_id_from_env(os.getenv("MGMT_OWNER_ID")),
            credits_initial_free=int(os.getenv("CREDITS_INITIAL_FREE", "50")),
            log_level=os.getenv("MGMT_LOG_LEVEL", "INFO").strip() or "INFO",
            log_file=_path_from_env(log_file_raw, base_dir) if log_file_raw else None,
        )

    def validate_for_runtime(self) -> None:
        missing = []
        if not self.mgmt_bot_token:
            missing.append("MGMT_BOT_TOKEN")
        if not self.mother_bot_token:
            missing.append("MOTHER_BOT_TOKEN")
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
