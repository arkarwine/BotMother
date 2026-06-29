from __future__ import annotations

import logging
import re
import uuid
import asyncio
import zlib
from html import escape, unescape
from typing import Any

from .ai import MAX_FOLLOWUP_ROUNDS, AIDecision, AIReadinessDecision, AIUsage
from .credits import ACTION_ASK, ACTION_EDIT, ACTION_LABELS, ACTION_NEW_BOT, ACTION_REVISE
from .db import Database
from .localization import normalize_locale, t
from .service import BotService, OperationResult
from .tokens import is_valid_telegram_token

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
    "python_missing": "🔴",
    "sandbox_missing": "🔴",
    "sandbox_unavailable": "🔴",
    "deleted": "🗑️",
    "generating": "🧠",
}

HELP_TEXT = t("help.text")

HELP_CATEGORY_TEXTS = {
    "create": t("help.create"),
    "manage": t("help.manage"),
    "ops": t("help.ops"),
    "utils": t("help.utils"),
    "credits": t("help.credits"),
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

BOT_LIST_PAGE_SIZE = 10
NEWBOT_TEMPLATE_CALLBACK_PATTERN = r"^template:\w+$"
NEWBOT_EXAMPLES_CALLBACK = "newbot:examples"


def apply_bot_template(prompt: str, template: str | None) -> str:
    mode = template if template in BOT_TEMPLATE_PROMPTS else "other"
    return f"{BOT_TEMPLATE_PROMPTS[mode]}\n\nUser request:\n{prompt.strip()}"


def newbot_brief_key(template: str | None) -> str:
    mode = template if template in BOT_TEMPLATE_PROMPTS else "other"
    return f"newbot.template_{mode}"


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


def format_bot_page(
    rows: list[Any],
    page: int = 0,
    locale: str = "en",
    empty_key: str = "empty.bots",
) -> tuple[str, int]:
    visible_rows, page, total_pages = page_slice(rows, page)
    text = format_bot_list(visible_rows, locale) if rows else t(empty_key, locale=locale)
    if rows:
        text += "\n\n" + t(
            "bots.page_info",
            locale=locale,
            page=str(page + 1),
            pages=str(total_pages),
            count=str(len(rows)),
        )
    return text, page


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
    name = bot_username_label(row) or bot_title(row)
    if len(name) > 34:
        name = name[:31] + "..."
    return f"{STATUS_EMOJI.get(row['status'], '•')} {name}"


def help_category_text(category: str, locale: str = "en") -> str:
    if category in {"create", "manage", "ops", "utils", "credits", "fallback"}:
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


def clamp_page(total: int, page: int, page_size: int = BOT_LIST_PAGE_SIZE) -> int:
    max_page = max(0, (max(0, total) - 1) // page_size)
    return max(0, min(page, max_page))


def page_slice(rows: list[Any], page: int, page_size: int = BOT_LIST_PAGE_SIZE) -> tuple[list[Any], int, int]:
    page = clamp_page(len(rows), page, page_size)
    start = page * page_size
    return rows[start : start + page_size], page, max(1, ((len(rows) - 1) // page_size) + 1) if rows else 1


def bot_matches(row: Any, query: str) -> bool:
    haystack = " ".join(
        str(row_value(row, key, "") or "")
        for key in (
            "name",
            "bot_username",
            "status",
            "owner_username",
            "owner_first_name",
            "owner_last_name",
        )
    ).lower()
    return query.lower() in haystack


def question_texts(decision: AIDecision | AIReadinessDecision) -> list[str]:
    return [question.question for question in decision.questions]


def strip_question_sentences(message: str) -> str:
    parts = re.split(r"(?<=[။.!?])\s+", message.strip())
    kept = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        if "?" in text:
            continue
        kept.append(text)
    return " ".join(kept).strip()


def format_ai_questions(decision: AIDecision | AIReadinessDecision) -> str:
    if decision.needs_questions and not decision.questions:
        return "I need a little more detail before building."
    questions = [question.question.strip() for question in decision.questions if question.question.strip()]
    message = decision.message.strip()
    if decision.needs_questions and questions:
        message = strip_question_sentences(message)
        visible_questions = [question for question in questions if question not in message]
        if message and visible_questions:
            return message + "\n\n" + "\n\n".join(visible_questions)
        if message:
            return message
        return "\n\n".join(questions)
    if message:
        return message
    return "I need a little more detail before building."


def html_to_plain_text(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return unescape(without_tags).strip()


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
    return normalize_locale(None)


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
        "AI response language: Myanmar/Burmese"
        if locale_for_update(update) == "my"
        else "AI response language: English",
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


def format_credit_overview(update: Any, service: BotService, user_id: int) -> str:
    if not service.settings.credits_enabled:
        return tr(update, "credits.disabled")
    if service.is_credit_exempt(user_id):
        return tr(update, "credits.exempt")
    balance = service.credit_balance(user_id)
    return tr(
        update,
        "credits.overview",
        balance=str(balance if balance is not None else 0),
        new_bot=str(service.credit_cost(ACTION_NEW_BOT)),
        edit=str(service.credit_cost(ACTION_EDIT)),
        ask=str(service.credit_cost(ACTION_ASK)),
        runtime=str(service.settings.credit_runtime_seconds_per_credit // 3600),
    )


def format_paid_action_intro(update: Any, service: BotService, action: str, body: str) -> str:
    if not service.settings.credits_enabled:
        return body
    cost = service.credit_cost(action)
    if service.is_credit_exempt(int(update.effective_user.id)):
        note = tr(update, "credits.exempt_line")
    else:
        balance = service.credit_balance(int(update.effective_user.id))
        note = tr(
            update,
            "credits.cost_line",
            cost=str(cost),
            balance=str(balance if balance is not None else 0),
        )
    return body + "\n\n" + note


def format_credit_gate(update: Any, gate: Any) -> str:
    if getattr(gate, "ok", False):
        return ""
    label = ACTION_LABELS.get(getattr(gate, "action", ""), getattr(gate, "action", "Action"))
    return tr(
        update,
        "credits.insufficient",
        action=escape(label),
        cost=str(getattr(gate, "cost", 0)),
        balance=str(getattr(gate, "balance", 0)),
    )


def format_ai_usage_plain(usage: AIUsage | None, locale: str = "en") -> str:
    if usage is None or not usage.has_counts:
        return ""
    unknown = "?"
    return t(
        "ai.usage_plain",
        locale=locale,
        prompt=str(usage.prompt_tokens if usage.prompt_tokens is not None else unknown),
        completion=str(
            usage.completion_tokens if usage.completion_tokens is not None else unknown
        ),
        total=str(usage.total_tokens if usage.total_tokens is not None else unknown),
    )


def append_ai_usage(text: str, usage: AIUsage | None, locale: str = "en") -> str:
    usage_text = format_ai_usage_plain(usage, locale=locale)
    return f"{text}\n\n{usage_text}" if usage_text else text


def combine_ai_usage(*usages: AIUsage | None) -> AIUsage | None:
    usable = [usage for usage in usages if usage is not None and usage.has_counts]
    if not usable:
        return None

    def total_for(field: str) -> int | None:
        values = [getattr(usage, field) for usage in usable]
        known = [int(value) for value in values if value is not None]
        return sum(known) if known else None

    combined = AIUsage(
        prompt_tokens=total_for("prompt_tokens"),
        completion_tokens=total_for("completion_tokens"),
        total_tokens=total_for("total_tokens"),
    )
    return combined if combined.has_counts else None


def format_home_title(update: Any, service: BotService, user_id: int) -> str:
    text = tr(update, "home.title")
    if not service.settings.credits_enabled:
        return text
    if service.is_credit_exempt(user_id):
        return text + "\n\n" + tr(update, "credits.exempt_line")
    balance = service.credit_balance(user_id)
    return text + "\n\n" + tr(
        update,
        "credits.home_line",
        balance=str(balance if balance is not None else 0),
    )


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
        "button.credits",
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

    def newbot_prompt_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("button.examples", locale=locale),
                        callback_data=NEWBOT_EXAMPLES_CALLBACK,
                    ),
                    InlineKeyboardButton(t("button.change_mode", locale=locale), callback_data="template:choose"),
                ],
                [InlineKeyboardButton(t("button.cancel", locale=locale), callback_data="nav:cancel")],
            ]
        )

    def token_retry_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.reprompt", locale=locale), callback_data="reprompt:newbot"),
                    InlineKeyboardButton(t("button.cancel", locale=locale), callback_data="nav:cancel"),
                ]
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
                [
                    t("button.credits", locale=locale),
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
                    InlineKeyboardButton(t("button.credits", locale=locale), callback_data="nav:credits"),
                    InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language"),
                ],
            ]
        )

    def credits_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [
                    InlineKeyboardButton(t("button.help", locale=locale), callback_data="help:credits"),
                    InlineKeyboardButton(t("button.home", locale=locale), callback_data="nav:home"),
                ],
            ]
        )

    def help_menu_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t("button.create", locale=locale), callback_data="help:create"),
                    InlineKeyboardButton(t("button.manage", locale=locale), callback_data="help:manage"),
                ],
                [
                    InlineKeyboardButton(t("button.operations", locale=locale), callback_data="help:ops"),
                    InlineKeyboardButton(t("button.utilities", locale=locale), callback_data="help:utils"),
                ],
                [
                    InlineKeyboardButton(t("button.credits", locale=locale), callback_data="help:credits"),
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
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
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
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
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
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "utils":
            rows = [
                [
                    InlineKeyboardButton(t("button.profile", locale=locale), callback_data="nav:id"),
                    InlineKeyboardButton(t("button.health", locale=locale), callback_data="nav:health"),
                ],
                [InlineKeyboardButton(t("button.language", locale=locale), callback_data="nav:language")],
                [InlineKeyboardButton(t("button.examples", locale=locale), callback_data="nav:examples")],
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
            ]
        elif category == "credits":
            rows = [
                [
                    InlineKeyboardButton(t("button.credits", locale=locale), callback_data="nav:credits"),
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                ],
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
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
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(t("button.new_bot", locale=locale), callback_data="nav:newbot"),
                    InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots"),
                ],
                [InlineKeyboardButton(t("button.back_help", locale=locale), callback_data="nav:help")],
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

    def search_empty_keyboard(locale: str = "en"):
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("button.my_bots", locale=locale), callback_data="nav:bots")],
                [InlineKeyboardButton(t("button.home", locale=locale), callback_data="nav:home")],
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

    def bots_keyboard(
        rows: list[Any],
        action: str = "status",
        show_back: bool = False,
        locale: str = "en",
        page: int = 0,
        pager_prefix: str = "bots_page",
    ):
        if not rows:
            return empty_state_keyboard(locale)
        visible_rows, page, total_pages = page_slice(rows, page)
        buttons = [
            [
                InlineKeyboardButton(
                    compact_bot_label(row), callback_data=f"{action}:{row['id']}"
                )
            ]
            for row in visible_rows
        ]
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(
                    InlineKeyboardButton(
                        "◀️",
                        callback_data=f"{pager_prefix}:{action}:{page - 1}",
                    )
                )
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(
                    InlineKeyboardButton(
                        "▶️",
                        callback_data=f"{pager_prefix}:{action}:{page + 1}",
                    )
                )
            buttons.append(nav)
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

    async def edit_or_reply_html(update: Update, text: str, reply_markup=None):
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
                result = await query.edit_message_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return result if hasattr(result, "edit_text") else update.effective_message
            except Exception as exc:
                if is_message_not_modified(exc):
                    return update.effective_message
                logger.debug(
                    "Could not edit callback message; sending a new message",
                    exc_info=True,
                )
        return await reply_html(update.effective_message, text, reply_markup=reply_markup)

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

    def reservation_keys() -> tuple[str, ...]:
        return ("newbot_credit_reservation_id", "edit_credit_reservation_id")

    def refund_flow_credits(context: ContextTypes.DEFAULT_TYPE, note: str) -> None:
        for key in reservation_keys():
            reservation_id = context.user_data.pop(key, None)
            if reservation_id is not None:
                service.refund_paid_action(int(reservation_id), note)

    async def reply_home(message, text: str, locale: str = "en") -> None:
        await reply_html(message, text, reply_markup=main_reply_keyboard(locale))

    def progress_draft_id(update: Update, title_key: str) -> int:
        message_id = getattr(update.effective_message, "message_id", None) or 1
        suffix = zlib.crc32(title_key.encode("utf-8")) % 997
        return max(1, (int(message_id) * 1000) + suffix + 1)

    def progress_tokens_html(
        update: Update, max_tokens: int | None, received_chars: int = 0
    ) -> str:
        if max_tokens is None:
            return ""
        if received_chars > 0:
            return tr(
                update,
                "ai.progress_tokens_live",
                approx=str(max(1, round(received_chars / 4))),
                chars=str(received_chars),
                budget=str(max_tokens),
            )
        return tr(update, "ai.progress_tokens_waiting", budget=str(max_tokens))

    def progress_text(
        update: Update,
        title: str,
        detail: str,
        elapsed_seconds: int,
        max_tokens: int | None = None,
        received_chars: int = 0,
    ) -> str:
        return tr(
            update,
            "ai.progress",
            title=escape(title),
            detail=escape(detail),
            elapsed=str(elapsed_seconds),
            tokens=progress_tokens_html(update, max_tokens, received_chars),
        )

    def draft_preview_text(text: str, limit: int = 3900) -> str:
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return "...\n" + stripped[-limit:]

    def stream_preview_with_usage(
        update: Update, text: str, max_tokens: int | None = None
    ) -> str:
        stripped = text.strip()
        chars = len(stripped)
        approx_tokens = max(1, round(chars / 4)) if chars else 0
        footer = t(
            "ai.stream_usage_plain",
            locale=locale_for_update(update),
            approx=str(approx_tokens),
            chars=str(chars),
            budget=str(max_tokens) if max_tokens is not None else "?",
        )
        room = max(1200, 3900 - len(footer) - 2)
        return draft_preview_text(stripped, limit=room) + "\n\n" + footer

    def coding_brief_chunks(brief: str, chunk_size: int = 2400) -> list[str]:
        return chunk_text(brief.strip(), chunk_size) if brief.strip() else []

    async def send_coding_brief_messages(
        update: Update,
        brief: str,
        title_key: str = "ai.coding_brief_title",
    ) -> None:
        chunks = coding_brief_chunks(brief)
        if not chunks:
            return
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            await reply_html(
                update.effective_message,
                tr(update, title_key, page=str(index), pages=str(total))
                + "\n\n<pre>"
                + escape(chunk)
                + "</pre>",
            )

    async def send_plain_draft(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        title_key: str,
        text: str,
    ) -> bool:
        try:
            await context.bot.send_message_draft(
                chat_id=_chat_id(update),
                draft_id=progress_draft_id(update, title_key),
                text=draft_preview_text(text),
            )
            return True
        except Exception:
            logger.debug("Telegram plain draft streaming failed", exc_info=True)
            return False

    async def run_with_progress(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        progress_message: Any,
        awaitable: Any,
        title_key: str,
        detail_key: str,
        max_tokens: int | None = None,
        interval_seconds: float = 6.0,
        stream_queue: asyncio.Queue[str] | None = None,
        stream_visible_text: bool = False,
    ):
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(awaitable)
        last_update = 0.0
        received_chars = 0
        streamed_parts: list[str] = []
        last_rendered = ""
        draft_ok = True
        while not task.done() or (stream_queue is not None and not stream_queue.empty()):
            if stream_queue is None:
                await asyncio.sleep(interval_seconds)
            else:
                try:
                    delta = await asyncio.wait_for(stream_queue.get(), timeout=0.8)
                    received_chars += len(delta)
                    streamed_parts.append(delta)
                except asyncio.TimeoutError:
                    pass
            if task.done() and (stream_queue is None or stream_queue.empty()):
                break
            now = loop.time()
            update_interval = 0.45 if stream_visible_text and received_chars else 1.0 if received_chars else interval_seconds
            if now - last_update < update_interval:
                continue
            elapsed = int(loop.time() - started)
            raw_stream = "".join(streamed_parts)
            visible_preview = raw_stream.strip() if stream_visible_text else ""
            if visible_preview:
                rendered = stream_preview_with_usage(
                    update,
                    visible_preview,
                    max_tokens=max_tokens,
                )
                if rendered != last_rendered:
                    if draft_ok:
                        draft_ok = await send_plain_draft(
                            update, context, title_key, rendered
                        )
                    if not draft_ok:
                        await edit_message_plain(progress_message, rendered)
                    last_rendered = rendered
            else:
                rendered = progress_text(
                    update,
                    tr(update, title_key),
                    tr(update, detail_key),
                    elapsed,
                    max_tokens=max_tokens,
                    received_chars=received_chars,
                )
                if rendered != last_rendered:
                    await edit_message_html(progress_message, rendered)
                    last_rendered = rendered
            last_update = now
        return await task

    def make_stream_queue() -> tuple[asyncio.Queue[str], Any]:
        loop = asyncio.get_running_loop()
        stream_queue: asyncio.Queue[str] = asyncio.Queue()

        def on_delta(delta: str) -> None:
            if delta:
                loop.call_soon_threadsafe(stream_queue.put_nowait, delta)

        return stream_queue, on_delta

    async def restart_expired_flow(
        update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, title: str
    ) -> int:
        refund_flow_credits(context, "Flow expired")
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
            format_home_title(update, service, user_id),
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
            format_user_profile(update, service.is_owner(user_id))
            + "\n\n"
            + format_credit_overview(update, service, user_id),
            reply_markup=keyboard_for_user(user_id),
        )

    async def credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        logger.info("Command /credits: user_id=%s chat_id=%s", user_id, _chat_id(update))
        await reply_html(
            update.effective_message,
            format_credit_overview(update, service, user_id),
            reply_markup=credits_keyboard(locale_for_update(update)),
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
        refund_flow_credits(context, "User cancelled the flow")
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
        refund_flow_credits(context, "New Bot restarted")
        context.user_data.pop("newbot_prompt", None)
        context.user_data.pop("newbot_credit_reservation_id", None)
        context.user_data["newbot_user_context"] = user_context_for_ai(update)
        await reply_html(
            update.effective_message,
            format_paid_action_intro(
                update, service, ACTION_NEW_BOT, tr(update, "newbot.choose_template")
            ),
            reply_markup=template_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def newbot_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        _remember_user(db, update)
        if "newbot_user_context" not in context.user_data:
            context.user_data["newbot_user_context"] = user_context_for_ai(update)
        template = (query.data if query else "template:other").split(":", 1)[1]
        if template == "choose":
            await edit_or_reply_html(
                update,
                format_paid_action_intro(
                    update, service, ACTION_NEW_BOT, tr(update, "newbot.choose_template")
                ),
                reply_markup=template_keyboard(locale_for_update(update)),
            )
            return NEW_PROMPT
        if template not in BOT_TEMPLATE_PROMPTS:
            template = "other"
        context.user_data["newbot_template"] = template
        await edit_or_reply_html(
            update,
            tr(update, newbot_brief_key(template)),
            reply_markup=newbot_prompt_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def newbot_template_entry(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        user_id = _remember_user(db, update)
        logger.info(
            "New Bot template entry: user_id=%s data=%s",
            user_id,
            update.callback_query.data if update.callback_query else None,
        )
        user_context = user_context_for_ai(update)
        refund_flow_credits(context, "New Bot template restarted")
        context.user_data.clear()
        context.user_data["newbot_user_context"] = user_context
        return await newbot_template(update, context)

    async def newbot_examples_button(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        if query is not None:
            await query.answer()
        _remember_user(db, update)
        if "newbot_user_context" not in context.user_data:
            context.user_data["newbot_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            tr(update, "newbot.examples_in_flow"),
            reply_markup=newbot_prompt_keyboard(locale_for_update(update)),
        )
        return NEW_PROMPT

    async def newbot_examples_entry(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        user_id = _remember_user(db, update)
        logger.info("New Bot examples entry: user_id=%s", user_id)
        user_context = user_context_for_ai(update)
        refund_flow_credits(context, "New Bot examples restarted")
        context.user_data.clear()
        context.user_data["newbot_user_context"] = user_context
        return await newbot_examples_button(update, context)

    async def newbot_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = _remember_user(db, update)
        raw_prompt = (update.effective_message.text or "").strip()
        if not raw_prompt:
            await reply_html(
                update.effective_message,
                tr(update, newbot_brief_key(context.user_data.get("newbot_template"))),
                reply_markup=newbot_prompt_keyboard(locale_for_update(update)),
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
        if "newbot_credit_reservation_id" not in context.user_data:
            gate = service.reserve_paid_action(user_id, ACTION_NEW_BOT)
            if not gate.ok:
                await reply_html(
                    update.effective_message,
                    format_credit_gate(update, gate),
                    reply_markup=credits_keyboard(locale_for_update(update)),
                )
                context.user_data.clear()
                return ConversationHandler.END
            if gate.reservation_id is not None:
                context.user_data["newbot_credit_reservation_id"] = gate.reservation_id
        return await continue_newbot_planning(update, context)

    async def continue_newbot_planning(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        user_id = _remember_user(db, update)
        prompt = context.user_data.get("newbot_prompt", "")
        answers = context.user_data.get("newbot_answers", [])
        force_code = len(answers) >= MAX_FOLLOWUP_ROUNDS
        progress_message = await reply_html(
            update.effective_message,
            progress_text(
                update,
                tr(update, "ai.progress_newbot_title"),
                tr(update, "ai.progress_newbot_detail"),
                0,
                max_tokens=service.settings.openrouter_interaction_max_tokens,
            ),
        )
        try:
            plan_stream_queue, plan_on_delta = make_stream_queue()
            decision = await run_with_progress(
                update,
                context,
                progress_message,
                asyncio.to_thread(
                    service.plan_new_bot,
                    prompt,
                    answers,
                    force_code=force_code,
                    user_context=str(context.user_data.get("newbot_user_context", "")),
                    on_delta=plan_on_delta,
                ),
                "ai.progress_newbot_title",
                "ai.progress_newbot_detail",
                max_tokens=service.settings.openrouter_interaction_max_tokens,
                stream_queue=plan_stream_queue,
                stream_visible_text=True,
            )
        except Exception:
            refund_flow_credits(context, "New Bot planning failed")
            raise
        if decision.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "newbot.more_detail"),
                )
                refund_flow_credits(context, "New Bot exceeded follow-up limit")
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(decision)
            await edit_message_html(
                progress_message,
                append_ai_usage(
                    format_ai_questions(decision),
                    decision.ai_usage,
                    locale_for_update(update),
                ),
                reply_markup=ai_response_keyboard("newbot", locale_for_update(update)),
            )
            return NEW_FOLLOWUP

        try:
            await edit_message_html(
                progress_message,
                progress_text(
                    update,
                    tr(update, "ai.progress_readiness_title"),
                    tr(update, "ai.progress_readiness_detail"),
                    0,
                    max_tokens=service.settings.openrouter_interaction_max_tokens,
                ),
            )
            readiness_stream_queue, readiness_on_delta = make_stream_queue()
            readiness = await run_with_progress(
                update,
                context,
                progress_message,
                asyncio.to_thread(
                    service.check_new_bot_readiness,
                    prompt,
                    answers,
                    decision,
                    user_context=str(context.user_data.get("newbot_user_context", "")),
                    on_delta=readiness_on_delta,
                ),
                "ai.progress_readiness_title",
                "ai.progress_readiness_detail",
                max_tokens=service.settings.openrouter_interaction_max_tokens,
                stream_queue=readiness_stream_queue,
                stream_visible_text=True,
            )
        except Exception:
            refund_flow_credits(context, "New Bot readiness check failed")
            raise
        if readiness.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "newbot.missing_launch_detail"),
                )
                refund_flow_credits(context, "New Bot missing essential launch detail")
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["newbot_pending_questions"] = question_texts(readiness)
            await edit_message_html(
                progress_message,
                append_ai_usage(
                    format_ai_questions(readiness),
                    combine_ai_usage(decision.ai_usage, readiness.ai_usage),
                    locale_for_update(update),
                ),
                reply_markup=ai_response_keyboard("newbot", locale_for_update(update)),
            )
            return NEW_FOLLOWUP

        combined_plan_usage = combine_ai_usage(decision.ai_usage, readiness.ai_usage)
        context.user_data["newbot_plan_usage"] = combined_plan_usage
        context.user_data["newbot_decision"] = decision
        await edit_message_html(
            progress_message,
            append_ai_usage(
                (escape(decision.message.strip()) + "\n\n" if decision.message else "")
                + tr(update, "newbot.token"),
                combined_plan_usage,
                locale_for_update(update),
            ),
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
        refund_flow_credits(context, "User re-prompted New Bot")
        context.user_data.clear()
        context.user_data["newbot_user_context"] = user_context
        await edit_or_reply_html(
            update,
            format_paid_action_intro(
                update, service, ACTION_NEW_BOT, tr(update, "newbot.choose_template")
            ),
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
        if not is_valid_telegram_token(token_text):
            await reply_html(
                update.effective_message,
                tr(update, "newbot.token_invalid"),
                reply_markup=token_retry_keyboard(locale_for_update(update)),
            )
            return NEW_TOKEN
        if token_text == service.settings.mother_bot_token:
            await reply_html(
                update.effective_message,
                tr(update, "newbot.token_mother"),
                reply_markup=token_retry_keyboard(locale_for_update(update)),
            )
            return NEW_TOKEN
        existing = db.get_bot_by_token(token_text)
        if existing is not None:
            duplicate_key = (
                "newbot.token_duplicate"
                if service.is_owner(user_id) or int(existing["owner_user_id"]) == user_id
                else "newbot.token_duplicate_other"
            )
            await reply_html(
                update.effective_message,
                tr(update, duplicate_key, title=escape(bot_title(existing))),
                reply_markup=token_retry_keyboard(locale_for_update(update)),
            )
            return NEW_TOKEN
        await send_coding_brief_messages(update, str(decision.code or ""))
        progress_message = await reply_html(
            update.effective_message,
            progress_text(
                update,
                tr(update, "ai.progress_codegen_title"),
                tr(update, "ai.progress_codegen_detail"),
                0,
                max_tokens=service.settings.openrouter_coding_max_tokens,
            ),
        )
        reservation_id = context.user_data.get("newbot_credit_reservation_id")
        try:
            result = await run_with_progress(
                update,
                context,
                progress_message,
                service.create_bot_from_decision(
                    user_id,
                    _chat_id(update),
                    prompt,
                    token_text,
                    decision,
                    user_context=str(context.user_data.get("newbot_user_context", "")),
                ),
                "ai.progress_codegen_title",
                "ai.progress_codegen_detail",
                max_tokens=service.settings.openrouter_coding_max_tokens,
            )
        except Exception:
            service.refund_paid_action(reservation_id, "New Bot creation failed")
            raise
        if result.ok or result.bot_id is not None:
            service.settle_paid_action(reservation_id, bot_id=result.bot_id, note="New Bot generated")
        else:
            service.refund_paid_action(reservation_id, "New Bot did not create a valid bot")
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
            append_ai_usage(
                result.message,
                combine_ai_usage(
                    context.user_data.get("newbot_plan_usage"),
                    result.ai_usage,
                ),
                locale_for_update(update),
            ),
            reply_markup=result_markup,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        await service.refresh_missing_bot_usernames_for(user_id)
        rows = service.list_bots_for(user_id)
        logger.info("Command /bots: user_id=%s count=%s", user_id, len(rows))
        text, page = format_bot_page(rows, locale=locale_for_update(update))
        await reply_html(
            update.effective_message,
            text,
            reply_markup=bots_keyboard(rows, locale=locale_for_update(update), page=page),
        )

    async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        query = " ".join(context.args).strip()
        logger.info("Command /search: user_id=%s query=%r", user_id, query)
        if not query:
            await reply_html(
                update.effective_message,
                tr(update, "search.usage"),
                reply_markup=keyboard_for_user(user_id),
            )
            return
        rows = [row for row in service.list_bots_for(user_id) if bot_matches(row, query)]
        context.user_data["last_search_query"] = query
        locale = locale_for_update(update)
        page_text, page = format_bot_page(rows, locale=locale, empty_key="search.empty")
        await reply_html(
            update.effective_message,
            tr(
                update,
                "search.results",
                query=escape(query),
                count=str(len(rows)),
                results=page_text,
            ),
            reply_markup=(
                bots_keyboard(
                    rows,
                    locale=locale,
                    page=page,
                    pager_prefix="search_page",
                )
                if rows
                else search_empty_keyboard(locale)
            ),
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = _remember_user(db, update)
        bot_id = parse_bot_id(context.args)
        logger.info("Command /status: user_id=%s bot_id=%s", user_id, bot_id)
        if bot_id is None:
            await service.refresh_missing_bot_usernames_for(user_id)
            rows = service.list_bots_for(user_id)
            text, page = format_bot_page(rows, locale=locale_for_update(update))
            await reply_html(
                update.effective_message,
                text,
                reply_markup=bots_keyboard(rows, locale=locale_for_update(update), page=page),
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
        context.user_data["ask_user_context"] = user_context_for_ai(update)
        if question:
            return await answer_bot_question(update, context, user_id, bot_id, question)
        context.user_data["ask_bot_id"] = bot_id
        await reply_html(
            update.effective_message,
            format_paid_action_intro(
                update,
                service,
                ACTION_ASK,
                tr(update, "ask.start_examples", title=title),
            ),
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
        title_key = "ai.progress_ask_title"
        detail_key = "ai.progress_ask_detail"
        initial_progress = progress_text(
            update,
            tr(update, title_key),
            tr(update, detail_key),
            0,
            max_tokens=service.settings.openrouter_interaction_max_tokens,
        )
        draft_ok = True
        progress_message = await reply_html(update.effective_message, initial_progress)

        loop = asyncio.get_running_loop()
        stream_queue: asyncio.Queue[str] = asyncio.Queue()

        def on_delta(delta: str) -> None:
            if delta:
                loop.call_soon_threadsafe(stream_queue.put_nowait, delta)

        result_task = asyncio.create_task(
            asyncio.to_thread(
                service.ask_bot_streaming,
                user_id,
                bot_id,
                question,
                user_context=str(context.user_data.get("ask_user_context", "")),
                on_delta=on_delta,
            )
        )
        streamed_parts: list[str] = []
        last_preview = ""
        started = loop.time()
        last_update = 0.0
        while not result_task.done() or not stream_queue.empty():
            try:
                delta = await asyncio.wait_for(stream_queue.get(), timeout=1.0)
                streamed_parts.append(delta)
            except asyncio.TimeoutError:
                delta = ""

            preview = "".join(streamed_parts).strip()
            now = loop.time()
            elapsed = int(now - started)
            if not preview:
                if now - last_update >= 6:
                    await edit_message_html(
                        progress_message,
                        progress_text(
                            update,
                            tr(update, title_key),
                            tr(update, detail_key),
                            elapsed,
                            max_tokens=service.settings.openrouter_interaction_max_tokens,
                        ),
                    )
                    last_update = now
                continue
            if preview == last_preview or (
                last_preview and now - last_update < 0.7 and len(delta) < 80
            ):
                continue
            last_preview = preview
            last_update = now
            streamed_text = stream_preview_with_usage(
                update,
                preview,
                max_tokens=service.settings.openrouter_interaction_max_tokens,
            )
            if draft_ok:
                draft_ok = await send_plain_draft(
                    update, context, title_key, streamed_text
                )
            if not draft_ok:
                await edit_message_plain(progress_message, streamed_text)

        result = await result_task
        logger.info(
            "Ask bot result: user_id=%s bot_id=%s ok=%s", user_id, bot_id, result.ok
        )
        chunks = chunk_text(
            append_ai_usage(result.message, result.ai_usage, locale_for_update(update))
        )
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
            rows,
            action,
            show_back=False,
            locale=locale_for_update(update),
            pager_prefix="pick_page",
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
        elif key == "button.credits":
            await credits(update, context)
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
        refund_flow_credits(context, "New Bot restarted")
        context.user_data.pop("newbot_prompt", None)
        context.user_data.pop("newbot_credit_reservation_id", None)
        context.user_data["newbot_user_context"] = user_context_for_ai(update)
        await edit_or_reply_html(
            update,
            format_paid_action_intro(
                update, service, ACTION_NEW_BOT, tr(update, "newbot.choose_template")
            ),
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
            format_paid_action_intro(
                update,
                service,
                ACTION_ASK,
                tr(update, "ask.start_short", title=escape(bot_title(row))),
            ),
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
            format_paid_action_intro(
                update,
                service,
                ACTION_EDIT,
                tr(update, "edit.start_short", title=escape(bot_title(row))),
            ),
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
            format_paid_action_intro(
                update,
                service,
                ACTION_REVISE,
                tr(update, "revise.start_short", title=escape(bot_title(row))),
            ),
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
                format_home_title(update, service, user_id),
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
        if data == "nav:credits":
            await edit_or_reply_html(
                update,
                format_credit_overview(update, service, user_id),
                reply_markup=credits_keyboard(locale_for_update(update)),
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
                    format_home_title(update, service, user_id),
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
            text, page = format_bot_page(rows, locale=locale_for_update(update))
            await edit_or_reply_html(
                update,
                text,
                reply_markup=bots_keyboard(rows, locale=locale_for_update(update), page=page),
            )
            return
        if data.startswith("bots_page:"):
            _, action, raw_page = data.split(":", 2)
            rows = service.list_bots_for(user_id)
            text, page = format_bot_page(
                rows, page=int(raw_page), locale=locale_for_update(update)
            )
            await edit_or_reply_html(
                update,
                text,
                reply_markup=bots_keyboard(
                    rows,
                    action=action,
                    locale=locale_for_update(update),
                    page=page,
                    pager_prefix="bots_page",
                ),
            )
            return
        if data.startswith("search_page:"):
            _, action, raw_page = data.split(":", 2)
            search_query = str(context.user_data.get("last_search_query", "")).strip()
            rows = service.list_bots_for(user_id)
            if search_query:
                rows = [row for row in rows if bot_matches(row, search_query)]
            locale = locale_for_update(update)
            text, page = format_bot_page(
                rows, page=int(raw_page), locale=locale, empty_key="search.empty"
            )
            await edit_or_reply_html(
                update,
                tr(
                    update,
                    "search.results",
                    query=escape(search_query or "all"),
                    count=str(len(rows)),
                    results=text,
                ),
                reply_markup=(
                    bots_keyboard(
                        rows,
                        action=action,
                        locale=locale,
                        page=page,
                        pager_prefix="search_page",
                    )
                    if rows
                    else search_empty_keyboard(locale)
                ),
            )
            return
        if data == "nav:id":
            await edit_or_reply_html(
                update,
                format_user_profile(update, service.is_owner(user_id))
                + "\n\n"
                + format_credit_overview(update, service, user_id),
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
        if data.startswith("pick_page:"):
            _, action, raw_page = data.split(":", 2)
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
            rows = service.list_bots_for(user_id)
            await edit_or_reply_html(
                update,
                f"<b>{escape(titles.get(action, 'Choose a bot:'))}</b>",
                reply_markup=bots_keyboard(
                    rows,
                    action=action,
                    locale=locale_for_update(update),
                    page=int(raw_page),
                    pager_prefix="pick_page",
                ),
            )
            return
        if data == "noop":
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
            progress_message = await edit_or_reply_html(
                update,
                progress_text(
                    update,
                    tr(update, "ai.progress_autofix_title"),
                    tr(update, "ai.progress_autofix_detail"),
                    0,
                    max_tokens=service.settings.openrouter_coding_max_tokens,
                ),
            )
            result = await run_with_progress(
                update,
                context,
                progress_message,
                service.auto_fix_bot(
                    user_id, bot_id, user_context=user_context_for_ai(update)
                ),
                "ai.progress_autofix_title",
                "ai.progress_autofix_detail",
                max_tokens=service.settings.openrouter_coding_max_tokens,
            )
            await edit_or_reply_result(
                update,
                append_ai_usage(result.message, result.ai_usage, locale_for_update(update)),
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
                                tr(update, "button.keep_bot"), callback_data=f"status:{bot_id}"
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
        progress_message = await reply_html(
            update.effective_message,
            progress_text(
                update,
                tr(update, "ai.progress_autofix_title"),
                tr(update, "ai.progress_autofix_detail"),
                0,
                max_tokens=service.settings.openrouter_coding_max_tokens,
            ),
        )
        result = await run_with_progress(
            update,
            context,
            progress_message,
            service.auto_fix_bot(
                user_id, bot_id, user_context=user_context_for_ai(update)
            ),
            "ai.progress_autofix_title",
            "ai.progress_autofix_detail",
            max_tokens=service.settings.openrouter_coding_max_tokens,
        )
        await edit_message_result(
            progress_message,
            append_ai_usage(result.message, result.ai_usage, locale_for_update(update)),
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
            format_paid_action_intro(
                update,
                service,
                ACTION_REVISE,
                tr(update, "revise.start", title=escape(bot_title(row))),
            ),
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
            progress_text(
                update,
                tr(update, "ai.progress_revise_title"),
                tr(update, "ai.progress_revise_detail"),
                0,
                max_tokens=service.settings.openrouter_coding_max_tokens,
            ),
        )
        result = await run_with_progress(
            update,
            context,
            progress_message,
            service.revise_bot(
                user_id,
                bot_id,
                prompt,
                user_context=str(context.user_data.get("revise_user_context", "")),
            ),
            "ai.progress_revise_title",
            "ai.progress_revise_detail",
            max_tokens=service.settings.openrouter_coding_max_tokens,
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
            append_ai_usage(result.message, result.ai_usage, locale_for_update(update)),
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
            format_paid_action_intro(
                update,
                service,
                ACTION_EDIT,
                tr(update, "edit.start", title=escape(bot_title(row))),
            ),
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
        if "edit_credit_reservation_id" not in context.user_data:
            gate = service.reserve_paid_action(user_id, ACTION_EDIT, bot_id=bot_id)
            if not gate.ok:
                await reply_html(
                    update.effective_message,
                    format_credit_gate(update, gate),
                    reply_markup=credits_keyboard(locale_for_update(update)),
                )
                context.user_data.clear()
                return ConversationHandler.END
            if gate.reservation_id is not None:
                context.user_data["edit_credit_reservation_id"] = gate.reservation_id
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
            update.effective_message,
            progress_text(
                update,
                tr(update, "ai.progress_edit_title"),
                tr(update, "ai.progress_edit_detail"),
                0,
                max_tokens=service.settings.openrouter_interaction_max_tokens,
            ),
        )
        try:
            edit_stream_queue, edit_on_delta = make_stream_queue()
            decision = await run_with_progress(
                update,
                context,
                progress_message,
                asyncio.to_thread(
                    service.plan_edit_bot,
                    user_id,
                    bot_id,
                    prompt,
                    answers,
                    force_code=force_code,
                    user_context=str(context.user_data.get("edit_user_context", "")),
                    on_delta=edit_on_delta,
                ),
                "ai.progress_edit_title",
                "ai.progress_edit_detail",
                max_tokens=service.settings.openrouter_interaction_max_tokens,
                stream_queue=edit_stream_queue,
                stream_visible_text=True,
            )
        except Exception:
            refund_flow_credits(context, "Edit planning failed")
            raise
        if isinstance(decision, OperationResult):
            refund_flow_credits(context, "Edit could not start")
            await edit_message_result(progress_message, decision.message)
            context.user_data.clear()
            return ConversationHandler.END
        if decision.needs_questions:
            if force_code:
                await edit_message_html(
                    progress_message,
                    tr(update, "edit.more_detail"),
                )
                refund_flow_credits(context, "Edit exceeded follow-up limit")
                context.user_data.clear()
                return ConversationHandler.END
            context.user_data["edit_pending_questions"] = question_texts(decision)
            await edit_message_html(
                progress_message,
                append_ai_usage(
                    format_ai_questions(decision),
                    decision.ai_usage,
                    locale_for_update(update),
                ),
                reply_markup=ai_response_keyboard("edit", locale_for_update(update)),
            )
            return EDIT_FOLLOWUP

        context.user_data["edit_plan_usage"] = decision.ai_usage
        await send_coding_brief_messages(
            update, str(decision.code or ""), title_key="ai.edit_brief_title"
        )
        await edit_message_html(
            progress_message,
            progress_text(
                update,
                tr(update, "ai.progress_edit_apply_title"),
                tr(update, "ai.progress_edit_apply_detail"),
                0,
                max_tokens=service.settings.openrouter_coding_max_tokens,
            ),
        )
        reservation_id = context.user_data.get("edit_credit_reservation_id")
        try:
            result = await run_with_progress(
                update,
                context,
                progress_message,
                service.edit_bot_from_decision(
                    user_id,
                    bot_id,
                    prompt,
                    decision,
                    user_context=str(context.user_data.get("edit_user_context", "")),
                ),
                "ai.progress_edit_apply_title",
                "ai.progress_edit_apply_detail",
                max_tokens=service.settings.openrouter_coding_max_tokens,
            )
        except Exception:
            service.refund_paid_action(reservation_id, "Edit failed")
            raise
        if result.ok or "saved" in result.message.lower():
            service.settle_paid_action(reservation_id, bot_id=bot_id, note="Edit saved")
        else:
            service.refund_paid_action(reservation_id, "Edit did not save")
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
            append_ai_usage(
                result.message,
                combine_ai_usage(
                    context.user_data.get("edit_plan_usage"),
                    result.ai_usage,
                ),
                locale_for_update(update),
            ),
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
        refund_flow_credits(context, "User re-prompted Edit")
        context.user_data.clear()
        context.user_data["edit_bot_id"] = bot_id
        context.user_data["edit_user_context"] = user_context
        await edit_or_reply_html(
            update,
            format_paid_action_intro(
                update,
                service,
                ACTION_EDIT,
                tr(update, "edit.reprompt", title=escape(bot_title(row))),
            ),
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
                BotCommand("credits", "Show your credit balance"),
                BotCommand("newbot", "Create and launch a child bot"),
                BotCommand("bots", "List your child bots"),
                BotCommand("search", "Search your child bots"),
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
        restoring_rows = service.db.running_bots()
        await service.runner.restore_running_bots()
        service.db.reset_runtime_meter_for_users(
            int(row["owner_user_id"]) for row in restoring_rows
        )
        if service.settings.credits_enabled:
            async def runtime_meter() -> None:
                while True:
                    await asyncio.sleep(
                        max(1, service.settings.credit_runtime_meter_interval_seconds)
                    )
                    try:
                        await service.bill_runtime_once(application.bot)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Runtime credit billing failed")

            application.bot_data["runtime_credit_task"] = asyncio.create_task(runtime_meter())

    async def post_shutdown(application) -> None:
        logger.info("Post-shutdown: stopping active child bots")
        task = application.bot_data.get("runtime_credit_task")
        if task is not None:
            task.cancel()
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
    restart_newbot_handlers = [
        CallbackQueryHandler(newbot_button, pattern="^nav:newbot$"),
        MessageHandler(filters.Regex("^🪄 New Bot$"), newbot),
    ]

    newbot_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newbot", newbot),
            MessageHandler(filters.Regex("^🪄 New Bot$"), newbot),
            CallbackQueryHandler(newbot_button, pattern="^nav:newbot$"),
            CallbackQueryHandler(newbot_reprompt, pattern=r"^reprompt:newbot$"),
            CallbackQueryHandler(
                newbot_template_entry, pattern=NEWBOT_TEMPLATE_CALLBACK_PATTERN
            ),
            CallbackQueryHandler(
                newbot_examples_entry, pattern=f"^{NEWBOT_EXAMPLES_CALLBACK}$"
            ),
        ],
        states={
            NEW_PROMPT: [
                *restart_newbot_handlers,
                CallbackQueryHandler(
                    newbot_template, pattern=NEWBOT_TEMPLATE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    newbot_examples_button, pattern=f"^{NEWBOT_EXAMPLES_CALLBACK}$"
                ),
                MessageHandler(conversation_text, newbot_prompt),
            ],
            NEW_FOLLOWUP: [
                *restart_newbot_handlers,
                CallbackQueryHandler(
                    newbot_template_entry, pattern=NEWBOT_TEMPLATE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    newbot_examples_entry, pattern=f"^{NEWBOT_EXAMPLES_CALLBACK}$"
                ),
                CallbackQueryHandler(newbot_reprompt, pattern=r"^reprompt:newbot$"),
                MessageHandler(conversation_text, newbot_followup),
            ],
            NEW_TOKEN: [
                *restart_newbot_handlers,
                CallbackQueryHandler(
                    newbot_template_entry, pattern=NEWBOT_TEMPLATE_CALLBACK_PATTERN
                ),
                CallbackQueryHandler(
                    newbot_examples_entry, pattern=f"^{NEWBOT_EXAMPLES_CALLBACK}$"
                ),
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
    application.add_handler(CommandHandler("credits", credits))
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
    application.add_handler(CommandHandler("search", search))
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

