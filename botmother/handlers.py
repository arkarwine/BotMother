from __future__ import annotations

from html import escape
import logging
from typing import Any
import uuid

from .ai import AIDecision, AIReadinessDecision, MAX_FOLLOWUP_ROUNDS
from .db import Database
from .service import BotService, OperationResult


logger = logging.getLogger(__name__)


NEW_PROMPT, NEW_FOLLOWUP, NEW_TOKEN, REVISE_PROMPT, EDIT_PROMPT, EDIT_FOLLOWUP, ASK_PROMPT = range(7)
BOT_NOT_FOUND_TEXT = "🔎 Bot not found, or you do not have access."

STATUS_EMOJI = {
    "running": "🟢",
    "ready": "🟡",
    "starting": "🟡",
    "stopped": "⚫",
    "interrupted": "🟠",
    "crashed": "🔴",
    "invalid": "🔴",
    "launch_failed": "🔴",
    "restart_failed": "🔴",
    "deleted": "🗑️",
    "generating": "🧠",
}

HELP_TEXT = """🤖 BotMother

Build, run, inspect, and edit Telegram bots with plain-language prompts.

Use the keyboard for the actions you use most. Open a help category below for the full button-based guide."""

HELP_CATEGORY_TEXTS = {
    "create": (
        "🪄 Create\n\n"
        "Start with New Bot, describe what the bot should do, answer any AI follow-up questions, then paste the child token from @BotFather.\n\n"
        "Useful prompt details: admin IDs, payment/contact info, required buttons, languages, API keys/settings, and what users should see first."
    ),
    "manage": (
        "📦 Manage\n\n"
        "Use My Bots to open your bot list. From there, tap a bot to inspect it, ask questions, edit with a prompt, or regenerate it from a fresh prompt.\n\n"
        "You do not need to type bot IDs for normal management."
    ),
    "ops": (
        "🧰 Operations\n\n"
        "Use Status, Logs, Restart, Stop, and Delete from the keyboard. BotMother will show a bot picker first, then run the action you tap.\n\n"
        "Delete stops the child bot and frees its token for reuse."
    ),
    "utils": (
        "🪪 Utilities\n\n"
        "Use My ID when a generated bot needs Telegram admin IDs. Health shows the manager and child-process summary. Examples gives ready-to-edit prompt ideas."
    ),
    "fallback": (
        "⌨️ Command Fallbacks\n\n"
        "Buttons are the main interface, but Telegram commands still work when you need them:\n"
        "/newbot, /bots, /status, /logs, /ask, /edit, /revise, /restart, /stop, /delete, /id, /health, /cancel."
    ),
}

EXAMPLES_TEXT = """✨ Bot Ideas

Copy one, tweak it, then send /newbot.

🛒 Shop bot
Online store bot with product catalog, cart, KPay payment instructions, order tracking, and admin notifications. Admin IDs are 123456789 and 987654321.

📅 Booking bot
Appointment booking bot for a small clinic. Users choose date/time, leave phone number, and admins can view bookings.

🎓 Quiz bot
Daily quiz bot with scores, leaderboard, hints, and admin command to add questions.

📣 Channel assistant
Bot that drafts announcements, stores reusable templates, and lets admins broadcast to subscribers.

Tip: include required admin IDs, payment info, API keys/settings, and any must-have commands up front."""


def parse_bot_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        value = int(args[0])
    except ValueError:
        return None
    return value if value > 0 else None


def parse_tail_args(args: list[str], default_limit: int = 30, max_limit: int = 100) -> tuple[int | None, int, str | None]:
    bot_id = parse_bot_id(args)
    if bot_id is None:
        return None, default_limit, "🧾 Choose Logs from the keyboard, or use /tail with a bot ID."

    if len(args) < 2:
        return bot_id, default_limit, None

    try:
        limit = int(args[1])
    except ValueError:
        return bot_id, default_limit, "🔢 Lines must be a number.\nExample: /tail 3 80"

    if limit < 1:
        return bot_id, default_limit, "🔢 Lines must be at least 1."
    return bot_id, min(limit, max_limit), None


def parse_ask_args(args: list[str]) -> tuple[int | None, str, str | None]:
    bot_id = parse_bot_id(args)
    if bot_id is None:
        return None, "", "💬 Choose Ask Bot from the keyboard, or use /ask with a bot ID."
    return bot_id, " ".join(args[1:]).strip(), None


def status_badge(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '•')} {status}"


def format_bot_list(rows: list[Any]) -> str:
    if not rows:
        return "🪄 No child bots yet.\nTap New Bot to create one, or open Examples for ideas."
    lines = ["📦 Your bots:", "Tap a bot to open actions."]
    for row in rows:
        lines.append(f"#{row['id']} • {status_badge(row['status'])} • {row['name']}")
    return "\n".join(lines)


def format_bot_status(row: Any) -> str:
    pid = row["pid"] if row["pid"] is not None else "-"
    return (
        f"📦 Bot #{row['id']}\n"
        f"Name: {row['name']}\n"
        f"Status: {status_badge(row['status'])}\n"
        f"PID: {pid}\n"
        f"Owner: {row['owner_user_id']}\n\n"
        "Use the buttons below for logs, edits, restarts, and cleanup."
    )


def format_logs(rows: list[Any]) -> str:
    if not rows:
        return "🧾 No logs yet. Start or restart the bot, then open Logs again."
    lines = []
    for row in rows:
        line = str(row["line"]).replace("\n", " ")
        lines.append(f"[{row['stream']}] {line}")
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[-3500:]
    return text


def compact_bot_label(row: Any) -> str:
    name = str(row["name"])
    if len(name) > 24:
        name = name[:21] + "..."
    return f"#{row['id']} {STATUS_EMOJI.get(row['status'], '•')} {name}"


def help_category_text(category: str) -> str:
    return HELP_CATEGORY_TEXTS.get(category, HELP_TEXT)


def chunk_text(text: str, chunk_size: int = 3200) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def question_texts(decision: AIDecision | AIReadinessDecision) -> list[str]:
    return [question.question for question in decision.questions]


def format_ai_questions(decision: AIDecision | AIReadinessDecision) -> str:
    if decision.message.strip():
        return decision.message.strip()
    if decision.questions:
        return "\n\n".join(question.question for question in decision.questions)
    return "I need a little more detail before building."


def _user_tuple(update: Any) -> tuple[int, str | None, str | None, str | None]:
    user = update.effective_user
    return (int(user.id), user.username, user.first_name, user.last_name)


def _chat_id(update: Any) -> int:
    return int(update.effective_chat.id)


def _remember_user(db: Database, update: Any) -> int:
    user_id, username, first_name, last_name = _user_tuple(update)
    db.upsert_user(user_id, username, first_name, last_name)
    return user_id


def build_application(token: str, db: Database, service: BotService):
    try:
        from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
        from telegram.constants import ParseMode
        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
            CommandHandler,
            ContextTypes,
            ConversationHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise RuntimeError("python-telegram-bot is not installed. Run: pip install -r requirements.txt") from exc

    main_keyboard = ReplyKeyboardMarkup(
        [
            ["🪄 New Bot", "📦 My Bots", "📊 Status"],
            ["💬 Ask Bot", "✏️ Edit Bot", "♻️ Revise"],
            ["🧾 Logs", "🔄 Restart", "🛑 Stop"],
            ["🗑️ Delete", "✨ Examples", "🪪 My ID"],
            ["🩺 Health", "❔ Help", "❌ Cancel"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

    def help_menu_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🪄 Create", callback_data="help:create"),
                    InlineKeyboardButton("📦 Manage", callback_data="help:manage"),
                ],
                [
                    InlineKeyboardButton("🧰 Operations", callback_data="help:ops"),
                    InlineKeyboardButton("🪪 Utilities", callback_data="help:utils"),
                ],
                [InlineKeyboardButton("⌨️ Command Fallbacks", callback_data="help:fallback")],
            ]
        )

    def help_category_keyboard(category: str):
        if category == "create":
            rows = [
                [
                    InlineKeyboardButton("🪄 New Bot", callback_data="nav:newbot"),
                    InlineKeyboardButton("✨ Examples", callback_data="nav:examples"),
                ],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        elif category == "manage":
            rows = [
                [
                    InlineKeyboardButton("📦 My Bots", callback_data="nav:bots"),
                    InlineKeyboardButton("📊 Status", callback_data="pick:status"),
                ],
                [
                    InlineKeyboardButton("💬 Ask Bot", callback_data="pick:ask"),
                    InlineKeyboardButton("✏️ Edit Bot", callback_data="pick:edit"),
                ],
                [InlineKeyboardButton("♻️ Revise", callback_data="pick:revise")],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        elif category == "ops":
            rows = [
                [
                    InlineKeyboardButton("🧾 Logs", callback_data="pick:tail"),
                    InlineKeyboardButton("🔄 Restart", callback_data="pick:restart"),
                ],
                [
                    InlineKeyboardButton("🛑 Stop", callback_data="pick:stop"),
                    InlineKeyboardButton("🗑️ Delete", callback_data="pick:delete_confirm"),
                ],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        elif category == "utils":
            rows = [
                [
                    InlineKeyboardButton("🪪 My ID", callback_data="nav:id"),
                    InlineKeyboardButton("🩺 Health", callback_data="nav:health"),
                ],
                [InlineKeyboardButton("✨ Examples", callback_data="nav:examples")],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        elif category == "fallback":
            rows = [
                [
                    InlineKeyboardButton("🪄 New Bot", callback_data="nav:newbot"),
                    InlineKeyboardButton("📦 My Bots", callback_data="nav:bots"),
                ],
                [
                    InlineKeyboardButton("📊 Status", callback_data="pick:status"),
                    InlineKeyboardButton("🧾 Logs", callback_data="pick:tail"),
                ],
                [
                    InlineKeyboardButton("💬 Ask Bot", callback_data="pick:ask"),
                    InlineKeyboardButton("✏️ Edit Bot", callback_data="pick:edit"),
                ],
                [
                    InlineKeyboardButton("♻️ Revise", callback_data="pick:revise"),
                    InlineKeyboardButton("🔄 Restart", callback_data="pick:restart"),
                ],
                [
                    InlineKeyboardButton("🛑 Stop", callback_data="pick:stop"),
                    InlineKeyboardButton("🗑️ Delete", callback_data="pick:delete_confirm"),
                ],
                [
                    InlineKeyboardButton("🪪 My ID", callback_data="nav:id"),
                    InlineKeyboardButton("🩺 Health", callback_data="nav:health"),
                ],
                [
                    InlineKeyboardButton("✨ Examples", callback_data="nav:examples"),
                    InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
                ],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton("🪄 New Bot", callback_data="nav:newbot"),
                    InlineKeyboardButton("📦 My Bots", callback_data="nav:bots"),
                ],
                [InlineKeyboardButton("📚 Help Menu", callback_data="nav:help")],
            ]
        return InlineKeyboardMarkup(rows)

    def empty_state_keyboard():
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🪄 New Bot", callback_data="nav:newbot")],
                [
                    InlineKeyboardButton("✨ Examples", callback_data="nav:examples"),
                    InlineKeyboardButton("📚 Help", callback_data="nav:help"),
                ],
            ]
        )

    def bot_actions_keyboard(bot_id: int):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 Status", callback_data=f"status:{bot_id}"),
                    InlineKeyboardButton("🧾 Logs", callback_data=f"tail:{bot_id}"),
                ],
                [
                    InlineKeyboardButton("💬 Ask", callback_data=f"ask:{bot_id}"),
                    InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{bot_id}"),
                ],
                [InlineKeyboardButton("♻️ Revise", callback_data=f"revise:{bot_id}")],
                [
                    InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{bot_id}"),
                    InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{bot_id}"),
                ],
                [InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_confirm:{bot_id}")],
            ]
        )

    def bots_keyboard(rows: list[Any], action: str = "status"):
        if not rows:
            return empty_state_keyboard()
        buttons = [[InlineKeyboardButton(compact_bot_label(row), callback_data=f"{action}:{row['id']}")] for row in rows[:20]]
        buttons.append(
            [
                InlineKeyboardButton("🪄 New Bot", callback_data="nav:newbot"),
                InlineKeyboardButton("📚 Help", callback_data="nav:help"),
            ]
        )
        return InlineKeyboardMarkup(buttons)

    async def reply_home(message, text: str) -> None:
        await message.reply_text(text, reply_markup=main_keyboard)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info("Command /start: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await reply_home(update.effective_message, HELP_TEXT)
        await update.effective_message.reply_text("📚 Choose a help category:", reply_markup=help_menu_keyboard())

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info("Command /help: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await reply_home(update.effective_message, HELP_TEXT)
        await update.effective_message.reply_text("📚 Choose a help category:", reply_markup=help_menu_keyboard())

    async def examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info("Command /examples: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await update.effective_message.reply_text(EXAMPLES_TEXT)

    async def identity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        chat_id = _chat_id(update)
        logger.info("Command /id: user_id=%s chat_id=%s", user_id, chat_id)
        await update.effective_message.reply_text(
            f"🪪 Your Telegram user ID: {user_id}\n"
            f"Chat ID: {chat_id}\n\n"
            "Use this as an admin ID when creating bots that need admin-only commands.",
        )

    async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        rows = service.list_bots_for(user_id)
        active_count = len(service.runner.active)
        running_visible = sum(1 for row in rows if row["status"] == "running")
        logger.info("Command /health: user_id=%s visible_bots=%s active=%s", user_id, len(rows), active_count)
        scope = "all bots" if service.is_owner(user_id) else "your bots"
        await update.effective_message.reply_text(
            "🩺 BotMother Health\n"
            f"Manager: online\n"
            f"Scope: {scope}\n"
            f"Visible bots: {len(rows)}\n"
            f"Running in DB: {running_visible}\n"
            f"Active child processes: {active_count}",
        )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /cancel: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.clear()
        await update.effective_message.reply_text("✅ Cancelled. Pick a next action below.", reply_markup=main_keyboard)
        return ConversationHandler.END

    async def newbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /newbot: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.pop("newbot_prompt", None)
        await update.effective_message.reply_text(
            "🪄 Describe the Telegram bot you want to build.\n\n"
            "Include the job it should do, required commands, admin IDs, payment/contact details, and any API keys/settings it will need.\n\n"
            "Example:\n"
            "Online shop bot with product catalog, cart, KPay payment phone number, order notifications to admin ID 123456789, and /addproduct for admins.",
            reply_markup=main_keyboard,
        )
        return NEW_PROMPT

    async def newbot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("✍️ Send a text prompt describing the child bot, or use /examples for ideas.")
            return NEW_PROMPT
        logger.info("Received newbot prompt: user_id=%s chars=%s", user_id, len(prompt))
        context.user_data["newbot_prompt"] = prompt
        context.user_data["newbot_answers"] = []
        context.user_data.pop("newbot_decision", None)
        return await continue_newbot_planning(update, context)

    async def continue_newbot_planning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        prompt = context.user_data.get("newbot_prompt", "")
        answers = context.user_data.get("newbot_answers", [])
        force_code = len(answers) >= MAX_FOLLOWUP_ROUNDS
        await update.effective_message.reply_text("🧠 Thinking through the requirements...")
        decision = service.plan_new_bot(prompt, answers, force_code=force_code)
        if decision.needs_questions:
            if force_code:
                await update.effective_message.reply_text(
                    "⚠️ I still need more detail before I can generate this safely.\n\n"
                    "Try /newbot again and include the missing essentials up front: admin IDs, payment/contact details, API keys/settings, and must-have commands."
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(decision)
            await update.effective_message.reply_text(format_ai_questions(decision))
            return NEW_FOLLOWUP

        readiness = service.check_new_bot_readiness(prompt, answers, decision)
        if readiness.needs_questions:
            if force_code:
                await update.effective_message.reply_text(
                    "⚠️ I still need an essential launch detail before I can generate this safely.\n\n"
                    "Try /newbot again with the missing required data included up front."
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(readiness)
            await update.effective_message.reply_text(format_ai_questions(readiness))
            return NEW_FOLLOWUP

        context.user_data["newbot_decision"] = decision
        await update.effective_message.reply_text(
            (decision.message + "\n\n" if decision.message else "")
            + "🔐 Final step: paste the child bot token from @BotFather.\n\n"
            "Create a separate child bot in @BotFather with /newbot, then paste only that token here. Do not use the mother bot token."
        )
        return NEW_TOKEN

    async def newbot_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await update.effective_message.reply_text("✍️ Reply with the missing details, or use /cancel.")
            return NEW_FOLLOWUP
        answers = context.user_data.setdefault("newbot_answers", [])
        answers.append(
            {
                "questions": context.user_data.get("newbot_pending_questions", []),
                "answer": answer,
            }
        )
        context.user_data.pop("newbot_pending_questions", None)
        return await continue_newbot_planning(update, context)

    async def newbot_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        prompt = context.user_data.get("newbot_prompt", "")
        decision = context.user_data.get("newbot_decision")
        token_text = (update.effective_message.text or "").strip()
        logger.info("Received newbot token; creating bot: user_id=%s prompt_chars=%s", user_id, len(prompt))
        if not isinstance(decision, AIDecision):
            await update.effective_message.reply_text("⌛ The AI plan expired. Tap New Bot to start again.")
            context.user_data.clear()
            return ConversationHandler.END
        await update.effective_message.reply_text("🚀 Refining, validating, sandboxing, and launching the child bot...")
        result = await service.create_bot_from_decision(user_id, _chat_id(update), prompt, token_text, decision)
        logger.info("Create bot result: user_id=%s ok=%s bot_id=%s", user_id, result.ok, result.bot_id)
        await update.effective_message.reply_text(result.message)
        context.user_data.clear()
        return ConversationHandler.END

    async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        rows = service.list_bots_for(user_id)
        logger.info("Command /bots: user_id=%s count=%s", user_id, len(rows))
        await update.effective_message.reply_text(format_bot_list(rows), reply_markup=bots_keyboard(rows))

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /status: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            rows = service.list_bots_for(user_id)
            await update.effective_message.reply_text(format_bot_list(rows), reply_markup=bots_keyboard(rows))
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return
        await update.effective_message.reply_text(format_bot_status(row))

    async def tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        command = update.effective_message.text.split(maxsplit=1)[0] if update.effective_message.text else "/tail"
        if not context.args:
            await choose_bot_for_action(update, "tail", "🧾 Choose a bot to inspect logs:")
            return
        bot_id, limit, error = parse_tail_args(context.args)
        logger.info("Command %s: user_id=%s bot_id=%s limit=%s", command, user_id, bot_id, limit)
        if error is not None:
            await update.effective_message.reply_text(error)
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return
        await update.effective_message.reply_text(
            f"<pre>{escape(format_logs(db.get_logs(bot_id, limit)))}</pre>",
            parse_mode=ParseMode.HTML,
        )

    async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        if not context.args:
            await choose_bot_for_action(update, "ask", "💬 Choose a bot to ask about:")
            return ConversationHandler.END
        bot_id, question, error = parse_ask_args(context.args)
        logger.info("Command /ask: user_id=%s bot_id=%s question_chars=%s", user_id, bot_id, len(question))
        if error is not None:
            await update.effective_message.reply_text(error)
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        if question:
            return await answer_bot_question(update, context, user_id, bot_id, question)
        context.user_data["ask_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"💬 What do you want to know about bot #{bot_id}?\n\n"
            "Examples:\n"
            f"Why did bot #{bot_id} stop?\n"
            f"What commands does bot #{bot_id} support?\n"
            f"How should I edit bot #{bot_id} to add payments?",
        )
        return ASK_PROMPT

    async def ask_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["ask_bot_id"])
        question = (update.effective_message.text or "").strip()
        if not question:
            await update.effective_message.reply_text("✍️ Ask a question about the bot, or use /cancel.")
            return ASK_PROMPT
        return await answer_bot_question(update, context, user_id, bot_id, question)

    async def answer_bot_question(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        bot_id: int,
        question: str,
    ) -> int:
        await update.effective_message.reply_text("🔍 Reading bot context, latest source, and recent logs...")
        result = service.ask_bot(user_id, bot_id, question)
        logger.info("Ask bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok)
        for chunk in chunk_text(result.message):
            await update.effective_message.reply_text(chunk)
        context.user_data.pop("ask_bot_id", None)
        return ConversationHandler.END

    async def choose_bot_for_action(update: Update, action: str, title: str) -> None:
        user_id = _remember_user(db, update)
        rows = service.list_bots_for(user_id)
        logger.info("Choose bot action: user_id=%s action=%s count=%s", user_id, action, len(rows))
        if not rows:
            await update.effective_message.reply_text(
                "🪄 No bots yet. Tap New Bot to create one first.",
                reply_markup=empty_state_keyboard(),
            )
            return
        await update.effective_message.reply_text(title, reply_markup=bots_keyboard(rows, action))

    async def button_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        if text == "📦 My Bots":
            await bots(update, context)
        elif text == "📊 Status":
            await choose_bot_for_action(update, "status", "📊 Choose a bot to inspect:")
        elif text == "💬 Ask Bot":
            await choose_bot_for_action(update, "ask", "💬 Choose a bot to ask about:")
        elif text == "✏️ Edit Bot":
            await choose_bot_for_action(update, "edit", "✏️ Choose a bot to edit:")
        elif text == "♻️ Revise":
            await choose_bot_for_action(update, "revise", "♻️ Choose a bot to regenerate:")
        elif text == "🧾 Logs":
            await choose_bot_for_action(update, "tail", "🧾 Choose a bot to inspect logs:")
        elif text == "🔄 Restart":
            await choose_bot_for_action(update, "restart", "🔄 Choose a bot to restart:")
        elif text == "🛑 Stop":
            await choose_bot_for_action(update, "stop", "🛑 Choose a bot to stop:")
        elif text == "🗑️ Delete":
            await choose_bot_for_action(update, "delete_confirm", "🗑️ Choose a bot to delete:")
        elif text == "🪪 My ID":
            await identity(update, context)
        elif text == "🩺 Health":
            await health(update, context)
        elif text == "✨ Examples":
            await examples(update, context)
        elif text == "❔ Help":
            await help_command(update, context)
        elif text == "❌ Cancel":
            await cancel(update, context)

    async def newbot_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query is not None:
            await update.callback_query.answer()
        return await newbot(update, context)

    async def ask_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        context.user_data["ask_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"💬 What do you want to know about bot #{bot_id}?",
        )
        return ASK_PROMPT

    async def edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        context.user_data["edit_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"✏️ Describe what you want to change in bot #{bot_id}.",
        )
        return EDIT_PROMPT

    async def revise_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        context.user_data["revise_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"♻️ Describe the new complete version of bot #{bot_id}.\n\n"
            "Use this when the bot should be rebuilt from a fresh prompt. For a smaller change, tap Edit.",
        )
        return REVISE_PROMPT

    async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        user_id = _remember_user(db, update)
        data = query.data
        logger.info("Callback button: user_id=%s data=%s", user_id, data)

        if data == "nav:help":
            await reply_home(update.effective_message, HELP_TEXT)
            await update.effective_message.reply_text("📚 Choose a help category:", reply_markup=help_menu_keyboard())
            return
        if data == "nav:examples":
            await update.effective_message.reply_text(EXAMPLES_TEXT)
            return
        if data == "nav:bots":
            rows = service.list_bots_for(user_id)
            await update.effective_message.reply_text(format_bot_list(rows), reply_markup=bots_keyboard(rows))
            return
        if data == "nav:id":
            await update.effective_message.reply_text(
                f"🪪 Your Telegram user ID: {user_id}\n"
                f"Chat ID: {_chat_id(update)}\n\n"
                "Use this as an admin ID when a bot needs admin-only controls.",
            )
            return
        if data == "nav:cancel":
            context.user_data.clear()
            await update.effective_message.reply_text("✅ Cancelled. Pick a next action below.", reply_markup=main_keyboard)
            return
        if data == "nav:health":
            rows = service.list_bots_for(user_id)
            active_count = len(service.runner.active)
            running_visible = sum(1 for row in rows if row["status"] == "running")
            await update.effective_message.reply_text(
                "🩺 BotMother Health\n"
                f"Visible bots: {len(rows)}\n"
                f"Running in DB: {running_visible}\n"
                f"Active child processes: {active_count}",
            )
            return
        if data.startswith("help:"):
            category = data.split(":", 1)[1]
            await update.effective_message.reply_text(
                help_category_text(category),
                reply_markup=help_category_keyboard(category),
            )
            return
        if data.startswith("pick:"):
            action = data.split(":", 1)[1]
            titles = {
                "status": "📊 Choose a bot to inspect:",
                "ask": "💬 Choose a bot to ask about:",
                "edit": "✏️ Choose a bot to edit:",
                "revise": "♻️ Choose a bot to regenerate:",
                "tail": "🧾 Choose a bot to inspect logs:",
                "restart": "🔄 Choose a bot to restart:",
                "stop": "🛑 Choose a bot to stop:",
                "delete_confirm": "🗑️ Choose a bot to delete:",
            }
            await choose_bot_for_action(update, action, titles.get(action, "Choose a bot:"))
            return

        if ":" not in data:
            return
        action, raw_bot_id = data.split(":", 1)
        try:
            bot_id = int(raw_bot_id)
        except ValueError:
            return

        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return

        if action == "status":
            row = service.get_accessible_bot(user_id, bot_id)
            await update.effective_message.reply_text(format_bot_status(row))
        elif action == "tail":
            await update.effective_message.reply_text(
                f"<pre>{escape(format_logs(db.get_logs(bot_id, 50)))}</pre>",
                parse_mode=ParseMode.HTML,
            )
        elif action == "restart":
            result = await service.restart_bot(user_id, bot_id)
            await update.effective_message.reply_text(result.message)
        elif action == "stop":
            result = await service.stop_bot(user_id, bot_id)
            await update.effective_message.reply_text(result.message)
        elif action == "delete_confirm":
            await update.effective_message.reply_text(
                f"🗑️ Delete bot #{bot_id}? This stops it and frees its token.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Yes, delete", callback_data=f"delete:{bot_id}"),
                            InlineKeyboardButton("Cancel", callback_data=f"status:{bot_id}"),
                        ]
                    ]
                ),
            )
        elif action == "delete":
            result = await service.delete_bot(user_id, bot_id)
            await update.effective_message.reply_text(result.message)

    async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /source: user_id=%s bot_id=%s", user_id, bot_id)
        if not service.is_owner(user_id):
            await update.effective_message.reply_text("🔒 Raw source is owner-only. Use Edit Bot to change bots with a prompt.")
            return
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /source <id>")
            return
        result = service.get_source(user_id, bot_id)
        if not result.ok or result.code is None:
            await update.effective_message.reply_text(result.message)
            return

        chunks = chunk_text(result.code)
        if len(chunks) > 1:
            await update.effective_message.reply_text(f"Source for bot #{bot_id} ({len(chunks)} parts):")
        for chunk in chunks:
            await update.effective_message.reply_text(
                f"<pre>{escape(chunk)}</pre>",
                parse_mode=ParseMode.HTML,
            )

    async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /stop: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "stop", "🛑 Choose a bot to stop:")
            return
        result = await service.stop_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /restart: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "restart", "🔄 Choose a bot to restart:")
            return
        result = await service.restart_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /delete: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "delete_confirm", "🗑️ Choose a bot to delete:")
            return
        result = await service.delete_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def killall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.warning("Command /killall: user_id=%s", user_id)
        result = await service.kill_all(user_id)
        await update.effective_message.reply_text(result.message)

    async def revise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /revise: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "revise", "♻️ Choose a bot to regenerate:")
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        context.user_data["revise_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"♻️ Send the new full prompt for bot #{bot_id}.\n\n"
            "This regenerates the bot from scratch. For smaller changes, tap Edit instead."
        )
        return REVISE_PROMPT

    async def revise_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["revise_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("✍️ Send a text prompt for the revision, or use /cancel.")
            return REVISE_PROMPT
        logger.info("Received revise prompt: user_id=%s bot_id=%s chars=%s", user_id, bot_id, len(prompt))
        await update.effective_message.reply_text("♻️ Regenerating, refining, validating, and restarting the child bot...")
        result = await service.revise_bot(user_id, bot_id, prompt)
        logger.info("Revise bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok)
        await update.effective_message.reply_text(result.message)
        context.user_data.pop("revise_bot_id", None)
        return ConversationHandler.END

    async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /edit: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "edit", "✏️ Choose a bot to edit:")
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text(BOT_NOT_FOUND_TEXT)
            return ConversationHandler.END
        context.user_data["edit_bot_id"] = bot_id
        await update.effective_message.reply_text(
            f"✏️ Describe what you want to change in bot #{bot_id}.\n\n"
            "Examples:\n"
            "Add a /help command with examples.\n"
            "Make checkout ask for phone number and address.\n"
            "Improve error messages and admin notifications.\n\n"
            "The AI may ask follow-up questions. Tap Cancel to abort.",
        )
        return EDIT_PROMPT

    async def edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["edit_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("✍️ Describe the change you want, or use /cancel.")
            return EDIT_PROMPT
        context.user_data["edit_prompt"] = prompt
        context.user_data["edit_answers"] = []
        return await continue_edit_planning(update, context)

    async def continue_edit_planning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["edit_bot_id"])
        prompt = context.user_data.get("edit_prompt", "")
        answers = context.user_data.get("edit_answers", [])
        force_code = len(answers) >= MAX_FOLLOWUP_ROUNDS
        await update.effective_message.reply_text("🧠 Thinking through the edit...")
        decision = service.plan_edit_bot(user_id, bot_id, prompt, answers, force_code=force_code)
        if isinstance(decision, OperationResult):
            await update.effective_message.reply_text(decision.message)
            context.user_data.clear()
            return ConversationHandler.END
        if decision.needs_questions:
            if force_code:
                await update.effective_message.reply_text(
                    "⚠️ I still need more detail before I can edit this safely.\n\n"
                    "Try /edit again with the missing details included up front."
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["edit_pending_questions"] = question_texts(decision)
            await update.effective_message.reply_text(format_ai_questions(decision))
            return EDIT_FOLLOWUP

        await update.effective_message.reply_text("🚀 Refining, validating, and restarting the child bot...")
        result = await service.edit_bot_from_decision(user_id, bot_id, prompt, decision)
        logger.info("Edit bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok)
        await update.effective_message.reply_text(result.message)
        context.user_data.clear()
        return ConversationHandler.END

    async def edit_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await update.effective_message.reply_text("✍️ Reply with the missing details, or use /cancel.")
            return EDIT_FOLLOWUP
        answers = context.user_data.setdefault("edit_answers", [])
        answers.append(
            {
                "questions": context.user_data.get("edit_pending_questions", []),
                "answer": answer,
            }
        )
        context.user_data.pop("edit_pending_questions", None)
        return await continue_edit_planning(update, context)

    async def post_init(application) -> None:
        logger.info("Post-init: restoring child bots")
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Show help"),
                BotCommand("help", "Show full guide"),
                BotCommand("examples", "Prompt examples"),
                BotCommand("newbot", "Create and launch a child bot"),
                BotCommand("bots", "List your child bots"),
                BotCommand("status", "Show bot status, or list all bots"),
                BotCommand("tail", "Show child bot logs"),
                BotCommand("ask", "Ask about a child bot"),
                BotCommand("edit", "Change a child bot with prompts"),
                BotCommand("id", "Show your Telegram user ID"),
                BotCommand("health", "Show manager health"),
                BotCommand("delete", "Stop and delete a child bot"),
                BotCommand("stop", "Stop a child bot"),
                BotCommand("restart", "Restart a child bot"),
                BotCommand("revise", "Regenerate a child bot"),
                BotCommand("cancel", "Cancel the active flow"),
            ]
        )
        await service.runner.restore_running_bots()

    async def post_shutdown(application) -> None:
        logger.info("Post-shutdown: stopping active child bots")
        await service.runner.stop_all(mark_stopped=False)

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        exc = context.error
        error_id = uuid.uuid4().hex[:8]
        exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
        logger.error("Unhandled Telegram handler error: error_id=%s update=%r", error_id, update, exc_info=exc_info)
        if isinstance(update, Update) and update.effective_message is not None:
            try:
                detail = ""
                if update.effective_user is not None and service.is_owner(int(update.effective_user.id)) and exc is not None:
                    detail = f"\nOwner detail: {type(exc).__name__}: {exc}"
                await update.effective_message.reply_text(
                    "⚠️ BotMother hit an unexpected error.\n\n"
                    f"Error ID: {error_id}\n"
                    "Try the action again, or open Help for usage examples. "
                    "If this involved a child bot, the Logs button may show more context."
                    f"{detail}"
                )
            except Exception:
                logger.exception("Failed to send handler error message: error_id=%s", error_id)

    conversation_text = filters.TEXT & ~filters.COMMAND & ~filters.Regex("^❌ Cancel$")
    cancel_fallbacks = [CommandHandler("cancel", cancel), MessageHandler(filters.Regex("^❌ Cancel$"), cancel)]

    newbot_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newbot", newbot),
            MessageHandler(filters.Regex("^🪄 New Bot$"), newbot),
            CallbackQueryHandler(newbot_button, pattern="^nav:newbot$"),
        ],
        states={
            NEW_PROMPT: [MessageHandler(conversation_text, newbot_prompt)],
            NEW_FOLLOWUP: [MessageHandler(conversation_text, newbot_followup)],
            NEW_TOKEN: [MessageHandler(conversation_text, newbot_token)],
        },
        fallbacks=cancel_fallbacks,
    )

    revise_conv = ConversationHandler(
        entry_points=[
            CommandHandler("revise", revise),
            CallbackQueryHandler(revise_button, pattern=r"^revise:\d+$"),
        ],
        states={
            REVISE_PROMPT: [MessageHandler(conversation_text, revise_prompt)],
        },
        fallbacks=cancel_fallbacks,
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit),
            CallbackQueryHandler(edit_button, pattern=r"^edit:\d+$"),
        ],
        states={
            EDIT_PROMPT: [MessageHandler(conversation_text, edit_prompt)],
            EDIT_FOLLOWUP: [MessageHandler(conversation_text, edit_followup)],
        },
        fallbacks=cancel_fallbacks,
    )

    ask_conv = ConversationHandler(
        entry_points=[
            CommandHandler("ask", ask),
            CallbackQueryHandler(ask_button, pattern=r"^ask:\d+$"),
        ],
        states={
            ASK_PROMPT: [MessageHandler(conversation_text, ask_prompt)],
        },
        fallbacks=cancel_fallbacks,
    )

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("commands", help_command))
    application.add_handler(CommandHandler("usage", help_command))
    application.add_handler(CommandHandler("examples", examples))
    application.add_handler(CommandHandler("id", identity))
    application.add_handler(CommandHandler("whoami", identity))
    application.add_handler(CommandHandler("health", health))
    application.add_handler(newbot_conv)
    application.add_handler(revise_conv)
    application.add_handler(edit_conv)
    application.add_handler(ask_conv)
    application.add_handler(
        MessageHandler(
            filters.Regex(
                "^(📦 My Bots|📊 Status|💬 Ask Bot|✏️ Edit Bot|♻️ Revise|🧾 Logs|🔄 Restart|🛑 Stop|🗑️ Delete|✨ Examples|🪪 My ID|🩺 Health|❔ Help|❌ Cancel)$"
            ),
            button_text,
        )
    )
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("bots", bots))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("tail", tail))
    application.add_handler(CommandHandler("logs", tail))
    application.add_handler(CommandHandler("source", source))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CommandHandler("killall", killall))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_error_handler(error_handler)
    return application
