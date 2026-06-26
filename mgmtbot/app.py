from __future__ import annotations

import logging

from .config import MgmtSettings
from .db import MgmtDatabase
from .handlers import build_application

logger = logging.getLogger(__name__)


def setup_logging(settings: MgmtSettings) -> str | None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_path = None

    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(settings.log_file, encoding="utf-8")
        handlers.append(fh)
        log_path = str(settings.log_file)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )
    return log_path


def main() -> None:
    settings = MgmtSettings.from_env()
    log_file = setup_logging(settings)
    settings.validate_for_runtime()

    logger.info(
        "Starting BotMother Management Bot: db=%s owner_id=%s log_file=%s",
        settings.db_path,
        settings.owner_id,
        log_file,
    )

    db = MgmtDatabase(settings.db_path)
    db.initialize()
    logger.info("Management DB tables initialized")

    application = build_application(settings, db)
    logger.info("Management bot polling starting")
    application.run_polling()
