from __future__ import annotations

from html import escape
import logging
from typing import Any

from .ai import AIDecision, MAX_FOLLOWUP_ROUNDS
from .db import Database
from .service import BotService, OperationResult


logger = logging.getLogger(__name__)


NEW_PROMPT, NEW_FOLLOWUP, NEW_TOKEN, REVISE_PROMPT, EDIT_PROMPT, EDIT_FOLLOWUP = range(6)


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
        return None, default_limit, "Usage: /tail <id> [lines]"

    if len(args) < 2:
        return bot_id, default_limit, None

    try:
        limit = int(args[1])
    except ValueError:
        return bot_id, default_limit, "Lines must be a number."

    if limit < 1:
        return bot_id, default_limit, "Lines must be at least 1."
    return bot_id, min(limit, max_limit), None


def format_bot_list(rows: list[Any]) -> str:
    if not rows:
        return "No bots yet. Use /newbot to create one."
    lines = ["Your bots:"]
    for row in rows:
        lines.append(f"#{row['id']} - {row['status']} - {row['name']}")
    return "\n".join(lines)


def format_bot_status(row: Any) -> str:
    pid = row["pid"] if row["pid"] is not None else "-"
    return (
        f"Bot #{row['id']}\n"
        f"Name: {row['name']}\n"
        f"Status: {row['status']}\n"
        f"PID: {pid}\n"
        f"Owner: {row['owner_user_id']}"
    )


def format_logs(rows: list[Any]) -> str:
    if not rows:
        return "No logs yet."
    lines = []
    for row in rows:
        line = str(row["line"]).replace("\n", " ")
        lines.append(f"[{row['stream']}] {line}")
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[-3500:]
    return text


def chunk_text(text: str, chunk_size: int = 3200) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def question_texts(decision: AIDecision) -> list[str]:
    return [question.question for question in decision.questions]


def format_ai_questions(decision: AIDecision, round_number: int) -> str:
    lines = [decision.message or "I need a little more detail before building."]
    for index, question in enumerate(decision.questions, start=1):
        lines.append(f"{index}. {question.question}")
        if question.suggestions:
            lines.append("Suggestions: " + "; ".join(question.suggestions))
    lines.append(f"Reply with your answers. Follow-up {round_number}/{MAX_FOLLOWUP_ROUNDS}.")
    return "\n".join(lines)


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
        from telegram import BotCommand, Update
        from telegram.constants import ParseMode
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            ContextTypes,
            ConversationHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise RuntimeError("python-telegram-bot is not installed. Run: pip install -r requirements.txt") from exc

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info("Command /start: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await update.effective_message.reply_text(
            "BotMother is ready.\n"
            "Use /newbot to build a child bot.\n"
            "Use /bots to list your bots.\n"
            "Use /tail <id> to see child bot logs.\n"
            "Use /edit <id> to change a bot with a prompt.\n"
            "The AI may ask follow-up questions before building."
        )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /cancel: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.clear()
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def newbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /newbot: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.pop("newbot_prompt", None)
        await update.effective_message.reply_text("Describe the Telegram bot you want to build.")
        return NEW_PROMPT

    async def newbot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("Send a text prompt describing the child bot.")
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
        await update.effective_message.reply_text("Thinking through the bot requirements...")
        decision = service.plan_new_bot(prompt, answers, force_code=force_code)
        if decision.needs_questions:
            if force_code:
                await update.effective_message.reply_text(
                    "I hit the follow-up limit and the AI still was not ready to generate safely. "
                    "Try /newbot again with the missing details included up front."
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(decision)
            await update.effective_message.reply_text(format_ai_questions(decision, len(answers) + 1))
            return NEW_FOLLOWUP

        context.user_data["newbot_decision"] = decision
        await update.effective_message.reply_text(
            (decision.message + "\n\n" if decision.message else "")
            + "Now paste the child bot token from @BotFather. Create a separate bot there with /newbot if needed."
        )
        return NEW_TOKEN

    async def newbot_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await update.effective_message.reply_text("Reply with your answers, or use /cancel.")
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
            await update.effective_message.reply_text("The AI plan expired. Use /newbot to start again.")
            context.user_data.clear()
            return ConversationHandler.END
        await update.effective_message.reply_text("Validating generated code and launching the child bot...")
        result = await service.create_bot_from_decision(user_id, _chat_id(update), prompt, token_text, decision)
        logger.info("Create bot result: user_id=%s ok=%s bot_id=%s", user_id, result.ok, result.bot_id)
        await update.effective_message.reply_text(result.message)
        context.user_data.clear()
        return ConversationHandler.END

    async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        rows = service.list_bots_for(user_id)
        logger.info("Command /bots: user_id=%s count=%s", user_id, len(rows))
        await update.effective_message.reply_text(format_bot_list(rows))

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /status: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            rows = service.list_bots_for(user_id)
            await update.effective_message.reply_text(format_bot_list(rows))
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return
        await update.effective_message.reply_text(format_bot_status(row))

    async def tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        command = update.effective_message.text.split(maxsplit=1)[0] if update.effective_message.text else "/tail"
        bot_id, limit, error = parse_tail_args(context.args)
        logger.info("Command %s: user_id=%s bot_id=%s limit=%s", command, user_id, bot_id, limit)
        if error is not None:
            await update.effective_message.reply_text(error)
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return
        await update.effective_message.reply_text(
            f"<pre>{escape(format_logs(db.get_logs(bot_id, limit)))}</pre>",
            parse_mode=ParseMode.HTML,
        )

    async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /source: user_id=%s bot_id=%s", user_id, bot_id)
        if not service.is_owner(user_id):
            await update.effective_message.reply_text("Raw source is owner-only. Use /edit <id> to change bots with a prompt.")
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
            await update.effective_message.reply_text("Usage: /stop <id>")
            return
        result = await service.stop_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /restart: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /restart <id>")
            return
        result = await service.restart_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /delete: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /delete <id>")
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
            await update.effective_message.reply_text("Usage: /revise <id>")
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return ConversationHandler.END
        context.user_data["revise_bot_id"] = bot_id
        await update.effective_message.reply_text("Send the new prompt for this bot.")
        return REVISE_PROMPT

    async def revise_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["revise_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("Send a text prompt for the revision.")
            return REVISE_PROMPT
        logger.info("Received revise prompt: user_id=%s bot_id=%s chars=%s", user_id, bot_id, len(prompt))
        await update.effective_message.reply_text("Regenerating raw Python and restarting the child bot...")
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
            await update.effective_message.reply_text("Usage: /edit <id>")
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return ConversationHandler.END
        context.user_data["edit_bot_id"] = bot_id
        await update.effective_message.reply_text(
            "Describe what you want to change. "
            "Example: add a /help command, or make the bot remember birthdays. "
            "The AI may ask follow-up questions. Use /cancel to abort."
        )
        return EDIT_PROMPT

    async def edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = int(context.user_data["edit_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("Describe the change you want.")
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
        await update.effective_message.reply_text("Thinking through the edit...")
        decision = service.plan_edit_bot(user_id, bot_id, prompt, answers, force_code=force_code)
        if isinstance(decision, OperationResult):
            await update.effective_message.reply_text(decision.message)
            context.user_data.clear()
            return ConversationHandler.END
        if decision.needs_questions:
            if force_code:
                await update.effective_message.reply_text(
                    "I hit the follow-up limit and the AI still was not ready to edit safely. "
                    "Try /edit again with the missing details included up front."
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["edit_pending_questions"] = question_texts(decision)
            await update.effective_message.reply_text(format_ai_questions(decision, len(answers) + 1))
            return EDIT_FOLLOWUP

        await update.effective_message.reply_text("Validating edited code and restarting the child bot...")
        result = await service.edit_bot_from_decision(user_id, bot_id, prompt, decision)
        logger.info("Edit bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok)
        await update.effective_message.reply_text(result.message)
        context.user_data.clear()
        return ConversationHandler.END

    async def edit_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await update.effective_message.reply_text("Reply with your answers, or use /cancel.")
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
                BotCommand("newbot", "Create and launch a child bot"),
                BotCommand("bots", "List your child bots"),
                BotCommand("status", "Show bot status, or list all bots"),
                BotCommand("tail", "Show child bot logs"),
                BotCommand("edit", "Change a child bot with prompts"),
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
        exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
        logger.error("Unhandled Telegram handler error: update=%r", update, exc_info=exc_info)
        if isinstance(update, Update) and update.effective_message is not None:
            try:
                await update.effective_message.reply_text("Something went wrong. Check BotMother logs.")
            except Exception:
                logger.exception("Failed to send handler error message")

    newbot_conv = ConversationHandler(
        entry_points=[CommandHandler("newbot", newbot)],
        states={
            NEW_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newbot_prompt)],
            NEW_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, newbot_followup)],
            NEW_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, newbot_token)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    revise_conv = ConversationHandler(
        entry_points=[CommandHandler("revise", revise)],
        states={
            REVISE_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, revise_prompt)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit)],
        states={
            EDIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_prompt)],
            EDIT_FOLLOWUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_followup)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(newbot_conv)
    application.add_handler(revise_conv)
    application.add_handler(edit_conv)
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
