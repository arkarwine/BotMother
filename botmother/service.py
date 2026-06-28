from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .ai import (
    AI_REFINEMENT_LAYERS,
    AIDecision,
    AIQuestion,
    AIReadinessDecision,
    OpenRouterCodeGenerator,
)
from .code_tools import (
    extract_python_code,
    validate_generated_code,
    validate_generated_code_report,
)
from .config import Settings
from .credits import (
    ACTION_ASK,
    ACTION_AUTOFIX,
    ACTION_EDIT,
    ACTION_LABELS,
    ACTION_NEW_BOT,
    ACTION_REVISE,
    CreditGateResult,
)
from .db import Database
from .runner import ProcessManager
from .tokens import is_valid_telegram_token, mask_token, redact_telegram_tokens

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


@dataclass(frozen=True)
class AskResult:
    ok: bool
    message: str
    bot_id: int | None = None


@dataclass(frozen=True)
class DashboardResult:
    ok: bool
    message: str
    bot_id: int | None = None


def prompt_to_name(prompt: str) -> str:
    name = " ".join(prompt.strip().split())
    if not name:
        return "Untitled bot"
    return name[:48]


async def fetch_bot_username(token: str) -> str | None:
    try:
        from telegram import Bot
    except ImportError:
        return None

    bot = Bot(token)
    try:
        me = await bot.get_me()
        username = str(getattr(me, "username", "") or "").strip().lstrip("@")
        return username or None
    except Exception:
        logger.exception("Failed to fetch child bot username from Telegram")
        return None
    finally:
        shutdown = getattr(bot, "shutdown", None)
        if callable(shutdown):
            try:
                await shutdown()
            except Exception:
                logger.debug("Ignoring child bot shutdown error after get_me")


class BotService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        generator: OpenRouterCodeGenerator,
        runner: ProcessManager,
    ) -> None:
        self.settings = settings
        self.db = db
        self.generator = generator
        self.runner = runner

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids

    def is_credit_exempt(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids or user_id == self.settings.mgmt_owner_id

    def credit_cost(self, action: str) -> int:
        costs = {
            ACTION_NEW_BOT: self.settings.credit_cost_new_bot,
            ACTION_EDIT: self.settings.credit_cost_edit,
            ACTION_REVISE: self.settings.credit_cost_revise,
            ACTION_AUTOFIX: self.settings.credit_cost_autofix,
            ACTION_ASK: self.settings.credit_cost_ask,
        }
        return max(0, int(costs.get(action, 0)))

    def credit_balance(self, user_id: int) -> int | None:
        if not self.settings.credits_enabled or self.is_credit_exempt(user_id):
            return None
        return self.db.credit_balance(user_id, self.settings.credits_initial_free)

    def reserve_paid_action(
        self,
        user_id: int,
        action: str,
        bot_id: int | None = None,
        note: str | None = None,
    ) -> CreditGateResult:
        cost = self.credit_cost(action)
        label = ACTION_LABELS.get(action, action)
        if not self.settings.credits_enabled or self.is_credit_exempt(user_id) or cost <= 0:
            return CreditGateResult(True, action, cost, exempt=True)
        reservation_id, balance = self.db.reserve_credits(
            user_id,
            cost,
            action,
            self.settings.credits_initial_free,
            bot_id=bot_id,
            note=note or label,
        )
        if reservation_id is None:
            return CreditGateResult(
                False,
                action,
                cost,
                balance=balance,
                message=(
                    f"💳 Not enough credits\n\n"
                    f"{label} costs {cost} credits.\n"
                    f"Your balance is {balance} credits.\n\n"
                    "Please ask an admin to add credits."
                ),
            )
        return CreditGateResult(
            True,
            action,
            cost,
            balance=balance,
            reservation_id=reservation_id,
        )

    def settle_paid_action(
        self, reservation_id: int | None, bot_id: int | None = None, note: str | None = None
    ) -> None:
        self.db.settle_credit_reservation(reservation_id, bot_id=bot_id, note=note)

    def refund_paid_action(self, reservation_id: int | None, note: str | None = None) -> None:
        self.db.refund_credit_reservation(reservation_id, note=note)

    def credit_summary_for_user(self, user_id: int) -> str:
        if not self.settings.credits_enabled:
            return "Credits are disabled."
        if self.is_credit_exempt(user_id):
            return "Credit-exempt owner account."
        balance = self.db.credit_balance(user_id, self.settings.credits_initial_free)
        return f"Balance: {balance} credits"

    def can_manage(self, user_id: int, bot_id: int) -> bool:
        bot = self.db.get_bot(bot_id)
        if bot is None:
            return False
        return self.is_owner(user_id) or int(bot["owner_user_id"]) == user_id

    def plan_new_bot(
        self,
        prompt: str,
        answer_history: list[dict],
        force_code: bool = False,
        user_context: str = "",
    ) -> AIDecision:
        return self.generator.decide_new_bot(
            prompt,
            answer_history,
            force_code=force_code,
            user_context=user_context,
        )

    def check_new_bot_readiness(
        self,
        prompt: str,
        answer_history: list[dict],
        decision: AIDecision,
        user_context: str = "",
    ) -> AIReadinessDecision:
        try:
            return self.generator.check_new_bot_readiness(
                prompt, answer_history, decision, user_context=user_context
            )
        except Exception as exc:
            logger.exception("Readiness check failed")
            return AIReadinessDecision(
                "questions",
                "I could not complete the final launch-data check. Please confirm any required admin IDs, API keys, payment/contact details, or external service settings before we continue.",
                (
                    AIQuestion(
                        "confirm_required_runtime_data",
                        "What required admin IDs, API keys, payment/contact details, or external service settings should this bot use?",
                        ("No extra settings are needed", "I will provide them here"),
                    ),
                ),
            )

    def plan_edit_bot(
        self,
        user_id: int,
        bot_id: int,
        edit_prompt: str,
        answer_history: list[dict],
        force_code: bool = False,
        user_context: str = "",
    ) -> AIDecision | OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied edit planning: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "Bot not found, or you do not have access.", bot_id
            )
        existing_revision = self.db.latest_revision(bot_id)
        if existing_revision is None or not existing_revision["code"]:
            return OperationResult(
                False, "This bot has no saved source to edit.", bot_id
            )
        return self.generator.decide_edit(
            existing_revision["code"],
            edit_prompt,
            answer_history,
            force_code=force_code,
            user_context=user_context,
        )

    async def create_bot(
        self,
        user_id: int,
        chat_id: int,
        prompt: str,
        token: str,
        user_context: str = "",
    ) -> OperationResult:
        coding_brief = self.generator.build_coding_brief(prompt, user_context=user_context)
        raw = self.generator.generate_code(coding_brief)
        return await self.create_bot_from_code(
            user_id,
            chat_id,
            prompt,
            token,
            raw,
            coding_context=coding_brief,
            user_context=user_context,
        )

    async def create_bot_from_decision(
        self,
        user_id: int,
        chat_id: int,
        prompt: str,
        token: str,
        decision: AIDecision,
        user_context: str = "",
    ) -> OperationResult:
        if decision.type != "code" or not decision.code:
            return OperationResult(False, "AI did not provide code yet.")
        coding_brief = decision.code
        raw_code = self.generator.generate_code(coding_brief)
        return await self.create_bot_from_code(
            user_id,
            chat_id,
            prompt,
            token,
            raw_code,
            self._env_dict(decision),
            coding_context=coding_brief,
            user_context=user_context,
        )

    async def create_bot_from_code(
        self,
        user_id: int,
        chat_id: int,
        prompt: str,
        token: str,
        raw_code: str,
        env_vars: dict[str, str] | None = None,
        coding_context: str | None = None,
        user_context: str = "",
    ) -> OperationResult:
        token = token.strip()
        if not is_valid_telegram_token(token):
            logger.info(
                "Rejected invalid child token: user_id=%s chat_id=%s", user_id, chat_id
            )
            return OperationResult(
                False,
                "🔐 Invalid child token\n\nPaste the child token from @BotFather.\n\nExample: 123456789:AA....",
            )
        if token == self.settings.mother_bot_token:
            logger.warning(
                "Rejected mother token as child token: user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )
            return OperationResult(
                False,
                "🔐 Mother token rejected\n\nCreate a separate child bot in @BotFather, then paste that child token here.",
            )

        released = self.db.release_deleted_token(token)
        if released:
            logger.info(
                "Released token from deleted bot record: user_id=%s count=%s",
                user_id,
                released,
            )

        existing = self.db.get_bot_by_token(token)
        if existing is not None:
            logger.info(
                "Rejected duplicate child token: user_id=%s existing_bot_id=%s",
                user_id,
                existing["id"],
            )
            if self.is_owner(user_id) or int(existing["owner_user_id"]) == user_id:
                return OperationResult(
                    False,
                    f"🔁 Token already attached\n\nThis token belongs to {existing['name']}.\n\nOpen My Bots to manage it.",
                )
            return OperationResult(
                False,
                "🔁 Token already attached\n\nThat token is already attached to another active bot.",
            )

        raw_code = self._refine_code_for_deploy(
            coding_context or prompt,
            raw_code,
            env_vars or {},
        )
        code = extract_python_code(raw_code)
        validation = validate_generated_code(code)
        if not validation.ok:
            logger.warning(
                "Generated code failed validation before create: user_id=%s error=%s",
                user_id,
                validation.error,
            )
            return OperationResult(
                False, f"Generated code was rejected: {validation.error}"
            )

        bot_username = await fetch_bot_username(token)
        name = prompt_to_name(prompt)
        placeholder = self.settings.workdir / "pending"
        bot_id = self.db.create_bot(user_id, chat_id, name, prompt, token, placeholder)
        bot_dir = self.settings.workdir / str(bot_id)
        self._set_workdir(bot_id, bot_dir)
        if bot_username:
            self.db.update_bot_username(bot_id, bot_username)
        logger.info(
            "Created bot record: bot_id=%s user_id=%s chat_id=%s name=%r bot_username=%r",
            bot_id,
            user_id,
            chat_id,
            name,
            bot_username,
        )

        if env_vars:
            self.db.set_bot_env_vars(bot_id, env_vars)
            logger.info(
                "Stored child env vars: bot_id=%s count=%s", bot_id, len(env_vars)
            )
        self.db.add_revision(bot_id, prompt, code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(
                bot_id, "system", f"Launch failed: {exc}", self.settings.log_tail_rows
            )
            return OperationResult(
                False,
                f"⚠️ Launch failed\n\n{name} was generated, but it could not start.\n\nError: {exc}\n\nOpen Logs for details.",
                bot_id,
            )

        logger.info("Bot running: bot_id=%s user_id=%s", bot_id, user_id)
        username = f"@{bot_username}" if bot_username else "Not available yet"
        env_names = ", ".join(sorted((env_vars or {}).keys())) or "None"
        return OperationResult(
            True,
            (
                f"✅ Setup complete\n\n"
                f"Name\n{name}\n\n"
                f"Bot username\n{username}\n\n"
                f"Status\nrunning\n\n"
                f"Token\n{mask_token(token)}\n\n"
                f"Extra env vars\n{env_names}\n\n"
                "Use the action buttons below to inspect, ask, edit, or manage it."
            ),
            bot_id,
        )

    async def revise_bot(
        self, user_id: int, bot_id: int, prompt: str, user_context: str = ""
    ) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied revise: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "🔎 Bot not found, or you do not have access."
            )

        gate = self.reserve_paid_action(user_id, ACTION_REVISE, bot_id=bot_id)
        if not gate.ok:
            return OperationResult(False, gate.message, bot_id)
        reservation_id = gate.reservation_id

        was_running = bot_id in self.runner.active
        logger.info(
            "Revising bot: bot_id=%s user_id=%s was_running=%s",
            bot_id,
            user_id,
            was_running,
        )
        if was_running:
            await self.runner.stop_bot(bot_id, mark_stopped=False)
        self.db.update_bot_status(bot_id, "generating")
        self.db.update_bot_prompt(bot_id, prompt)

        result = await self._generate_validate_and_save(
            bot_id, prompt, user_context=user_context
        )
        if not result.ok:
            self.refund_paid_action(reservation_id, "Revision did not produce valid code")
            logger.warning(
                "Bot revision rejected: bot_id=%s message=%s", bot_id, result.message
            )
            return result

        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed after revise: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(
                bot_id,
                "system",
                f"Launch failed after revise: {exc}",
                self.settings.log_tail_rows,
            )
            self.settle_paid_action(reservation_id, bot_id=bot_id, note="Revision saved; launch failed")
            bot = self.db.get_bot(bot_id)
            name = bot["name"] if bot is not None else "The bot"
            return OperationResult(
                False,
                f"⚠️ Revision saved, launch failed\n\n{name} was revised, but it could not start.\n\nError: {exc}\n\nOpen Logs for details.",
                bot_id,
            )

        logger.info("Bot revised and running: bot_id=%s user_id=%s", bot_id, user_id)
        self.settle_paid_action(reservation_id, bot_id=bot_id, note="Revision saved")
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        return OperationResult(
            True,
            f"✅ {name} revised and running\n\nUse the keyboard to view logs, status, or ask what changed.",
            bot_id,
        )

    async def edit_bot_with_prompt(
        self, user_id: int, bot_id: int, edit_prompt: str
    ) -> OperationResult:
        plan = self.plan_edit_bot(user_id, bot_id, edit_prompt, [], force_code=True)
        if isinstance(plan, OperationResult):
            return plan
        return await self.edit_bot_from_decision(user_id, bot_id, edit_prompt, plan)

    async def edit_bot_from_decision(
        self,
        user_id: int,
        bot_id: int,
        edit_prompt: str,
        decision: AIDecision,
        user_context: str = "",
    ) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied edit: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "🔎 Bot not found, or you do not have access."
            )

        if decision.type != "code" or not decision.code:
            return OperationResult(
                False, "🧠 AI did not provide edited code yet.", bot_id
            )

        env_vars = self._env_dict(decision)
        existing_env_vars = self.db.get_bot_env_vars(bot_id)
        refinement_env_vars = {**existing_env_vars, **env_vars}
        latest_revision = self.db.latest_revision(bot_id)
        current_code = str(latest_revision["code"]) if latest_revision is not None else ""
        generated_code = self.generator.edit_code(
            current_code,
            decision.code,
        )
        raw_code = self._refine_code_for_deploy(
            decision.code,
            generated_code,
            refinement_env_vars,
        )
        code = extract_python_code(raw_code)
        validation = validate_generated_code(code)
        if not validation.ok:
            logger.warning(
                "Prompt edit failed validation: bot_id=%s error=%s",
                bot_id,
                validation.error,
            )
            self.db.add_revision(
                bot_id, f"Edit: {edit_prompt}", code, "failed", validation.error
            )
            return OperationResult(
                False,
                f"⚠️ Edit rejected\n\n{validation.error}\n\nThe running bot was left unchanged.",
                bot_id,
            )

        was_running = bot_id in self.runner.active
        logger.info(
            "Applying prompt edit: bot_id=%s user_id=%s was_running=%s code_chars=%s",
            bot_id,
            user_id,
            was_running,
            len(code),
        )
        if was_running:
            await self.runner.stop_bot(bot_id, mark_stopped=False)

        if env_vars:
            self.db.set_bot_env_vars(bot_id, env_vars)
            logger.info(
                "Stored child env vars after edit: bot_id=%s count=%s",
                bot_id,
                len(env_vars),
            )
        self.db.add_revision(bot_id, f"Edit: {edit_prompt}", code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")
        try:
            await self.runner.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Launch failed after prompt edit: bot_id=%s", bot_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            self.db.add_log(
                bot_id,
                "system",
                f"Launch failed after prompt edit: {exc}",
                self.settings.log_tail_rows,
            )
            bot = self.db.get_bot(bot_id)
            name = bot["name"] if bot is not None else "The bot"
            return OperationResult(
                False,
                f"⚠️ Edit saved, launch failed\n\n{name} was edited, but it could not start.\n\nError: {exc}\n\nOpen Logs for details.",
                bot_id,
            )

        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        return OperationResult(
            True,
            f"✅ {name} edited and running\n\nUse the keyboard to view logs, status, or ask what changed.",
            bot_id,
        )

    async def stop_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied stop: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "🔎 Bot not found, or you do not have access."
            )
        logger.info("Stopping bot by request: bot_id=%s user_id=%s", bot_id, user_id)
        await self.runner.stop_bot(bot_id)
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        return OperationResult(
            True,
            f"🛑 {name} stopped\n\nUse Restart when you want to run it again.",
            bot_id,
        )

    async def restart_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied restart: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "🔎 Bot not found, or you do not have access."
            )
        try:
            logger.info(
                "Restarting bot by request: bot_id=%s user_id=%s", bot_id, user_id
            )
            await self.runner.restart_bot(bot_id)
        except Exception as exc:
            logger.exception("Restart failed: bot_id=%s user_id=%s", bot_id, user_id)
            self.db.update_bot_status(bot_id, "launch_failed")
            bot = self.db.get_bot(bot_id)
            name = bot["name"] if bot is not None else "The bot"
            return OperationResult(
                False,
                f"⚠️ Restart failed\n\n{name} could not start.\n\nError: {exc}\n\nOpen Logs for details.",
                bot_id,
            )
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        return OperationResult(
            True,
            f"🔄 {name} restarted\n\nOpen Logs if you want to watch startup output.",
            bot_id,
        )

    async def delete_bot(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied delete: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(
                False, "🔎 Bot not found, or you do not have access."
            )
        logger.info("Deleting bot by request: bot_id=%s user_id=%s", bot_id, user_id)
        await self.runner.stop_bot(bot_id, mark_stopped=False)
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        self.db.soft_delete_bot(bot_id)
        return OperationResult(
            True,
            f"🗑️ {name} deleted\n\nIts token can now be reused for a new bot.",
            bot_id,
        )

    async def kill_all(self, user_id: int) -> OperationResult:
        if not self.is_owner(user_id):
            logger.warning("Denied killall for non-owner: user_id=%s", user_id)
            return OperationResult(False, "🔒 Only owners can use /killall.")
        logger.warning(
            "Owner requested killall: user_id=%s active_count=%s",
            user_id,
            len(self.runner.active),
        )
        await self.runner.stop_all(mark_stopped=True)
        return OperationResult(True, "🛑 All active child bots were stopped.")

    async def refresh_missing_bot_usernames_for(self, user_id: int) -> None:
        rows = self.list_bots_for(user_id)
        for row in rows:
            if row["deleted_at"] is not None:
                continue
            existing_username = str(row["bot_username"] or "").strip()
            if existing_username:
                continue
            fetched_username = await fetch_bot_username(str(row["token"]))
            if fetched_username:
                self.db.update_bot_username(int(row["id"]), fetched_username)

    def list_bots_for(self, user_id: int):
        if self.is_owner(user_id):
            return self.db.list_bots()
        return self.db.list_bots(owner_user_id=user_id)

    def get_accessible_bot(self, user_id: int, bot_id: int):
        if not self.can_manage(user_id, bot_id):
            return None
        return self.db.get_bot(bot_id)

    def bot_dashboard(self, user_id: int, bot_id: int) -> DashboardResult:
        if not self.can_manage(user_id, bot_id):
            return DashboardResult(False, "🔎 Bot not found, or you do not have access.", bot_id)
        bot = self.db.get_bot(bot_id)
        if bot is None:
            return DashboardResult(False, "🔎 Bot not found, or you do not have access.", bot_id)
        revision = self.db.latest_revision(bot_id)
        env_names = sorted(self.db.get_bot_env_vars(bot_id))
        recent_logs = self.db.get_logs(bot_id, 5)
        error_lines = [
            str(row["line"]).replace("\n", " ")
            for row in recent_logs
            if str(row["stream"]).lower() in {"stderr", "system"}
        ]
        validation = self._validation_report_text(revision["code"] if revision is not None else "")
        username = f"@{bot['bot_username']}" if bot["bot_username"] else "Not available yet"
        pid = str(bot["pid"] or "none")
        process_state = "active" if bot_id in self.runner.active else "not active"
        message = (
            f"📦 {bot['name']}\n\n"
            f"Bot username\n{username}\n\n"
            f"Status\n{bot['status']}\n\n"
            f"Process\n{process_state}, pid {pid}\n\n"
            f"Owner\n{bot['owner_username'] or bot['owner_first_name'] or bot['owner_user_id']}\n\n"
            f"Revisions\n{self.db.count_revisions(bot_id)}\n\n"
            f"Extra env vars\n{', '.join(env_names) if env_names else 'None'}\n\n"
            f"Validation\n{validation}\n\n"
            f"Latest issue\n{error_lines[-1] if error_lines else 'No recent errors.'}"
        )
        return DashboardResult(True, message, bot_id)

    def validation_report(self, user_id: int, bot_id: int) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            return OperationResult(False, "🔎 Bot not found, or you do not have access.", bot_id)
        revision = self.db.latest_revision(bot_id)
        if revision is None or not revision["code"]:
            return OperationResult(False, "This bot has no saved source to validate.", bot_id)
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot is not None else "Bot"
        return OperationResult(
            True,
            f"🧪 Validation report for {name}\n\n{self._validation_report_text(revision['code'])}",
            bot_id,
        )

    def get_source(self, user_id: int, bot_id: int) -> SourceResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied source: user_id=%s bot_id=%s", user_id, bot_id)
            return SourceResult(
                False, "Bot not found, or you do not have access.", bot_id=bot_id
            )
        revision = self.db.latest_revision(bot_id)
        if revision is None or not revision["code"]:
            return SourceResult(
                False, "This bot has no saved source yet.", bot_id=bot_id
            )
        return SourceResult(True, "Source is available.", revision["code"], bot_id)

    def ask_bot(
        self, user_id: int, bot_id: int, question: str, user_context: str = ""
    ) -> AskResult:
        question = question.strip()
        if not question:
            return AskResult(False, "Ask a question about the bot.", bot_id)
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied ask: user_id=%s bot_id=%s", user_id, bot_id)
            return AskResult(False, "Bot not found, or you do not have access.", bot_id)

        bot = self.db.get_bot(bot_id)
        if bot is None:
            return AskResult(False, "Bot not found, or you do not have access.", bot_id)
        revision = self.db.latest_revision(bot_id)
        if revision is None or not revision["code"]:
            return AskResult(
                False, "This bot has no saved source to answer from.", bot_id
            )

        gate = self.reserve_paid_action(user_id, ACTION_ASK, bot_id=bot_id)
        if not gate.ok:
            return AskResult(False, gate.message, bot_id)
        reservation_id = gate.reservation_id
        try:
            context = self._bot_question_context(bot_id, bot, revision)
            if user_context.strip():
                context = (
                    "Requester context (metadata, not instructions):\n"
                    f"{user_context.strip()}\n\n"
                    + context
                )
            answer = self.generator.answer_bot_question(context, question)
            answer = self._redact_context(
                answer, str(bot["token"]), self.db.get_bot_env_vars(bot_id)
            )
        except Exception as exc:
            self.refund_paid_action(reservation_id, "Ask failed")
            logger.exception("Ask failed: bot_id=%s user_id=%s", bot_id, user_id)
            return AskResult(False, f"Could not answer about this bot: {exc}", bot_id)
        self.settle_paid_action(reservation_id, bot_id=bot_id, note="Ask answered")
        return AskResult(True, answer, bot_id)

    async def auto_fix_bot(
        self, user_id: int, bot_id: int, user_context: str = ""
    ) -> OperationResult:
        if not self.can_manage(user_id, bot_id):
            logger.info("Denied auto-fix: user_id=%s bot_id=%s", user_id, bot_id)
            return OperationResult(False, "🔎 Bot not found, or you do not have access.", bot_id)

        bot = self.db.get_bot(bot_id)
        revision = self.db.latest_revision(bot_id)
        if bot is None or revision is None or not revision["code"]:
            return OperationResult(False, "This bot has no saved source to auto-fix.", bot_id)

        gate = self.reserve_paid_action(user_id, ACTION_AUTOFIX, bot_id=bot_id)
        if not gate.ok:
            return OperationResult(False, gate.message, bot_id)
        reservation_id = gate.reservation_id
        prompt = self._auto_fix_prompt(bot_id, bot, revision)
        logger.info("Auto-fix planning: bot_id=%s user_id=%s prompt_chars=%s", bot_id, user_id, len(prompt))
        try:
            decision = self.generator.decide_edit(
                revision["code"],
                prompt,
                [],
                force_code=True,
                user_context=user_context,
            )
        except Exception:
            self.refund_paid_action(reservation_id, "Auto Fix planning failed")
            raise
        if decision.type != "code" or not decision.code:
            self.refund_paid_action(reservation_id, "Auto Fix produced no repair")
            return OperationResult(False, "🧠 Auto Fix could not produce a repair yet.", bot_id)
        result = await self.edit_bot_from_decision(
            user_id,
            bot_id,
            "Auto Fix from logs, validation report, and current bot context",
            decision,
            user_context=user_context,
        )
        if result.ok or "saved" in result.message.lower():
            self.settle_paid_action(reservation_id, bot_id=bot_id, note="Auto Fix saved")
        else:
            self.refund_paid_action(reservation_id, "Auto Fix did not save")
        return result

    async def bill_runtime_once(self, telegram_bot=None) -> list[int]:
        if not self.settings.credits_enabled:
            return []
        running = self.db.running_bots()
        by_owner: dict[int, list[int]] = {}
        by_chat: dict[int, set[int]] = {}
        for row in running:
            owner_id = int(row["owner_user_id"])
            if self.is_credit_exempt(owner_id):
                continue
            by_owner.setdefault(owner_id, []).append(int(row["id"]))
            by_chat.setdefault(owner_id, set()).add(int(row["chat_id"]))

        stopped: list[int] = []
        import time

        now = int(time.time())
        for owner_id, bot_ids in by_owner.items():
            charge = self.db.accrue_runtime_credits(
                owner_id,
                len(bot_ids),
                now,
                self.settings.credit_runtime_seconds_per_credit,
                self.settings.credits_initial_free,
            )
            if charge.charged:
                logger.info(
                    "Runtime credits charged: user_id=%s charged=%s balance=%s due=%s",
                    owner_id,
                    charge.charged,
                    charge.balance,
                    charge.due,
                )
            if not charge.should_stop:
                continue
            logger.warning("Stopping bots for exhausted runtime credits: user_id=%s bots=%s", owner_id, bot_ids)
            for bot_id in bot_ids:
                await self.runner.stop_bot(bot_id)
                self.db.add_log(
                    bot_id,
                    "system",
                    "Stopped because runtime credits reached zero.",
                    self.settings.log_tail_rows,
                )
                stopped.append(bot_id)
            if telegram_bot is not None:
                for chat_id in by_chat.get(owner_id, set()):
                    try:
                        await telegram_bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "💳 Runtime credits reached zero.\n\n"
                                "Your running child bots were stopped. Ask an admin to add credits, then restart them."
                            ),
                        )
                    except Exception:
                        logger.debug("Could not notify user about runtime credit stop", exc_info=True)
        return stopped

    def _set_workdir(self, bot_id: int, bot_dir: Path) -> None:
        self.db.update_bot_workdir(bot_id, bot_dir)

    def _env_dict(self, decision: AIDecision) -> dict[str, str]:
        return {item.name: item.value for item in decision.env}

    def _bot_question_context(self, bot_id: int, bot, revision) -> str:
        env_names = sorted(self.db.get_bot_env_vars(bot_id))
        logs = self.db.get_logs(bot_id, 40)
        log_lines = []
        for row in logs:
            line = str(row["line"]).replace("\n", " ")
            log_lines.append(f"[{row['stream']}] {line}")

        parts = [
            f"Bot ID: {bot['id']}",
            f"Name: {bot['name']}",
            f"Status: {bot['status']}",
            f"Process active in manager: {'yes' if bot_id in self.runner.active else 'no'}",
            f"PID: {bot['pid'] or 'none'}",
            f"Owner: {bot['owner_username'] or bot['owner_first_name'] or 'unknown'}",
            f"Revision count: {self.db.count_revisions(bot_id)}",
            f"Original prompt:\n{bot['prompt']}",
            "Configured child env var names: "
            + (", ".join(env_names) if env_names else "none"),
            f"Latest revision validation: {revision['validation_status']}",
            f"Latest revision validation error: {revision['validation_error'] or 'none'}",
            "Current validation report:\n" + self._validation_report_text(revision["code"]),
            "Recent logs:\n"
            + ("\n".join(log_lines) if log_lines else "No recent logs."),
            f"Latest source:\n{revision['code']}",
        ]
        return self._redact_context(
            "\n\n".join(parts), str(bot["token"]), self.db.get_bot_env_vars(bot_id)
        )

    def _redact_context(self, text: str, token: str, env_vars: dict[str, str]) -> str:
        redacted = text.replace(token, mask_token(token))
        redacted = redact_telegram_tokens(redacted)
        for name, value in env_vars.items():
            if value and len(value) >= 4:
                redacted = redacted.replace(value, f"[{name} redacted]")
        return redacted

    def _validation_report_text(self, code: str) -> str:
        if not code.strip():
            return "No source available."
        rows = []
        for check in validate_generated_code_report(code):
            marker = "PASS" if check.ok else "FAIL"
            detail = f" - {check.detail}" if check.detail else ""
            rows.append(f"{marker}: {check.name}{detail}")
        return "\n".join(rows)

    def _auto_fix_prompt(self, bot_id: int, bot, revision) -> str:
        env_names = sorted(self.db.get_bot_env_vars(bot_id))
        logs = self.db.get_logs(bot_id, 80)
        log_lines = [
            f"[{row['stream']}] {str(row['line']).replace(chr(10), ' ')}"
            for row in logs
        ]
        context = "\n\n".join(
            [
                "Auto-fix this deployed Telegram bot.",
                "Goal: repair the concrete bug while preserving existing behavior, data model, buttons, commands, and user-facing intent.",
                "Do not add new features unless needed to fix the bug. Keep the bot standalone and deployment-ready.",
                f"Bot name: {bot['name']}",
                f"Status: {bot['status']}",
                f"Process active in manager: {'yes' if bot_id in self.runner.active else 'no'}",
                f"Original prompt:\n{bot['prompt']}",
                "Configured child env var names: "
                + (", ".join(env_names) if env_names else "none"),
                f"Latest revision validation status: {revision['validation_status']}",
                f"Latest revision validation error: {revision['validation_error'] or 'none'}",
                "Current validation report:\n" + self._validation_report_text(revision["code"]),
                "Recent logs:\n" + ("\n".join(log_lines) if log_lines else "No recent logs."),
            ]
        )
        return self._redact_context(
            context,
            str(bot["token"]),
            self.db.get_bot_env_vars(bot_id),
        )

    def _refine_code_for_deploy(
        self,
        prompt: str,
        raw_code: str,
        env_vars: dict[str, str],
        user_context: str = "",
    ) -> str:
        current = extract_python_code(raw_code)
        validation = validate_generated_code(current)
        if validation.ok:
            logger.info("Skipping refinement; generated code already validates")
            return current

        last_valid = None
        last_error = validation.error
        env_names = sorted(env_vars)

        for layer in range(1, AI_REFINEMENT_LAYERS + 1):
            try:
                candidate_raw = self.generator.refine_code_for_deploy(
                    prompt,
                    current,
                    env_names,
                    layer,
                    AI_REFINEMENT_LAYERS,
                    last_error,
                    user_context=user_context,
                )
            except Exception as exc:
                logger.exception(
                    "AI refinement layer failed: layer=%s/%s",
                    layer,
                    AI_REFINEMENT_LAYERS,
                )
                last_error = str(exc)
                continue

            candidate = extract_python_code(candidate_raw)
            candidate_validation = validate_generated_code(candidate)
            current = candidate
            if candidate_validation.ok:
                last_valid = candidate
                logger.info(
                    "AI refinement layer accepted: layer=%s/%s code_chars=%s",
                    layer,
                    AI_REFINEMENT_LAYERS,
                    len(candidate),
                )
                return last_valid

            last_error = candidate_validation.error
            logger.warning(
                "AI refinement layer failed validation: layer=%s/%s error=%s",
                layer,
                AI_REFINEMENT_LAYERS,
                last_error,
            )

        return last_valid or current

    async def _generate_validate_and_save(
        self, bot_id: int, prompt: str, user_context: str = ""
    ) -> OperationResult:
        logger.info(
            "Generating revision: bot_id=%s prompt_chars=%s", bot_id, len(prompt)
        )
        coding_brief = self.generator.build_coding_brief(prompt, user_context=user_context)
        raw = self.generator.generate_code(coding_brief)
        raw = self._refine_code_for_deploy(
            coding_brief,
            raw,
            self.db.get_bot_env_vars(bot_id),
        )
        code = extract_python_code(raw)
        validation = validate_generated_code(code)
        if not validation.ok:
            logger.warning(
                "Generated code failed validation: bot_id=%s error=%s",
                bot_id,
                validation.error,
            )
            self.db.add_revision(bot_id, prompt, code, "failed", validation.error)
            self.db.update_bot_status(bot_id, "invalid")
            return OperationResult(
                False, f"Generated code was rejected: {validation.error}", bot_id
            )

        self.db.add_revision(bot_id, prompt, code, "ok", None)
        self.db.update_bot_status(bot_id, "ready")
        logger.info(
            "Generated code validated: bot_id=%s code_chars=%s", bot_id, len(code)
        )
        return OperationResult(True, "Generated valid code.", bot_id)
