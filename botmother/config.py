from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shlex


def load_env_file(path: str | Path = ".env") -> None:
    """Load a small .env file without adding an extra runtime dependency."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if len(parsed) == 1 else value
        except ValueError:
            os.environ[key] = value.strip("\"'")


def _bool_from_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _owner_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    ids: set[int] = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if chunk:
            ids.add(int(chunk))
    return ids


def _path_from_env(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    mother_bot_token: str
    gemini_api_key: str
    gemini_model: str
    db_path: Path
    workdir: Path
    owner_ids: set[int]
    python_bin: str
    bwrap_bin: str
    require_bwrap: bool
    log_tail_rows: int = 2000
    log_level: str = "INFO"
    log_file: Path | None = None

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_env_file(env_file)
        base_dir = Path.cwd()
        return cls(
            mother_bot_token=os.getenv("MOTHER_BOT_TOKEN", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip(),
            db_path=_path_from_env(os.getenv("BOTMOTHER_DB", "./data/botmother.sqlite3"), base_dir),
            workdir=_path_from_env(os.getenv("BOTMOTHER_WORKDIR", "./data/bots"), base_dir),
            owner_ids=_owner_ids(os.getenv("OWNER_IDS")),
            python_bin=os.getenv("PYTHON_BIN", "python3").strip() or "python3",
            bwrap_bin=os.getenv("BWRAP_BIN", "bwrap").strip() or "bwrap",
            require_bwrap=_bool_from_env(os.getenv("BOTMOTHER_REQUIRE_BWRAP"), True),
            log_tail_rows=int(os.getenv("BOTMOTHER_LOG_TAIL_ROWS", "2000")),
            log_level=os.getenv("BOTMOTHER_LOG_LEVEL", "INFO").strip() or "INFO",
            log_file=_path_from_env(os.getenv("BOTMOTHER_LOG_FILE", "./data/botmother.log"), base_dir),
        )

    def validate_for_runtime(self) -> None:
        missing = []
        if not self.mother_bot_token:
            missing.append("MOTHER_BOT_TOKEN")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variables: {joined}")
