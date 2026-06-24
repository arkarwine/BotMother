from __future__ import annotations

from .ai import GeminiCodeGenerator
from .config import Settings
from .db import Database
from .handlers import build_application
from .runner import ProcessManager
from .service import BotService


def main() -> None:
    settings = Settings.from_env()
    settings.validate_for_runtime()
    settings.workdir.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    db.initialize()

    generator = GeminiCodeGenerator(api_key=settings.gemini_api_key, model=settings.gemini_model)
    runner = ProcessManager(settings=settings, db=db)
    service = BotService(settings=settings, db=db, generator=generator, runner=runner)
    application = build_application(settings.mother_bot_token, db, service)
    application.run_polling()

