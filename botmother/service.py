from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .ai import GeminiCodeGenerator
from .code_tools import extract_python_code, validate_generated_code
from .config import Settings
from .db import Database
from .runner import ProcessManager
from .tokens import is_valid_telegram_token, mask_token


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str
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
            return OperationResult(False, "That does not look like a Telegram bot token.")
        if token == self.settings.mother_bot_token:
            return OperationResult(False, "That token belongs to the mother bot. Create a separate child bot in BotFather.")
        existing = self.db.get_bot_by_token(token)
        if existing is not None:
            return OperationResult(False, f"That token is already attached to bot #{existing['id']}.")

        name = prompt_to_name(prompt)
        placeholder = self.settings.workdir / "pending"
        bot_id = self.db.create_bot(user_id, chat_id, name, prompt, token, placeholder)
        bot_dir = self.settings.workdir / str(bot_id)
        self._set_workdir(bot_id, bot_dir)

        result = await self._generate_validate_and_save(bot_id, prompt)
        if not result.ok:
            return result

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(bot_id, "system", f"Launch failed: {exc}", self.settings.log_tail_rows)
            return OperationResult(False, f"Generated bot #{bot_id}, but launch failed: {exc}", bot_id)

        return OperationResult(True, f"Bot #{bot_id} is running. Token: {mask_token(token)}", bot_id)

    async def revise_bot(self, user_id: int, bot_id: int, prompt: str) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            return OperationResult(False, "Bot not found, or you do not have access.")

        was_running = bot_id in self.runner.active
        if was_running:
            await self.runner.stop_bot(bot_id, mark_stopped=False)
        self.db.update_bot_status(bot_id, "generating")
        self.db.update_bot_prompt(bot_id, prompt)

        result = await self._generate_validate_and_save(bot_id, prompt)
        if not result.ok:
            return result

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(bot_id, "system", f"Launch failed after revise: {exc}", self.settings.log_tail_rows)
            return OperationResult(False, f"Revision saved for bot #{bot_id}, but launch failed: {exc}", bot_id)

        return OperationResult(True, f"Bot #{bot_id} revised and running.", bot_id)

    async def stop_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            return OperationResult(False, "Bot not found, or you do not have access.")
        await self.runner.stop_bot(bot_id)
        return OperationResult(True, f"Bot #{bot_id} stopped.", bot_id)

    async def restart_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            return OperationResult(False, "Bot not found, or you do not have access.")
        try:
            await self.runner.restart_bot(bot_id)
        except Exception as exc:
            self.db.update_bot_status(bot_id, "launch_failed")
            return OperationResult(False, f"Restart failed for bot #{bot_id}: {exc}", bot_id)
        return OperationResult(True, f"Bot #{bot_id} restarted.", bot_id)

    async def delete_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            return OperationResult(False, "Bot not found, or you do not have access.")
        await self.runner.stop_bot(bot_id, mark_stopped=False)
        self.db.soft_delete_bot(bot_id)
        return OperationResult(True, f"Bot #{bot_id} deleted.", bot_id)

    async def kill_all(self, user_id: int) -> OperationResult:
        if not self.is_owner(user_id):
            return OperationResult(False, "Only owners can use /killall.")
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

    def _set_workdir(self, bot_id: int, bot_dir: Path) -> None:
        self.db.update_bot_workdir(bot_id, bot_dir)

    async def _generate_validate_and_save(self, bot_id: int, prompt: str) -> OperationResult:
        raw = self.generator.generate_code(prompt)
        code = extract_python_code(raw)
        validation = validate_generated_code(code)
        if not validation.ok:
            self.db.add_revision(bot_id, prompt, code, "failed", validation.error)
            self.db.update_bot_status(bot_id, "invalid")
            return OperationResult(False, f"Generated code for bot #{bot_id} was rejected: {validation.error}", bot_id)

        self.db.add_revision(bot_id, prompt, code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")
        return OperationResult(True, f"Generated valid code for bot #{bot_id}.", bot_id)
