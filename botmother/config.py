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


def _csv_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _openrouter_provider_names(value: str | None) -> tuple[str, ...]:
    names = _csv_values(value)
    canonical = {
        "deepseek": "DeepSeek",
        "novita": "Novita",
        "fireworks": "Fireworks",
        "siliconflow": "SiliconFlow",
    }
    return tuple(canonical.get(name.lower(), name) for name in names)


def _path_from_env(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    mother_bot_token: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_interaction_model: str
    openrouter_coding_model: str
    openrouter_base_url: str
    openrouter_app_name: str
    openrouter_app_url: str
    db_path: Path
    workdir: Path
    owner_ids: set[int]
    python_bin: str
    openrouter_interaction_max_tokens: int = 6000
    openrouter_coding_max_tokens: int = 24000
    openrouter_interaction_reasoning_effort: str = "minimal"
    openrouter_coding_reasoning_effort: str = "low"
    openrouter_exclude_reasoning: bool = True
    openrouter_request_timeout_seconds: int = 180
    openrouter_coding_timeout_seconds: float = 360.0
    openrouter_coding_provider_only: tuple[str, ...] = (
        "Novita",
        "Fireworks",
        "SiliconFlow",
    )
    credits_enabled: bool = True
    credits_initial_free: int = 50
    credit_cost_new_bot: int = 10
    credit_cost_edit: int = 3
    credit_cost_revise: int = 3
    credit_cost_autofix: int = 3
    credit_cost_ask: int = 1
    credit_runtime_seconds_per_credit: int = 86400
    credit_runtime_meter_interval_seconds: int = 300
    mgmt_owner_id: int | None = None
    log_tail_rows: int = 2000
    log_level: str = "INFO"
    log_file: Path | None = None

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_env_file(env_file)
        base_dir = Path.cwd()
        return cls(
            mother_bot_token=os.getenv("MOTHER_BOT_TOKEN", "").strip(),
            openrouter_api_key=(
                os.getenv("OPENROUTER_API_KEY", "").strip()
                or os.getenv("GEMINI_API_KEY", "").strip()
            ),
            openrouter_model=(
                os.getenv("OPENROUTER_MODEL", "").strip()
                or os.getenv("GEMINI_MODEL", "").strip()
                or "google/gemini-2.5-flash-lite"
            ),
            openrouter_interaction_model=(
                os.getenv("OPENROUTER_INTERACTION_MODEL", "").strip()
                or os.getenv("OPENROUTER_MODEL", "").strip()
                or "google/gemini-2.5-flash"
            ),
            openrouter_coding_model=(
                os.getenv("OPENROUTER_CODING_MODEL", "").strip()
                or os.getenv("OPENROUTER_MODEL", "").strip()
                or "deepseek/deepseek-v4-pro"
            ),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).strip() or "https://openrouter.ai/api/v1",
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "BotMother").strip()
            or "BotMother",
            openrouter_app_url=os.getenv("OPENROUTER_APP_URL", "").strip(),
            db_path=_path_from_env(os.getenv("BOTMOTHER_DB", "./data/botmother.sqlite3"), base_dir),
            workdir=_path_from_env(os.getenv("BOTMOTHER_WORKDIR", "./data/bots"), base_dir),
            owner_ids=_owner_ids(os.getenv("OWNER_IDS")),
            python_bin=os.getenv("PYTHON_BIN", "python3").strip() or "python3",
            openrouter_interaction_max_tokens=int(
                os.getenv("OPENROUTER_INTERACTION_MAX_TOKENS", "6000")
            ),
            openrouter_coding_max_tokens=int(
                os.getenv("OPENROUTER_CODING_MAX_TOKENS", "24000")
            ),
            openrouter_interaction_reasoning_effort=os.getenv(
                "OPENROUTER_INTERACTION_REASONING_EFFORT", "minimal"
            ).strip(),
            openrouter_coding_reasoning_effort=os.getenv(
                "OPENROUTER_CODING_REASONING_EFFORT", "low"
            ).strip(),
            openrouter_exclude_reasoning=_bool_from_env(
                os.getenv("OPENROUTER_EXCLUDE_REASONING"), True
            ),
            openrouter_request_timeout_seconds=int(
                os.getenv("OPENROUTER_REQUEST_TIMEOUT_SECONDS", "180")
            ),
            openrouter_coding_timeout_seconds=float(
                os.getenv("OPENROUTER_CODING_TIMEOUT_SECONDS", "360")
            ),
            openrouter_coding_provider_only=_openrouter_provider_names(
                os.getenv(
                    "OPENROUTER_CODING_PROVIDER_ONLY",
                    "Novita,Fireworks,SiliconFlow",
                )
            ),
            credits_enabled=_bool_from_env(os.getenv("CREDITS_ENABLED"), True),
            credits_initial_free=int(os.getenv("CREDITS_INITIAL_FREE", "50")),
            credit_cost_new_bot=int(os.getenv("CREDIT_COST_NEW_BOT", "10")),
            credit_cost_edit=int(os.getenv("CREDIT_COST_EDIT", "3")),
            credit_cost_revise=int(os.getenv("CREDIT_COST_REVISE", "3")),
            credit_cost_autofix=int(os.getenv("CREDIT_COST_AUTOFIX", "3")),
            credit_cost_ask=int(os.getenv("CREDIT_COST_ASK", "1")),
            credit_runtime_seconds_per_credit=int(
                os.getenv("CREDIT_RUNTIME_SECONDS_PER_CREDIT", "86400")
            ),
            credit_runtime_meter_interval_seconds=int(
                os.getenv("CREDIT_RUNTIME_METER_INTERVAL_SECONDS", "300")
            ),
            mgmt_owner_id=(
                int(os.getenv("MGMT_OWNER_ID", "").strip())
                if os.getenv("MGMT_OWNER_ID", "").strip().isdigit()
                else None
            ),
            log_tail_rows=int(os.getenv("BOTMOTHER_LOG_TAIL_ROWS", "2000")),
            log_level=os.getenv("BOTMOTHER_LOG_LEVEL", "INFO").strip() or "INFO",
            log_file=_path_from_env(os.getenv("BOTMOTHER_LOG_FILE", "./data/botmother.log"), base_dir),
        )

    def validate_for_runtime(self) -> None:
        missing = []
        if not self.mother_bot_token:
            missing.append("MOTHER_BOT_TOKEN")
        if not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variables: {joined}")
