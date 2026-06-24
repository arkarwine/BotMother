from __future__ import annotations

import logging

from .ai import GeminiCodeGenerator
from .config import Settings
from .db import Database
from .handlers import build_application
from .logging_config import setup_logging
from .runner import ProcessManager
from .service import BotService


logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings.from_env()
    log_file = setup_logging(settings)
    settings.validate_for_runtime()
    settings.workdir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Starting BotMother: db=%s workdir=%s model=%s require_bwrap=%s log_file=%s",
        settings.db_path,
        settings.workdir,
        settings.gemini_model,
        settings.require_bwrap,
        log_file,
    )

    db = Database(settings.db_path)
    db.initialize()
    logger.info("Database initialized")

    generator = GeminiCodeGenerator(api_key=settings.gemini_api_key, model=settings.gemini_model)
    runner = ProcessManager(settings=settings, db=db)
    service = BotService(settings=settings, db=db, generator=generator, runner=runner)
    application = build_application(settings.mother_bot_token, db, service)
    logger.info("Telegram polling starting")
    application.run_polling()
