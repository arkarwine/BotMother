from __future__ import annotations

from html import escape
from typing import Any

from .db import Database
from .service import BotService


NEW_PROMPT, NEW_TOKEN, REVISE_PROMPT = range(3)


def parse_bot_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        value = int(args[0])
    except ValueError:
        return None
    return value if value > 0 else None


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
        from telegram import Update
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
        _remember_user(db, update)
        await update.effective_message.reply_text(
            "BotMother is ready.\n"
            "Use /newbot to build a child bot.\n"
            "Use /bots to list your bots."
        )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def newbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        context.user_data.pop("newbot_prompt", None)
        await update.effective_message.reply_text("Describe the Telegram bot you want to build.")
        return NEW_PROMPT

    async def newbot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("Send a text prompt describing the child bot.")
            return NEW_PROMPT
        context.user_data["newbot_prompt"] = prompt
        await update.effective_message.reply_text(
            "Now paste the child bot token from @BotFather. "
            "Create a separate bot there with /newbot if you do not have one yet."
        )
        return NEW_TOKEN

    async def newbot_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        prompt = context.user_data.get("newbot_prompt", "")
        token_text = (update.effective_message.text or "").strip()
        await update.effective_message.reply_text("Generating raw Python and launching the child bot...")
        result = await service.create_bot(user_id, _chat_id(update), prompt, token_text)
        await update.effective_message.reply_text(result.message)
        context.user_data.pop("newbot_prompt", None)
        return ConversationHandler.END

    async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        await update.effective_message.reply_text(format_bot_list(service.list_bots_for(user_id)))

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /status <id>")
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return
        await update.effective_message.reply_text(format_bot_status(row))

    async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /logs <id>")
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await update.effective_message.reply_text("Bot not found, or you do not have access.")
            return
        await update.effective_message.reply_text(
            f"<pre>{escape(format_logs(db.get_logs(bot_id, 30)))}</pre>",
            parse_mode=ParseMode.HTML,
        )

    async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /stop <id>")
            return
        result = await service.stop_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /restart <id>")
            return
        result = await service.restart_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        if bot_id is None:
            await update.effective_message.reply_text("Usage: /delete <id>")
            return
        result = await service.delete_bot(user_id, bot_id)
        await update.effective_message.reply_text(result.message)

    async def killall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        result = await service.kill_all(user_id)
        await update.effective_message.reply_text(result.message)

    async def revise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
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
        await update.effective_message.reply_text("Regenerating raw Python and restarting the child bot...")
        result = await service.revise_bot(user_id, bot_id, prompt)
        await update.effective_message.reply_text(result.message)
        context.user_data.pop("revise_bot_id", None)
        return ConversationHandler.END

    async def post_init(application) -> None:
        await service.runner.restore_running_bots()

    async def post_shutdown(application) -> None:
        await service.runner.stop_all(mark_stopped=False)

    newbot_conv = ConversationHandler(
        entry_points=[CommandHandler("newbot", newbot)],
        states={
            NEW_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newbot_prompt)],
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
    application.add_handler(CommandHandler("bots", bots))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("logs", logs))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CommandHandler("killall", killall))
    application.add_handler(CommandHandler("cancel", cancel))
    return application
