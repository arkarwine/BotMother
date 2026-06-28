from __future__ import annotations

import logging

from .ai import OpenRouterCodeGenerator
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
        "Starting BotMother: db=%s workdir=%s interaction_model=%s coding_model=%s require_bwrap=%s log_file=%s",
        settings.db_path,
        settings.workdir,
        settings.openrouter_interaction_model,
        settings.openrouter_coding_model,
        settings.require_bwrap,
        log_file,
    )

    db = Database(settings.db_path)
    db.initialize()
    logger.info("Database initialized")

    generator = OpenRouterCodeGenerator(
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_model,
        interaction_model=settings.openrouter_interaction_model,
        coding_model=settings.openrouter_coding_model,
        base_url=settings.openrouter_base_url,
        app_name=settings.openrouter_app_name,
        app_url=settings.openrouter_app_url,
    )
    runner = ProcessManager(settings=settings, db=db)
    service = BotService(settings=settings, db=db, generator=generator, runner=runner)
    application = build_application(settings.mother_bot_token, db, service)
    logger.info("Telegram polling starting")
    application.run_polling()
