from __future__ import annotations

import logging
import re
import uuid
from html import escape
from typing import Any

from .ai import MAX_FOLLOWUP_ROUNDS, AIDecision, AIReadinessDecision
from .db import Database
from .localization import normalize_locale, t
from .service import BotService, OperationResult

logger = logging.getLogger(__name__)


(
    NEW_PROMPT,
    NEW_FOLLOWUP,
    NEW_TOKEN,
    REVISE_PROMPT,
    EDIT_PROMPT,
    EDIT_FOLLOWUP,
    ASK_PROMPT,
) = range(7)
BOT_NOT_FOUND_TEXT = t("bot_not_found")

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

HELP_TEXT = t("help.text")

HELP_CATEGORY_TEXTS = {
    "create": t("help.create"),
    "manage": t("help.manage"),
    "ops": t("help.ops"),
    "utils": t("help.utils"),
    "fallback": t("help.fallback"),
}

EXAMPLES_TEXT = t("examples.text")

USER_LOCALE_CACHE: dict[int, str] = {}

BOT_TEMPLATE_PROMPTS = {
    "shop": "Mode: e-commerce shop bot. Include product catalog, cart, checkout flow, payment/contact instructions, order storage, and admin order notifications/tools when details are provided.",
    "booking": "Mode: booking bot. Include service selection, date/time collection, customer contact details, booking storage, admin review, and confirmation/cancel flows.",
    "support": "Mode: support/helpdesk bot. Include ticket creation, categories, status tracking, admin replies, FAQ/help, and user-friendly escalation flow.",
    "quiz": "Mode: quiz bot. Include question flow, scoring, leaderboard, admin question management when useful, persistence, and replay/help controls.",
    "channel": "Mode: channel assistant bot. Include drafting, templates, subscriber/admin controls, broadcast confirmation, and safe preview flows.",
    "other": "Mode: custom bot. Follow the user's prompt and apply sensible complete-bot defaults.",
}


def apply_bot_template(prompt: str, template: str | None) -> str:
    mode = template if template in BOT_TEMPLATE_PROMPTS else "other"
    return f"{BOT_TEMPLATE_PROMPTS[mode]}\n\nUser request:\n{prompt.strip()}"


def parse_bot_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        value = int(args[0])
    except ValueError:
        return None
    return value if value > 0 else None


def parse_tail_args(
    args: list[str], default_limit: int = 30, max_limit: int = 100
) -> tuple[int | None, int, str | None]:
    bot_id = parse_bot_id(args)
    if bot_id is None:
        return (
            None,
            default_limit,
            t("choose.logs_no_id"),
        )

    if len(args) < 2:
        return bot_id, default_limit, None

    try:
        limit = int(args[1])
    except ValueError:
        return bot_id, default_limit, t("tail.bad_limit")

    if limit < 1:
        return bot_id, default_limit, t("tail.min_limit")
    return bot_id, min(limit, max_limit), None


def parse_ask_args(args: list[str]) -> tuple[int | None, str, str | None]:
    bot_id = parse_bot_id(args)
    if bot_id is None:
        return (
            None,
            "",
            t("choose.ask_no_id"),
        )
    return bot_id, " ".join(args[1:]).strip(), None


def status_badge(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '•')} {status}"


def row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def owner_label(row: Any) -> str:
    username = row_value(row, "owner_username")
    if username:
        return f"@{username}"
    first_name = str(row_value(row, "owner_first_name", "") or "").strip()
    last_name = str(row_value(row, "owner_last_name", "") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return full_name or "Owner"


def bot_username_label(row: Any) -> str | None:
    username = str(row_value(row, "bot_username", "") or "").strip().lstrip("@")
    if not username:
        return None
    return f"@{username}"


def bot_title(row: Any) -> str:
    return str(row["name"])


def format_bot_list(rows: list[Any], locale: str = "en") -> str:
    if not rows:
        return t("empty.bots", locale=locale)
    lines = [t("bots.title", locale=locale)]
    for row in rows:
        line = f"{escape(status_badge(row['status']))}  <b>{escape(bot_title(row))}</b>"
        username = bot_username_label(row)
        if username:
            line += f"\n<i>{escape(username)}</i>"
        lines.append(line)
    return "\n".join(lines)


def format_bot_status(row: Any) -> str:
    return (
        f"<b>📦 {escape(bot_title(row))}</b>\n\n"
        f"<b>Status</b>\n{escape(status_badge(row['status']))}\n\n"
        f"<b>Owner</b>\n{escape(owner_label(row))}"
    )


def format_logs(rows: list[Any], locale: str = "en") -> str:
    if not rows:
        return t("logs.empty", locale=locale)
    lines = []
    for row in rows:
        line = str(row["line"]).replace("\n", " ")
        lines.append(f"[{row['stream']}] {line}")
    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[-3500:]
    return text


def compact_bot_label(row: Any) -> str:
    name = bot_title(row)
    if len(name) > 34:
        name = name[:31] + "..."
    return f"{STATUS_EMOJI.get(row['status'], '•')} {name}"


def help_category_text(category: str, locale: str = "en") -> str:
    if category in {"create", "manage", "ops", "utils", "fallback"}:
        return t(f"help.{category}", locale=locale)
    return t("help.text", locale=locale)


def format_result_html(text: str) -> str:
    lines = text.splitlines()
    first_content_index = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_content_index is None:
        return ""

    formatted: list[str] = []
    for index, line in enumerate(lines):
        escaped = escape(line)
        if index == first_content_index:
            formatted.append(f"<b>{escaped}</b>")
        else:
            formatted.append(escaped)
    return "\n".join(formatted)


def chunk_text(text: str, chunk_size: int = 3200) -> list[str]:
    if not text:
        return [""]
    return [
        text[index : index + chunk_size] for index in range(0, len(text), chunk_size)
    ]


def question_texts(decision: AIDecision | AIReadinessDecision) -> list[str]:
    return [question.question for question in decision.questions]


def format_ai_questions(decision: AIDecision | AIReadinessDecision) -> str:
    if decision.needs_questions and not decision.questions:
        return "I need a little more detail before building."
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
    preferred_locale = db.get_user_locale(user_id)
    if preferred_locale:
        USER_LOCALE_CACHE[user_id] = normalize_locale(preferred_locale)
    return user_id


def locale_for_update(update: Any) -> str:
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if user_id is not None and int(user_id) in USER_LOCALE_CACHE:
        return USER_LOCALE_CACHE[int(user_id)]
    return normalize_locale(getattr(user, "language_code", None))


def tr(update: Any, key: str, **values: Any) -> str:
    return t(key, locale=locale_for_update(update), **values)


def bot_not_found_text(update: Any) -> str:
    return tr(update, "bot_not_found")


def user_context_for_ai(update: Any) -> str:
    user = update.effective_user
    chat = update.effective_chat
    username = getattr(user, "username", None) or ""
    full_name = " ".join(
        part
        for part in (
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        )
        if part
    ).strip()
    lines = [
        f"Telegram user ID: {getattr(user, 'id', '')}",
        f"Username: @{username}" if username else "Username: none",
        f"Full name: {full_name or 'unknown'}",
        f"First name: {getattr(user, 'first_name', '') or 'unknown'}",
        f"Last name: {getattr(user, 'last_name', '') or 'none'}",
        f"Language code: {getattr(user, 'language_code', None) or 'unknown'}",
        f"BotMother locale: {locale_for_update(update)}",
        f"Is bot: {getattr(user, 'is_bot', False)}",
    ]
    if chat is not None:
        lines.extend(
            [
                f"Chat ID: {getattr(chat, 'id', '')}",
                f"Chat type: {getattr(chat, 'type', '') or 'unknown'}",
                f"Chat title: {getattr(chat, 'title', None) or 'none'}",
                f"Chat username: @{getattr(chat, 'username', '')}"
                if getattr(chat, "username", None)
                else "Chat username: none",
            ]
        )
    return "\n".join(lines)


def format_user_profile(update: Any, is_owner: bool) -> str:
    user = update.effective_user
    chat = update.effective_chat
    username = getattr(user, "username", None)
    chat_username = getattr(chat, "username", None) if chat is not None else None
    rows = [
        ("User ID", str(getattr(user, "id", ""))),
        ("Username", f"@{username}" if username else "None"),
        ("First name", getattr(user, "first_name", None) or "None"),
        ("Last name", getattr(user, "last_name", None) or "None"),
        ("Language code", getattr(user, "language_code", None) or "Unknown"),
        ("BotMother locale", locale_for_update(update)),
        ("Is bot", str(getattr(user, "is_bot", False))),
        ("Is premium", str(getattr(user, "is_premium", False))),
        ("Owner access", "Yes" if is_owner else "No"),
        ("Chat ID", str(getattr(chat, "id", "")) if chat is not None else "Unknown"),
        ("Chat type", getattr(chat, "type", None) or "Unknown"),
        ("Chat title", getattr(chat, "title", None) or "None"),
        ("Chat username", f"@{chat_username}" if chat_username else "None"),
    ]
    lines = ["<b>🪪 Your Telegram Profile</b>", ""]
    for label, value in rows:
        lines.append(f"<b>{escape(label)}</b>\n<code>{escape(value)}</code>")
        lines.append("")
    return "\n".join(lines).strip()


def build_application(token: str, db: Database, service: BotService):
    try:
        from telegram import (
            BotCommand,
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            ReplyKeyboardMarkup,
            ReplyKeyboardRemove,
            Update,
        )
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
        raise RuntimeError(
            "python-telegram-bot is not installed. Run: pip install -r requirements.txt"
        ) from exc

    keyboard_button_keys = [
        "button.new_bot",
        "button.my_bots",
        "button.examples",
        "button.help",
        "button.profile",
        "button.health",
        "button.language",
        "button.status",
        "button.ask_bot",
        "button.edit_bot",
        "button.revise",
        "button.logs",
        "button.restart",
        "button.stop",
        "button.delete",
        "button.auto_fix",
        "button.cancel",
    ]
    keyboard_button_labels = {
        t(key, locale=locale)
        for locale in ("en", "my")
        for key in keyboard_button_keys
    } | {"❔ Help"}
    keyboard_button_pattern = (
        "^(" + "|".join(re.escape(label) for label in sorted(keyboard_button_labels, key=len, reverse=True)) + ")$"
    )

    remove_keyboard = ReplyKeyboardRemove()

    def keyboard_for_rows(rows: list[Any], locale: str = "en"):
        return main_reply_keyboard(locale)

    def keyboard_for_user(user_id: int):
        locale = db.get_user_locale(user_id) if user_id else None
        return main_reply_keyboard(normalize_locale(locale))

    def flow_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("button.cancel", locale=locale), callback_data="nav:cancel")]]
        )

    def ai_response_keyboard(flow: str, locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("button.reprompt", locale=locale),
                        callback_data=f"reprompt:{flow}",
                    ),
                    InlineKeyboardButton(
                        t("button.cancel", locale=locale), callback_data="nav:cancel"
                    ),
                ]
            ]
        )

    def template_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("template.shop", locale=locale), callback_data="template:shop"),
                    InlineKeyboardButton(t("template.booking", locale=locale), callback_data="template:booking"),
                ],
                [
                    InlineKeyboardButton(t("template.support", locale=locale), callback_data="template:support"),
                    InlineKeyboardButton(t("template.quiz", locale=locale), callback_data="template:quiz"),
                ],
                [
                    InlineKeyboardButton(t("template.channel", locale=locale), callback_data="template:channel"),
                    InlineKeyboardButton(t("template.other", locale=locale), callback_data="template:other"),
                ],
                [InlineKeyboardButton(t("button.cancel", locale=locale), callback_data="nav:cancel")],
            ]
        )

    def main_reply_keyboard(locale: str = "en"):
        return ReplyKeyboardMarkup(
            [
                [
                    t("button.new_bot", locale=locale),
                    t("button.my_bots", locale=locale),
                ],
                [
                    t("button.examples", locale=locale),
                    t("button.help", locale=locale),
                ],
                [
                    t("button.language", locale=locale),
                    t("button.profile", locale=locale),
                ],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    def home_menu_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [
                    InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples"),
                    InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help"),
                ],
                [
                    InlineKeyboardButton(t("button.profile", locale=locale), callback_data="nav:id"),
                    InlineKeyboardButton(t("button.health", locale=locale), callback_data="nav:health"),
                ],
                [
                    InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language"),
                ],
            ]
        )

    def help_menu_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [
                    InlineKeyboardButton(t("button.create", locale=locale), callback_data="help:create"),
                    InlineKeyboardButton(t("button.manage", locale=locale), callback_data="help:manage"),
                ],
                [
                    InlineKeyboardButton(t("button.operations", locale=locale), callback_data="help:ops"),
                    InlineKeyboardButton(t("button.utilities", locale=locale), callback_data="help:utils"),
                ],
                [
                    InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language"),
                ],
                [
                    InlineKeyboardButton(
                        t("button.command_fallbacks", locale=locale), callback_data="help:fallback"
                    )
                ],
                [InlineKeyboardButton(t("button.home", locale=locale), callback_data="nav:home")],
            ]
        )

    def help_category_keyboard(category: str, locale: str = "en"):
        if category == "create":
            rows = [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples"),
                ],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "manage":
            rows = [
                [
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                    InlineKeyboardButton(t("button.status", locale=locale), callback_data="pick:status"),
                ],
                [
                    InlineKeyboardButton(t("button.ask_bot", locale=locale), callback_data="pick:ask"),
                    InlineKeyboardButton(t("button.edit_bot", locale=locale), callback_data="pick:edit"),
                ],
                [InlineKeyboardButton(t("button.revise", locale=locale), callback_data="pick:revise")],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "ops":
            rows = [
                [
                    InlineKeyboardButton(t("button.logs", locale=locale), callback_data="pick:tail"),
                    InlineKeyboardButton(t("button.restart", locale=locale), callback_data="pick:restart"),
                ],
                [
                    InlineKeyboardButton(t("button.stop", locale=locale), callback_data="pick:stop"),
                    InlineKeyboardButton(
                        t("button.delete", locale=locale), callback_data="pick:delete_confirm"
                    ),
                ],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "utils":
            rows = [
                [
                    InlineKeyboardButton(t("button.profile", locale=locale), callback_data="nav:id"),
                    InlineKeyboardButton(t("button.health", locale=locale), callback_data="nav:health"),
                ],
                [InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language")],
                [InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples")],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "fallback":
            rows = [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [
                    InlineKeyboardButton(t("button.status", locale=locale), callback_data="pick:status"),
                    InlineKeyboardButton(t("button.logs", locale=locale), callback_data="pick:tail"),
                ],
                [
                    InlineKeyboardButton(t("button.ask_bot", locale=locale), callback_data="pick:ask"),
                    InlineKeyboardButton(t("button.edit_bot", locale=locale), callback_data="pick:edit"),
                ],
                [
                    InlineKeyboardButton(t("button.revise", locale=locale), callback_data="pick:revise"),
                    InlineKeyboardButton(t("button.restart", locale=locale), callback_data="pick:restart"),
                ],
                [
                    InlineKeyboardButton(t("button.stop", locale=locale), callback_data="pick:stop"),
                    InlineKeyboardButton(
                        t("button.delete", locale=locale), callback_data="pick:delete_confirm"
                    ),
                ],
                [
                    InlineKeyboardButton(t("button.profile", locale=locale), callback_data="nav:id"),
                    InlineKeyboardButton(t("button.health", locale=locale), callback_data="nav:health"),
                ],
                [InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language")],
                [
                    InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples"),
                    InlineKeyboardButton(t("button.cancel", locale=locale), callback_data="nav:cancel"),
                ],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help")],
            ]
        return InlineKeyboardMarkup(rows)

    def language_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.language_en", locale=locale), callback_data="lang:en"),
                    InlineKeyboardButton(t("button.language_my", locale=locale), callback_data="lang:my"),
                ],
                [InlineKeyboardButton(t("button.home", locale=locale), callback_data="nav:home")],
            ]
        )

    def empty_state_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot")],
                [
                    InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples"),
                    InlineKeyboardButton(t("button.help", locale=locale), callback_data="nav:help"),
                ],
            ]
        )

    def bot_actions_keyboard(bot_id: int, locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.status", locale=locale), callback_data=f"status:{bot_id}"),
                    InlineKeyboardButton(t("button.logs", locale=locale), callback_data=f"tail:{bot_id}"),
                ],
                [
                    InlineKeyboardButton(t("button.validation", locale=locale), callback_data=f"validation:{bot_id}"),
                    InlineKeyboardButton(t("button.auto_fix", locale=locale), callback_data=f"autofix:{bot_id}"),
                ],
                [
                    InlineKeyboardButton(t("button.ask", locale=locale), callback_data=f"ask:{bot_id}"),
                    InlineKeyboardButton(t("button.edit", locale=locale), callback_data=f"edit:{bot_id}"),
                ],
                [InlineKeyboardButton(t("button.revise", locale=locale), callback_data=f"revise:{bot_id}")],
                [
                    InlineKeyboardButton(
                        t("button.restart", locale=locale), callback_data=f"restart:{bot_id}"
                    ),
                    InlineKeyboardButton(t("button.stop", locale=locale), callback_data=f"stop:{bot_id}"),
                ],
                [
                    InlineKeyboardButton(
                        t("button.delete", locale=locale), callback_data=f"delete_confirm:{bot_id}"
                    )
                ],
                [InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots")],
            ]
        )

    def bots_keyboard(rows: list[Any], action: str = "status", show_back: bool = False, locale: str = "en"):
        if not rows:
            return empty_state_keyboard(locale)
        buttons = [
            [
                InlineKeyboardButton(
                    compact_bot_label(row), callback_data=f"{action}:{row['id']}"
                )
            ]
            for row in rows[:20]
        ]
        if show_back:
            buttons.append([InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots")])
        return InlineKeyboardMarkup(buttons)

    def is_message_not_modified(exc: Exception) -> bool:
        return "message is not modified" in str(exc).lower()

    async def reply_html(message, text: str, reply_markup=None):
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )

    async def edit_message_html(message, text: str, reply_markup=None) -> None:
        can_edit_markup = reply_markup is None or isinstance(
            reply_markup, InlineKeyboardMarkup
        )
        if can_edit_markup:
            try:
                await message.edit_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return
            except Exception as exc:
                if is_message_not_modified(exc):
                    return
                logger.debug(
                    "Could not edit bot message; sending a new message", exc_info=True
                )
        await reply_html(message, text, reply_markup=reply_markup)

    async def edit_message_plain(message, text: str, reply_markup=None) -> None:
        can_edit_markup = reply_markup is None or isinstance(
            reply_markup, InlineKeyboardMarkup
        )
        if can_edit_markup:
            try:
                await message.edit_text(text=text, reply_markup=reply_markup)
                return
            except Exception as exc:
                if is_message_not_modified(exc):
                    return
                logger.debug(
                    "Could not edit bot message; sending a new message", exc_info=True
                )
        await message.reply_text(text, reply_markup=reply_markup)

    async def edit_or_reply_html(update: Update, text: str, reply_markup=None) -> None:
        query = update.callback_query
        can_edit_markup = reply_markup is None or isinstance(
            reply_markup, InlineKeyboardMarkup
        )
        if (
            query is not None
            and update.effective_message is not None
            and can_edit_markup
        ):
            try:
                await query.edit_message_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return
            except Exception as exc:
                if is_message_not_modified(exc):
                    return
                logger.debug(
                    "Could not edit callback message; sending a new message",
                    exc_info=True,
                )
        await reply_html(update.effective_message, text, reply_markup=reply_markup)

    async def reply_result(message, text: str, reply_markup=None) -> None:
        await reply_html(message, format_result_html(text), reply_markup=reply_markup)

    async def edit_message_result(message, text: str, reply_markup=None) -> None:
        await edit_message_html(
            message, format_result_html(text), reply_markup=reply_markup
        )

    async def edit_or_reply_result(update: Update, text: str, reply_markup=None) -> None:
        await edit_or_reply_html(
            update, format_result_html(text), reply_markup=reply_markup
        )

    async def reply_home(message, text: str, locale: str = "en") -> None:
        await reply_html(message, text, reply_markup=main_reply_keyboard(locale))

    async def restart_expired_flow(
        update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, title: str
    ) -> int:
        context.user_data.clear()
        await reply_html(
            update.effective_message,
            tr(update, "flow.expired"),
            reply_markup=keyboard_for_user(_remember_user(db, update)),
        )
        await choose_bot_for_action(update, action, title)
        return ConversationHandler.END

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        locale = locale_for_update(update)
        logger.info("Command /start: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await reply_home(update.effective_message, t("help.text", locale=locale), locale)
        await reply_html(
            update.effective_message,
            t("home.title", locale=locale),
            reply_markup=home_menu_keyboard(locale),
        )

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        locale = locale_for_update(update)
        logger.info("Command /help: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await reply_home(update.effective_message, t("help.text", locale=locale), locale)
        await reply_html(
            update.effective_message,
            t("help.menu_title", locale=locale),
            reply_markup=help_menu_keyboard(locale),
        )

    async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        locale = locale_for_update(update)
        logger.info(
            "Command /language: user_id=%s chat_id=%s", user_id, _chat_id(update)
        )
        await reply_html(
            update.effective_message,
            t("language.title", locale=locale),
            reply_markup=language_keyboard(locale),
        )

    async def examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info(
            "Command /examples: user_id=%s chat_id=%s", user_id, _chat_id(update)
        )
        await reply_html(
            update.effective_message,
            tr(update, "examples.text"),
            reply_markup=keyboard_for_user(user_id),
        )

    async def identity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        chat_id = _chat_id(update)
        logger.info("Command /id: user_id=%s chat_id=%s", user_id, chat_id)
        await reply_html(
            update.effective_message,
            format_user_profile(update, service.is_owner(user_id)),
            reply_markup=keyboard_for_user(user_id),
        )

    async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        locale = locale_for_update(update)
        rows = service.list_bots_for(user_id)
        active_count = len(service.runner.active)
        running_visible = sum(1 for row in rows if row["status"] == "running")
        logger.info(
            "Command /health: user_id=%s visible_bots=%s active=%s",
            user_id,
            len(rows),
            active_count,
        )
        scope = "all bots" if service.is_owner(user_id) else "your bots"
        await reply_html(
            update.effective_message,
            t("health.title", locale=locale)
            + "\n\n"
            f"<b>Manager</b>\nonline\n\n"
            f"<b>Scope</b>\n{escape(scope)}\n\n"
            f"<b>Visible bots</b>\n<code>{len(rows)}</code>\n\n"
            f"<b>Running in DB</b>\n<code>{running_visible}</code>\n\n"
            f"<b>Active child processes</b>\n<code>{active_count}</code>",
            reply_markup=keyboard_for_rows(rows, locale),
        )

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /cancel: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.clear()
        await edit_or_reply_html(
            update,
            tr(update, "cancel.done"),
            reply_markup=home_menu_keyboard(locale_for_update(update))
            if update.callback_query is not None
            else keyboard_for_user(user_id),
        )
        return ConversationHandler.END

    async def newbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        logger.info("Command /newbot: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.pop("newbot_prompt", None)
        context.user_data["newbot_user_context"] = user_context_for_ai(update)
        await reply_html(
            update.effective_message,
            tr(update, "newbot.choose_template"),
            reply_markup=template_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def newbot_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        _remember_user(db, update)
        template = (query.data if query else "template:other").split(":", 1)[1]
        if template not in BOT_TEMPLATE_PROMPTS:
            template = "other"
        context.user_data["newbot_template"] = template
        await edit_or_reply_html(
            update,
            tr(update, f"newbot.template_{template}"),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            tr(update, "button.examples"), callback_data="nav:examples"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            tr(update, "button.cancel"), callback_data="nav:cancel"
                        )
                    ],
                ]
            ),
        )
        return NEW_PROMPT

    async def newbot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        raw_prompt = (update.effective_message.text or "").strip()
        if not raw_prompt:
            await reply_html(
                update.effective_message,
                tr(update, "newbot.empty_prompt"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
            return NEW_PROMPT
        template = str(context.user_data.get("newbot_template", "other"))
        prompt = apply_bot_template(raw_prompt, template)
        logger.info(
            "Received newbot prompt: user_id=%s template=%s chars=%s",
            user_id,
            template,
            len(raw_prompt),
        )
        context.user_data["newbot_prompt"] = prompt
        context.user_data["newbot_answers"] = []
        context.user_data.pop("newbot_decision", None)
        return await continue_newbot_planning(update, context)

    async def continue_newbot_planning(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        user_id = _remember_user(db, update)
        prompt = context.user_data.get("newbot_prompt", "")
        answers = context.user_data.get("newbot_answers", [])
        force_code = len(answers) >= MAX_FOLLOWUP_ROUNDS
        progress_message = await reply_html(
            update.effective_message, tr(update, "newbot.thinking")
        )
        decision = service.plan_new_bot(
            prompt,
            answers,
            force_code=force_code,
            user_context=str(context.user_data.get("newbot_user_context", "")),
        )
        if decision.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "newbot.more_detail"),
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(decision)
            await edit_message_html(
                progress_message,
                format_ai_questions(decision),
                reply_markup=ai_response_keyboard("newbot", locale_for_update(update)),
            )
            return NEW_FOLLOWUP

        readiness = service.check_new_bot_readiness(
            prompt,
            answers,
            decision,
            user_context=str(context.user_data.get("newbot_user_context", "")),
        )
        if readiness.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "newbot.missing_launch_detail"),
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(readiness)
            await edit_message_html(
                progress_message,
                format_ai_questions(readiness),
                reply_markup=ai_response_keyboard("newbot", locale_for_update(update)),
            )
            return NEW_FOLLOWUP

        context.user_data["newbot_decision"] = decision
        await edit_message_html(
            progress_message,
            (escape(decision.message.strip()) + "\n\n" if decision.message else "")
            + tr(update, "newbot.token"),
            reply_markup=ai_response_keyboard("newbot", locale_for_update(update)),
        )
        return NEW_TOKEN

    async def newbot_followup(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        _remember_user(db, update)
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await reply_html(
                update.effective_message,
                tr(update, "followup.empty"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
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

    async def newbot_reprompt(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        logger.info("Re-prompt newbot: user_id=%s", user_id)
        user_context = user_context_for_ai(update)
        context.user_data.clear()
        context.user_data["newbot_user_context"] = user_context
        await edit_or_reply_html(
            update,
            tr(update, "newbot.choose_template"),
            reply_markup=template_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def newbot_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        prompt = context.user_data.get("newbot_prompt", "")
        decision = context.user_data.get("newbot_decision")
        token_text = (update.effective_message.text or "").strip()
        logger.info(
            "Received newbot token; creating bot: user_id=%s prompt_chars=%s",
            user_id,
            len(prompt),
        )
        if not isinstance(decision, AIDecision):
            await reply_html(
                update.effective_message,
                tr(update, "newbot.expired"),
                reply_markup=keyboard_for_user(user_id),
            )
            context.user_data.clear()
            return ConversationHandler.END
        progress_message = await reply_html(
            update.effective_message,
            tr(update, "newbot.launching"),
        )
        result = await service.create_bot_from_decision(
            user_id,
            _chat_id(update),
            prompt,
            token_text,
            decision,
            user_context=str(context.user_data.get("newbot_user_context", "")),
        )
        logger.info(
            "Create bot result: user_id=%s ok=%s bot_id=%s",
            user_id,
            result.ok,
            result.bot_id,
        )
        result_markup = (
            bot_actions_keyboard(result.bot_id, locale_for_update(update))
            if result.bot_id
            else None
        )
        await edit_message_result(
            progress_message,
            result.message,
            reply_markup=result_markup,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        await service.refresh_missing_bot_usernames_for(user_id)
        rows = service.list_bots_for(user_id)
        logger.info("Command /bots: user_id=%s count=%s", user_id, len(rows))
        await reply_html(
            update.effective_message,
            format_bot_list(rows, locale_for_update(update)),
            reply_markup=bots_keyboard(rows, locale=locale_for_update(update)),
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /status: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await service.refresh_missing_bot_usernames_for(user_id)
            rows = service.list_bots_for(user_id)
            await reply_html(
                update.effective_message,
                format_bot_list(rows, locale_for_update(update)),
                reply_markup=bots_keyboard(rows, locale=locale_for_update(update)),
            )
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await reply_html(update.effective_message, bot_not_found_text(update))
            return
        dashboard = service.bot_dashboard(user_id, bot_id)
        await reply_html(
            update.effective_message,
            format_result_html(dashboard.message),
            reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
        )

    async def tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        command = (
            update.effective_message.text.split(maxsplit=1)[0]
            if update.effective_message.text
            else "/tail"
        )
        if not context.args:
            await choose_bot_for_action(update, "tail", tr(update, "choose.tail"))
            return
        bot_id, limit, error = parse_tail_args(context.args)
        logger.info(
            "Command %s: user_id=%s bot_id=%s limit=%s", command, user_id, bot_id, limit
        )
        if error is not None:
            await reply_html(update.effective_message, error)
            return
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await reply_html(update.effective_message, bot_not_found_text(update))
            return
        await update.effective_message.reply_text(
            f"<pre>{escape(format_logs(db.get_logs(bot_id, limit), locale_for_update(update)))}</pre>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_user(user_id),
        )

    async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        if not context.args:
            await choose_bot_for_action(update, "ask", tr(update, "choose.ask"))
            return ConversationHandler.END
        bot_id, question, error = parse_ask_args(context.args)
        logger.info(
            "Command /ask: user_id=%s bot_id=%s question_chars=%s",
            user_id,
            bot_id,
            len(question),
        )
        if error is not None:
            await reply_html(update.effective_message, error)
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        title = escape(bot_title(row))
        if question:
            return await answer_bot_question(update, context, user_id, bot_id, question)
        context.user_data["ask_bot_id"] = bot_id
        context.user_data["ask_user_context"] = user_context_for_ai(update)
        await reply_html(
            update.effective_message,
            tr(update, "ask.start_examples", title=title),
            reply_markup=flow_keyboard(locale_for_update(update)),
        )
        return ASK_PROMPT

    async def ask_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        if "ask_bot_id" not in context.user_data:
            return await restart_expired_flow(update, context, "ask", tr(update, "choose.ask"))
        bot_id = int(context.user_data["ask_bot_id"])
        question = (update.effective_message.text or "").strip()
        if not question:
            await reply_html(
                update.effective_message,
                tr(update, "ask.empty"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
            return ASK_PROMPT
        return await answer_bot_question(update, context, user_id, bot_id, question)

    async def answer_bot_question(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        bot_id: int,
        question: str,
    ) -> int:
        progress_message = await reply_html(
            update.effective_message,
            tr(update, "ask.reading"),
        )
        result = service.ask_bot(user_id, bot_id, question)
        logger.info(
            "Ask bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok
        )
        chunks = chunk_text(result.message)
        await edit_message_plain(progress_message, chunks[0])
        for index, chunk in enumerate(chunks[1:], start=1):
            reply_markup = (
                keyboard_for_user(user_id) if index == len(chunks) - 1 else None
            )
            await update.effective_message.reply_text(chunk, reply_markup=reply_markup)
        context.user_data.pop("ask_bot_id", None)
        return ConversationHandler.END

    async def choose_bot_for_action(
        update: Update, action: str, title: str, edit: bool = False
    ) -> None:
        user_id = _remember_user(db, update)
        rows = service.list_bots_for(user_id)
        logger.info(
            "Choose bot action: user_id=%s action=%s count=%s",
            user_id,
            action,
            len(rows),
        )
        if not rows:
            message = tr(update, "choose.none")
            if edit:
                await edit_or_reply_html(
                    update, message, reply_markup=empty_state_keyboard(locale_for_update(update))
                )
            else:
                await reply_html(
                    update.effective_message,
                    message,
                    reply_markup=empty_state_keyboard(locale_for_update(update)),
                )
            return
        message = f"<b>{escape(title)}</b>"
        reply_markup = bots_keyboard(
            rows, action, show_back=False, locale=locale_for_update(update)
        )
        if edit:
            await edit_or_reply_html(update, message, reply_markup=reply_markup)
        else:
            await reply_html(update.effective_message, message, reply_markup=reply_markup)

    async def button_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        button_key_by_label = {
            t(key, locale=locale): key
            for locale in ("en", "my")
            for key in keyboard_button_keys
        }
        button_key_by_label["❔ Help"] = "button.help"
        key = button_key_by_label.get(text)
        if key == "button.my_bots":
            await bots(update, context)
        elif key == "button.status":
            await choose_bot_for_action(update, "status", tr(update, "choose.status"))
        elif key == "button.ask_bot":
            await choose_bot_for_action(update, "ask", tr(update, "choose.ask"))
        elif key == "button.edit_bot":
            await choose_bot_for_action(update, "edit", tr(update, "choose.edit"))
        elif key == "button.revise":
            await choose_bot_for_action(update, "revise", tr(update, "choose.revise"))
        elif key == "button.logs":
            await choose_bot_for_action(update, "tail", tr(update, "choose.tail"))
        elif key == "button.restart":
            await choose_bot_for_action(update, "restart", tr(update, "choose.restart"))
        elif key == "button.stop":
            await choose_bot_for_action(update, "stop", tr(update, "choose.stop"))
        elif key == "button.delete":
            await choose_bot_for_action(update, "delete_confirm", tr(update, "choose.delete"))
        elif key == "button.profile":
            await identity(update, context)
        elif key == "button.health":
            await health(update, context)
        elif key == "button.examples":
            await examples(update, context)
        elif key == "button.help":
            await help_command(update, context)
        elif key == "button.language":
            await language_command(update, context)
        elif key == "button.new_bot":
            await newbot(update, context)
        elif key == "button.cancel":
            await cancel(update, context)

    async def newbot_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query is not None:
            await update.callback_query.answer()
        user_id = _remember_user(db, update)
        logger.info("Button New Bot: user_id=%s chat_id=%s", user_id, _chat_id(update))
        context.user_data.pop("newbot_prompt", None)
        context.user_data["newbot_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            tr(update, "newbot.choose_template"),
            reply_markup=template_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def ask_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await edit_or_reply_html(update, bot_not_found_text(update))
            return ConversationHandler.END
        context.user_data["ask_bot_id"] = bot_id
        context.user_data["ask_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            tr(update, "ask.start_short", title=escape(bot_title(row))),
        )
        return ASK_PROMPT

    async def edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await edit_or_reply_html(update, bot_not_found_text(update))
            return ConversationHandler.END
        context.user_data["edit_bot_id"] = bot_id
        context.user_data["edit_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            tr(update, "edit.start_short", title=escape(bot_title(row))),
        )
        return EDIT_PROMPT

    async def revise_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        bot_id = int((query.data if query else "").split(":", 1)[1])
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await edit_or_reply_html(update, bot_not_found_text(update))
            return ConversationHandler.END
        context.user_data["revise_bot_id"] = bot_id
        context.user_data["revise_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            tr(update, "revise.start_short", title=escape(bot_title(row))),
        )
        return REVISE_PROMPT

    async def button_callback(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        user_id = _remember_user(db, update)
        data = query.data
        logger.info("Callback button: user_id=%s data=%s", user_id, data)

        if data == "nav:home":
            await edit_or_reply_html(
                update,
                tr(update, "home.title"),
                reply_markup=home_menu_keyboard(locale_for_update(update)),
            )
            return
        if data == "nav:language":
            await edit_or_reply_html(
                update,
                tr(update, "language.title"),
                reply_markup=language_keyboard(locale_for_update(update)),
            )
            return
        if data.startswith("lang:"):
            selected_locale = normalize_locale(data.split(":", 1)[1])
            db.update_user_locale(user_id, selected_locale)
            USER_LOCALE_CACHE[user_id] = selected_locale
            await edit_or_reply_html(
                update,
                t(
                    "language.changed_my" if selected_locale == "my" else "language.changed",
                    locale=selected_locale,
                ),
                reply_markup=home_menu_keyboard(selected_locale),
            )
            if update.effective_message is not None:
                await reply_home(
                    update.effective_message,
                    t("home.title", locale=selected_locale),
                    selected_locale,
                )
            return
        if data == "nav:help":
            await edit_or_reply_html(
                update,
                tr(update, "help.menu_title"),
                reply_markup=help_menu_keyboard(locale_for_update(update)),
            )
            return
        if data == "nav:examples":
            await edit_or_reply_html(update, tr(update, "examples.text"))
            return
        if data == "nav:bots":
            await service.refresh_missing_bot_usernames_for(user_id)
            rows = service.list_bots_for(user_id)
            await edit_or_reply_html(
                update,
                format_bot_list(rows, locale_for_update(update)),
                reply_markup=bots_keyboard(rows, locale=locale_for_update(update)),
            )
            return
        if data == "nav:id":
            await edit_or_reply_html(
                update,
                format_user_profile(update, service.is_owner(user_id)),
                reply_markup=home_menu_keyboard(locale_for_update(update)),
            )
            return
        if data == "nav:cancel":
            context.user_data.clear()
            await edit_or_reply_html(
                update,
                tr(update, "cancel.done"),
                reply_markup=home_menu_keyboard(locale_for_update(update)),
            )
            return
        if data == "nav:health":
            rows = service.list_bots_for(user_id)
            active_count = len(service.runner.active)
            running_visible = sum(1 for row in rows if row["status"] == "running")
            await edit_or_reply_html(
                update,
                tr(update, "health.title")
                + "\n\n"
                f"<b>Visible bots</b>\n<code>{len(rows)}</code>\n\n"
                f"<b>Running in DB</b>\n<code>{running_visible}</code>\n\n"
                f"<b>Active child processes</b>\n<code>{active_count}</code>",
                reply_markup=home_menu_keyboard(locale_for_update(update)),
            )
            return
        if data.startswith("help:"):
            category = data.split(":", 1)[1]
            await edit_or_reply_html(
                update,
                help_category_text(category, locale_for_update(update)),
                reply_markup=help_category_keyboard(category, locale_for_update(update)),
            )
            return
        if data.startswith("pick:"):
            action = data.split(":", 1)[1]
            titles = {
                "status": tr(update, "choose.status"),
                "ask": tr(update, "choose.ask"),
                "edit": tr(update, "choose.edit"),
                "revise": tr(update, "choose.revise"),
                "tail": tr(update, "choose.tail"),
                "restart": tr(update, "choose.restart"),
                "stop": tr(update, "choose.stop"),
                "autofix": tr(update, "choose.autofix"),
                "delete_confirm": tr(update, "choose.delete"),
            }
            await choose_bot_for_action(
                update, action, titles.get(action, "Choose a bot:"), edit=True
            )
            return

        if ":" not in data:
            return
        action, raw_bot_id = data.split(":", 1)
        try:
            bot_id = int(raw_bot_id)
        except ValueError:
            return

        if not service.can_manage(user_id, bot_id):
            await edit_or_reply_html(update, bot_not_found_text(update))
            return

        if action == "status":
            dashboard = service.bot_dashboard(user_id, bot_id)
            await edit_or_reply_html(
                update,
                format_result_html(dashboard.message),
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "tail":
            await edit_or_reply_html(
                update,
                f"<pre>{escape(format_logs(db.get_logs(bot_id, 50), locale_for_update(update)))}</pre>",
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "validation":
            result = service.validation_report(user_id, bot_id)
            await edit_or_reply_result(
                update,
                result.message,
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "autofix":
            await edit_or_reply_html(update, tr(update, "autofix.running"))
            result = await service.auto_fix_bot(
                user_id, bot_id, user_context=user_context_for_ai(update)
            )
            await edit_or_reply_result(
                update,
                result.message,
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "restart":
            result = await service.restart_bot(user_id, bot_id)
            await edit_or_reply_result(
                update,
                result.message,
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "stop":
            result = await service.stop_bot(user_id, bot_id)
            await edit_or_reply_result(
                update,
                result.message,
                reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)),
            )
        elif action == "delete_confirm":
            row = service.get_accessible_bot(user_id, bot_id)
            title = escape(bot_title(row)) if row is not None else "this bot"
            await edit_or_reply_html(
                update,
                tr(update, "delete.confirm", title=title),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                tr(update, "button.yes_delete"), callback_data=f"delete:{bot_id}"
                            ),
                            InlineKeyboardButton(
                                tr(update, "button.my_bots"), callback_data=f"status:{bot_id}"
                            ),
                        ]
                    ]
                ),
            )
        elif action == "delete":
            result = await service.delete_bot(user_id, bot_id)
            await edit_or_reply_result(update, result.message)

    async def source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /source: user_id=%s bot_id=%s", user_id, bot_id)
        if not service.is_owner(user_id):
            await reply_html(
                update.effective_message,
                tr(update, "source.owner_only"),
            )
            return
        if bot_id is None:
            await reply_html(
                update.effective_message,
                tr(update, "source.usage"),
            )
            return
        result = service.get_source(user_id, bot_id)
        if not result.ok or result.code is None:
            await reply_result(update.effective_message, result.message)
            return

        chunks = chunk_text(result.code)
        if len(chunks) > 1:
            row = service.get_accessible_bot(user_id, bot_id)
            title = escape(bot_title(row)) if row is not None else "Bot Source"
            await reply_html(
                update.effective_message,
                f"<b>Source for {title}</b>\n\n{len(chunks)} parts",
            )
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
            await choose_bot_for_action(update, "stop", tr(update, "choose.stop"))
            return
        result = await service.stop_bot(user_id, bot_id)
        await reply_result(
            update.effective_message,
            result.message,
            reply_markup=keyboard_for_user(user_id),
        )

    async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /restart: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "restart", tr(update, "choose.restart"))
            return
        result = await service.restart_bot(user_id, bot_id)
        await reply_result(
            update.effective_message,
            result.message,
            reply_markup=keyboard_for_user(user_id),
        )

    async def validation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /validate: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "validation", tr(update, "choose.validation"))
            return
        result = service.validation_report(user_id, bot_id)
        await reply_result(
            update.effective_message,
            result.message,
            reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)) if result.bot_id else keyboard_for_user(user_id),
        )

    async def autofix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /fix: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "autofix", tr(update, "choose.autofix"))
            return
        progress_message = await reply_html(update.effective_message, tr(update, "autofix.running"))
        result = await service.auto_fix_bot(
            user_id, bot_id, user_context=user_context_for_ai(update)
        )
        await edit_message_result(
            progress_message,
            result.message,
            reply_markup=bot_actions_keyboard(bot_id, locale_for_update(update)) if result.bot_id else keyboard_for_user(user_id),
        )

    async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /delete: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "delete_confirm", tr(update, "choose.delete"))
            return
        result = await service.delete_bot(user_id, bot_id)
        await reply_result(
            update.effective_message,
            result.message,
            reply_markup=keyboard_for_user(user_id),
        )

    async def killall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.warning("Command /killall: user_id=%s", user_id)
        result = await service.kill_all(user_id)
        await reply_result(
            update.effective_message,
            result.message,
            reply_markup=keyboard_for_user(user_id),
        )

    async def revise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /revise: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "revise", tr(update, "choose.revise"))
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        context.user_data["revise_bot_id"] = bot_id
        context.user_data["revise_user_context"] = user_context_for_ai(update)
        await reply_html(
            update.effective_message,
            tr(update, "revise.start", title=escape(bot_title(row))),
            reply_markup=flow_keyboard(locale_for_update(update)),
        )
        return REVISE_PROMPT

    async def revise_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        if "revise_bot_id" not in context.user_data:
            return await restart_expired_flow(
                update, context, "revise", tr(update, "choose.revise")
            )
        bot_id = int(context.user_data["revise_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await reply_html(
                update.effective_message,
                tr(update, "revise.empty"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
            return REVISE_PROMPT
        logger.info(
            "Received revise prompt: user_id=%s bot_id=%s chars=%s",
            user_id,
            bot_id,
            len(prompt),
        )
        progress_message = await reply_html(
            update.effective_message,
            tr(update, "revise.running"),
        )
        result = await service.revise_bot(
            user_id,
            bot_id,
            prompt,
            user_context=str(context.user_data.get("revise_user_context", "")),
        )
        logger.info(
            "Revise bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok
        )
        result_markup = (
            bot_actions_keyboard(result.bot_id, locale_for_update(update))
            if result.bot_id
            else None
        )
        await edit_message_result(
            progress_message,
            result.message,
            reply_markup=result_markup,
        )
        context.user_data.pop("revise_bot_id", None)
        return ConversationHandler.END

    async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /edit: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await choose_bot_for_action(update, "edit", tr(update, "choose.edit"))
            return ConversationHandler.END
        if not service.can_manage(user_id, bot_id):
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await reply_html(update.effective_message, bot_not_found_text(update))
            return ConversationHandler.END
        context.user_data["edit_bot_id"] = bot_id
        context.user_data["edit_user_context"] = user_context_for_ai(update)
        await reply_html(
            update.effective_message,
            tr(update, "edit.start", title=escape(bot_title(row))),
            reply_markup=flow_keyboard(locale_for_update(update)),
        )
        return EDIT_PROMPT

    async def edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        if "edit_bot_id" not in context.user_data:
            return await restart_expired_flow(
                update, context, "edit", tr(update, "choose.edit")
            )
        bot_id = int(context.user_data["edit_bot_id"])
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await reply_html(
                update.effective_message,
                tr(update, "edit.empty"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
            return EDIT_PROMPT
        context.user_data["edit_prompt"] = prompt
        context.user_data["edit_answers"] = []
        return await continue_edit_planning(update, context)

    async def continue_edit_planning(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        user_id = _remember_user(db, update)
        if "edit_bot_id" not in context.user_data:
            return await restart_expired_flow(
                update, context, "edit", tr(update, "choose.edit")
            )
        bot_id = int(context.user_data["edit_bot_id"])
        prompt = context.user_data.get("edit_prompt", "")
        answers = context.user_data.get("edit_answers", [])
        force_code = len(answers) >= MAX_FOLLOWUP_ROUNDS
        progress_message = await reply_html(
            update.effective_message, tr(update, "edit.thinking")
        )
        decision = service.plan_edit_bot(
            user_id,
            bot_id,
            prompt,
            answers,
            force_code=force_code,
            user_context=str(context.user_data.get("edit_user_context", "")),
        )
        if isinstance(decision, OperationResult):
            await edit_message_result(progress_message, decision.message)
            context.user_data.clear()
            return ConversationHandler.END
        if decision.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "edit.more_detail"),
                )
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["edit_pending_questions"] = question_texts(decision)
            await edit_message_html(
                progress_message,
                format_ai_questions(decision),
                reply_markup=ai_response_keyboard("edit", locale_for_update(update)),
            )
            return EDIT_FOLLOWUP

        await edit_message_html(
            progress_message,
            tr(update, "edit.applying"),
        )
        result = await service.edit_bot_from_decision(
            user_id,
            bot_id,
            prompt,
            decision,
            user_context=str(context.user_data.get("edit_user_context", "")),
        )
        logger.info(
            "Edit bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok
        )
        result_markup = (
            bot_actions_keyboard(result.bot_id, locale_for_update(update))
            if result.bot_id
            else None
        )
        await edit_message_result(
            progress_message,
            result.message,
            reply_markup=result_markup,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def edit_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _remember_user(db, update)
        if "edit_bot_id" not in context.user_data:
            return await restart_expired_flow(update, context, "edit", tr(update, "choose.edit"))
        answer = (update.effective_message.text or "").strip()
        if not answer:
            await reply_html(
                update.effective_message,
                tr(update, "followup.empty"),
                reply_markup=flow_keyboard(locale_for_update(update)),
            )
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

    async def edit_reprompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        user_id = _remember_user(db, update)
        if "edit_bot_id" not in context.user_data:
            return await restart_expired_flow(
                update, context, "edit", tr(update, "choose.edit")
            )
        bot_id = int(context.user_data["edit_bot_id"])
        row = service.get_accessible_bot(user_id, bot_id)
        if row is None:
            await edit_or_reply_html(update, bot_not_found_text(update))
            return ConversationHandler.END
        logger.info("Re-prompt edit: user_id=%s bot_id=%s", user_id, bot_id)
        user_context = context.user_data.get("edit_user_context") or user_context_for_ai(update)
        context.user_data.clear()
        context.user_data["edit_bot_id"] = bot_id
        context.user_data["edit_user_context"] = user_context
        await edit_or_reply_html(
            update,
            tr(update, "edit.reprompt", title=escape(bot_title(row))),
            reply_markup=flow_keyboard(locale_for_update(update)),
        )
        return EDIT_PROMPT

    async def post_init(application) -> None:
        logger.info("Post-init: restoring child bots")
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Show help"),
                BotCommand("help", "Show full guide"),
                BotCommand("examples", "Prompt examples"),
                BotCommand("language", "Change language"),
                BotCommand("newbot", "Create and launch a child bot"),
                BotCommand("bots", "List your child bots"),
                BotCommand("status", "Show bot status, or list all bots"),
                BotCommand("tail", "Show child bot logs"),
                BotCommand("ask", "Ask about a child bot"),
                BotCommand("edit", "Change a child bot with prompts"),
                BotCommand("id", "Show your Telegram user ID"),
                BotCommand("health", "Show manager health"),
                BotCommand("validate", "Show generated-code validation report"),
                BotCommand("fix", "Auto-fix a child bot from logs"),
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
        logger.error(
            "Unhandled Telegram handler error: error_id=%s update=%r",
            error_id,
            update,
            exc_info=exc_info,
        )
        if isinstance(update, Update) and update.effective_message is not None:
            try:
                detail = ""
                if (
                    update.effective_user is not None
                    and service.is_owner(int(update.effective_user.id))
                    and exc is not None
                ):
                    detail = f"\n\n<b>Owner detail</b>\n{escape(type(exc).__name__)}: {escape(str(exc))}"
                await reply_html(
                    update.effective_message,
                    tr(update, "error.unhandled", error_id=error_id)
                    + f"{detail}",
                )
            except Exception:
                logger.exception(
                    "Failed to send handler error message: error_id=%s", error_id
                )

    conversation_text = (
        filters.TEXT & ~filters.COMMAND & ~filters.Regex(keyboard_button_pattern)
    )
    cancel_fallbacks = [
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(cancel, pattern="^nav:cancel$"),
        MessageHandler(filters.Regex("^❌ Cancel$"), cancel),
    ]

    newbot_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newbot", newbot),
            MessageHandler(filters.Regex("^🪄 New Bot$"), newbot),
            CallbackQueryHandler(newbot_button, pattern="^nav:newbot$"),
        ],
        states={
            NEW_PROMPT: [
                CallbackQueryHandler(newbot_template, pattern=r"^template:\w+$"),
                MessageHandler(conversation_text, newbot_prompt),
            ],
            NEW_FOLLOWUP: [
                CallbackQueryHandler(newbot_reprompt, pattern=r"^reprompt:newbot$"),
                MessageHandler(conversation_text, newbot_followup),
            ],
            NEW_TOKEN: [
                CallbackQueryHandler(newbot_reprompt, pattern=r"^reprompt:newbot$"),
                MessageHandler(conversation_text, newbot_token),
            ],
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
            EDIT_FOLLOWUP: [
                CallbackQueryHandler(edit_reprompt, pattern=r"^reprompt:edit$"),
                MessageHandler(conversation_text, edit_followup),
            ],
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
    application.add_handler(CommandHandler("language", language_command))
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
            filters.Regex(keyboard_button_pattern),
            button_text,
        )
    )
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("bots", bots))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("tail", tail))
    application.add_handler(CommandHandler("logs", tail))
    application.add_handler(CommandHandler("validate", validation))
    application.add_handler(CommandHandler("fix", autofix))
    application.add_handler(CommandHandler("autofix", autofix))
    application.add_handler(CommandHandler("source", source))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("delete", delete))
    application.add_handler(CommandHandler("killall", killall))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_error_handler(error_handler)
    return application

