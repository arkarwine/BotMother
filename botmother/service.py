from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from .ai import GeminiCodeGenerator
from .code_tools import extract_python_code, validate_generated_code
from .config import Settings
from .db import Database
from .runner import ProcessManager
from .tokens import is_valid_telegram_token, mask_token


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str
    bot_id: int | None = None


@dataclass(frozen=True)
class SourceResult:
    ok: bool
    message: str
    code: str | None = None
    bot_id: int | None = None


def prompt_to_name(prompt: str) -> str:
    name = " ".join(prompt.strip().split())
    if not name:
        return "Untitled bot"
    return name[:48]


class BotService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        generator: GeminiCodeGenerator,
        runner: ProcessManager,
    ) -> None:
        self.settings = settings
        self.db = db
        self.generator = generator
        self.runner = runner

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids

    def can_manage(self, user_id: int, bot_id: int) -> bool:
        bot = self.db.get_bot(bot_id)
        if bot is None:
            return False
        return self.is_owner(user_id) or int(bot["owner_user_id"]) == user_id

    async def create_bot(self, user_id: int, chat_id: int, prompt: str, token: str) -> OperationResult:
        token = token.strip()
        if not is_valid_telegram_token(token):
            logger.info("Rejected invalid child token: user_id=%s chat_id=%s", user_id, chat_id)
            return OperationResult(False, "That does not look like a Telegram bot token.")
        if token == self.settings.mother_bot_token:
            logger.warning("Rejected mother token as child token: user_id=%s chat_id=%s", user_id, chat_id)
            return OperationResult(False, "That token belongs to the mother bot. Create a separate child bot in BotFather.")

        released = self.db.release_deleted_token(token)
        if released:
            logger.info("Released token from deleted bot record: user_id=%s count=%s", user_id, released)

        existing = self.db.get_bot_by_token(token)
        if existing is not None:
            logger.info("Rejected duplicate child token: user_id=%s existing_bot_id=%s", user_id, existing["id"])
            if self.is_owner(user_id) or int(existing["owner_user_id"]) == user_id:
                return OperationResult(False, f"That token is already attached to bot #{existing['id']}.")
            return OperationResult(False, "That token is already attached to another active bot.")

        name = prompt_to_name(prompt)
        placeholder = self.settings.workdir / "pending"
        bot_id = self.db.create_bot(user_id, chat_id, name, prompt, token, placeholder)
        bot_dir = self.settings.workdir / str(bot_id)
        self._set_workdir(bot_id, bot_dir)
        logger.info("Created bot record: bot_id=%s user_id=%s chat_id=%s name=%r", bot_id, user_id, chat_id, name)

        result = await self._generate_validate_and_save(bot_id, prompt)
        if not result.ok:
            logger.warning("Bot generation rejected: bot_id=%s message=%s", bot_id, result.message)
            return result

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(bot_id, "system", f"Launch failed: {exc}", self.settings.log_tail_rows)
            return OperationResult(False, f"Generated bot #{bot_id}, but launch failed: {exc}", bot_id)

        logger.info("Bot running: bot_id=%s user_id=%s", bot_id, user_id)
        return OperationResult(True, f"Bot #{bot_id} is running. Token: {mask_token(token)}", bot_id)

    async def revise_bot(self, user_id: int, bot_id: int, prompt: str) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied revise: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "Bot not found, or you do not have access.")

        was_running = bot_id in self.runner.active
        logger.info("Revising bot: bot_id=%s user_id=%s was_running=%s", bot_id, user_id, was_running)
        if was_running:
            await self.runner.stop_bot(bot_id, mark_stopped=False)
        self.db.update_bot_status(bot_id, "generating")
        self.db.update_bot_prompt(bot_id, prompt)

        result = await self._generate_validate_and_save(bot_id, prompt)
        if not result.ok:
            logger.warning("Bot revision rejected: bot_id=%s message=%s", bot_id, result.message)
            return result

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed after revise: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(bot_id, "system", f"Launch failed after revise: {exc}", self.settings.log_tail_rows)
            return OperationResult(False, f"Revision saved for bot #{bot_id}, but launch failed: {exc}", bot_id)

        logger.info("Bot revised and running: bot_id=%s user_id=%s", bot_id, user_id)
        return OperationResult(True, f"Bot #{bot_id} revised and running.", bot_id)

    async def edit_bot_code(self, user_id: int, bot_id: int, raw_code: str) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied edit: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "Bot not found, or you do not have access.")

        code = extract_python_code(raw_code)
        validation = validate_generated_code(code)
        if not validation.ok:
            logger.warning("Manual edit failed validation: bot_id=%s error=%s", bot_id, validation.error)
            self.db.add_revision(bot_id, "Manual edit", code, "failed", validation.error)
            return OperationResult(False, f"Edited code for bot #{bot_id} was rejected: {validation.error}", bot_id)

        was_running = bot_id in self.runner.active
        logger.info("Applying manual edit: bot_id=%s user_id=%s was_running=%s code_chars=%s", bot_id, user_id, was_running, len(code))
        if was_running:
            await self.runner.stop_bot(bot_id, mark_stopped=False)

        self.db.add_revision(bot_id, "Manual edit", code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")
        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed after manual edit: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(bot_id, "system", f"Launch failed after manual edit: {exc}", self.settings.log_tail_rows)
            return OperationResult(False, f"Manual edit saved for bot #{bot_id}, but launch failed: {exc}", bot_id)

        return OperationResult(True, f"Bot #{bot_id} edited and running.", bot_id)

    async def stop_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied stop: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "Bot not found, or you do not have access.")
        logger.info("Stopping bot by request: bot_id=%s user_id=%s", bot_id, user_id)
        await self.runner.stop_bot(bot_id)
        return OperationResult(True, f"Bot #{bot_id} stopped.", bot_id)

    async def restart_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied restart: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "Bot not found, or you do not have access.")
        try:
            logger.info("Restarting bot by request: bot_id=%s user_id=%s", bot_id, user_id)
            await self.runner.restart_bot(bot_id)
        except Exception as exc:
            logger.exception("Restart failed: bot_id=%s user_id=%s", bot_id, user_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            return OperationResult(False, f"Restart failed for bot #{bot_id}: {exc}", bot_id)
        return OperationResult(True, f"Bot #{bot_id} restarted.", bot_id)

    async def delete_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied delete: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "Bot not found, or you do not have access.")
        logger.info("Deleting bot by request: bot_id=%s user_id=%s", bot_id, user_id)
        await self.runner.stop_bot(bot_id, mark_stopped=False)
        self.db.soft_delete_bot(bot_id)
        return OperationResult(True, f"Bot #{bot_id} deleted.", bot_id)

    async def kill_all(self, user_id: int) -> OperationResult:
        if not self.is_owner(user_id):
            logger.warning("Denied killall for non-owner: user_id=%s", user_id)
            return OperationResult(False, "Only owners can use /killall.")
        logger.warning("Owner requested killall: user_id=%s active_count=%s", user_id, len(self.runner.active))
        await self.runner.stop_all(mark_stopped=True)
        return OperationResult(True, "All active child bots were stopped.")

    def list_bots_for(self, user_id: int):
        if self.is_owner(user_id):
            return self.db.list_bots()
        return self.db.list_bots(owner_user_id=user_id)

    def get_accessible_bot(self, user_id: int, bot_id: int):
        if not self.can_manage(user_id, bot_id):
            return None
        return self.db.get_bot(bot_id)

    def get_source(self, user_id: int, bot_id: int) -> SourceResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied source: user_id=%s bot_id=%s", user_id, bot_id)
            return SourceResult(False, "Bot not found, or you do not have access.", bot_id=bot_id)
        revision = self.db.latest_revision(bot_id)
        if revision is None or not revision["code"]:
            return SourceResult(False, f"Bot #{bot_id} has no saved source yet.", bot_id=bot_id)
        return SourceResult(True, f"Source for bot #{bot_id}.", revision["code"], bot_id)

    def _set_workdir(self, bot_id: int, bot_dir: Path) -> None:
        self.db.update_bot_workdir(bot_id, bot_dir)

    async def _generate_validate_and_save(self, bot_id: int, prompt: str) -> OperationResult:
        logger.info("Generating revision: bot_id=%s prompt_chars=%s", bot_id, len(prompt))
        raw = self.generator.generate_code(prompt)
        code = extract_python_code(raw)
        validation = validate_generated_code(code)
        if not validation.ok:
            logger.warning("Generated code failed validation: bot_id=%s error=%s", bot_id, validation.error)
            self.db.add_revision(bot_id, prompt, code, "failed", validation.error)
            self.db.update_bot_status(bot_id, "invalid")
            return OperationResult(False, f"Generated code for bot #{bot_id} was rejected: {validation.error}", bot_id)

        self.db.add_revision(bot_id, prompt, code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")
        logger.info("Generated code validated: bot_id=%s code_chars=%s", bot_id, len(code))
        return OperationResult(True, f"Generated valid code for bot #{bot_id}.", bot_id)
