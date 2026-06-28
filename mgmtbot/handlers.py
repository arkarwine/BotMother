from __future__ import annotations

"""
Management bot handlers — mirrors BotMother's UX conventions exactly:
  - /start removes old reply keyboard, sends reply keyboard + inline home menu (two messages)
  - ParseMode.HTML + html.escape everywhere
  - InlineKeyboardMarkup for all actions; ReplyKeyboardMarkup (persistent) for quick access
  - edit_or_reply_html pattern: edits on callback, replies fresh on command
  - format_result_html: bolds first non-empty line
  - chunk_text for long outputs
  - nav:home / nav:cancel / nav:back callback prefixes
  - Confirmation keyboards (Yes / Cancel two-button row) before destructive actions
  - Progress messages (send plain, edit in place with result)
  - ConversationHandler for broadcast compose flow
"""

import logging
import re
from html import escape
from typing import Any

from .db import (
    MgmtDatabase,
    ROLE_ADMIN,
    ROLE_B2B,
    TARGET_ALL_USERS,
    TARGET_ALL_CHATS,
    TARGET_BOT_OWNERS,
    TARGET_CUSTOM,
)
from .stats import gather_stats, format_stats_card
from .broadcaster import broadcast_message

logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
(
    BROADCAST_TARGET,
    BROADCAST_CUSTOM_IDS,
    BROADCAST_COMPOSE,
    BROADCAST_CONFIRM,
    ADMIN_ADD_USER,
) = range(5)

# ─── Status emoji (same as BotMother) ─────────────────────────────────────────
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

TARGET_LABELS = {
    TARGET_ALL_USERS: "👥 All Users",
    TARGET_ALL_CHATS: "💬 All Chats",
    TARGET_BOT_OWNERS: "🤖 Bot Owners",
    TARGET_CUSTOM: "✏️ Custom List",
}

PAGE_SIZE = 10


def clamp_page(total: int, page: int, page_size: int = PAGE_SIZE) -> int:
    max_page = max(0, (max(0, total) - 1) // page_size)
    return max(0, min(page, max_page))


def page_slice(rows: list[Any], page: int, page_size: int = PAGE_SIZE) -> tuple[list[Any], int, int]:
    page = clamp_page(len(rows), page, page_size)
    start = page * page_size
    total_pages = max(1, ((len(rows) - 1) // page_size) + 1) if rows else 1
    return rows[start : start + page_size], page, total_pages


def build_application(settings: Any, db: MgmtDatabase):  # type: ignore[return]
    try:
        from telegram import (
            Bot,
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

    # ─── Helper: check access ─────────────────────────────────────────────────

    def _user_id(update: Update) -> int:
        return int(update.effective_user.id)

    def _is_owner(user_id: int) -> bool:
        return settings.owner_id is not None and user_id == settings.owner_id

    def _is_authorized(user_id: int) -> bool:
        return _is_owner(user_id) or db.is_admin(user_id)

    def _is_admin_role(user_id: int) -> bool:
        """Owner or admin (not b2b only)."""
        if _is_owner(user_id):
            return True
        role = db.get_role(user_id)
        return role == ROLE_ADMIN

    def _role_label(user_id: int) -> str:
        if _is_owner(user_id):
            return "👑 Owner"
        role = db.get_role(user_id)
        if role == ROLE_ADMIN:
            return "🛡️ Admin"
        if role == ROLE_B2B:
            return "🏢 B2B"
        return "—"

    # ─── Reply keyboards ──────────────────────────────────────────────────────

    def main_reply_keyboard():
        return ReplyKeyboardMarkup(
            [
                ["📊 Dashboard", "📢 Broadcast"],
                ["🤖 Bots", "📋 Logs"],
                ["📈 Stats", "👥 Users"],
                ["💳 Credits"],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    def owner_reply_keyboard():
        return ReplyKeyboardMarkup(
            [
                ["📊 Dashboard", "📢 Broadcast"],
                ["🤖 Bots", "📋 Logs"],
                ["📈 Stats", "👥 Users"],
                ["💳 Credits"],
                ["⚙️ Admins", "📜 History"],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    def reply_keyboard_for(user_id: int):
        if _is_admin_role(user_id):
            return owner_reply_keyboard()
        return main_reply_keyboard()

    remove_keyboard = ReplyKeyboardRemove()

    # ─── Inline keyboards ─────────────────────────────────────────────────────

    def home_keyboard(user_id: int):
        rows = [
            [
                InlineKeyboardButton("📊 Dashboard", callback_data="nav:dashboard"),
                InlineKeyboardButton("📢 Broadcast", callback_data="nav:broadcast"),
            ],
            [
                InlineKeyboardButton("🤖 Bots", callback_data="nav:bots"),
                InlineKeyboardButton("📋 Logs", callback_data="pick:logs"),
            ],
            [
                InlineKeyboardButton("📈 Stats", callback_data="nav:stats"),
                InlineKeyboardButton("👥 Users", callback_data="nav:users"),
            ],
            [
                InlineKeyboardButton("💳 Credits", callback_data="nav:credits"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="nav:history"),
            ],
        ]
        if _is_admin_role(user_id):
            rows.append([InlineKeyboardButton("⚙️ Admins", callback_data="nav:admins")])
        return InlineKeyboardMarkup(rows)

    def back_home_keyboard():
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
        )

    def back_home_bots_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🤖 All Bots", callback_data="nav:bots"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ]
            ]
        )

    def paged_keyboard(prefix: str, page: int, total_pages: int, extra_rows=None):
        rows = list(extra_rows or [])
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{page + 1}"))
            rows.append(nav)
        rows.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
        return InlineKeyboardMarkup(rows)

    def cancel_keyboard():
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]]
        )

    def confirm_broadcast_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Send Now", callback_data="bc:confirm"),
                    InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
                ]
            ]
        )

    def bot_actions_keyboard(bot_id: int):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📊 Status", callback_data=f"botstatus:{bot_id}"),
                    InlineKeyboardButton("📋 Logs", callback_data=f"botlogs:{bot_id}"),
                ],
                [
                    InlineKeyboardButton("📢 Broadcast", callback_data=f"botbroadcast:{bot_id}"),
                ],
                [
                    InlineKeyboardButton("🤖 All Bots", callback_data="nav:bots"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ],
            ]
        )

    def bots_picker_keyboard(bots: list, action: str = "botstatus", page: int = 0):
        if not bots:
            return back_home_keyboard()
        visible_bots, page, total_pages = page_slice(bots, page)
        buttons = [
            [
                InlineKeyboardButton(
                    f"{STATUS_EMOJI.get(str(b['status']), '•')} {str(b['name'])[:32]}",
                    callback_data=f"{action}:{b['id']}",
                )
            ]
            for b in visible_bots
        ]
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀️", callback_data=f"page:bots:{action}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("▶️", callback_data=f"page:bots:{action}:{page + 1}"))
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
        return InlineKeyboardMarkup(buttons)

    def broadcast_target_keyboard(user_id: int):
        rows = [
            [
                InlineKeyboardButton(TARGET_LABELS[TARGET_ALL_USERS], callback_data=f"bctarget:{TARGET_ALL_USERS}"),
                InlineKeyboardButton(TARGET_LABELS[TARGET_ALL_CHATS], callback_data=f"bctarget:{TARGET_ALL_CHATS}"),
            ],
            [
                InlineKeyboardButton(TARGET_LABELS[TARGET_BOT_OWNERS], callback_data=f"bctarget:{TARGET_BOT_OWNERS}"),
                InlineKeyboardButton(TARGET_LABELS[TARGET_CUSTOM], callback_data=f"bctarget:{TARGET_CUSTOM}"),
            ],
        ]
        # B2B users only see their own targets
        if not _is_admin_role(user_id):
            rows = [
                [InlineKeyboardButton("💬 My Bot Chats", callback_data=f"bctarget:own_chats")],
            ]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")])
        return InlineKeyboardMarkup(rows)

    def admins_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("➕ Add Admin", callback_data="admin:add_prompt"),
                    InlineKeyboardButton("🏢 Add B2B", callback_data="admin:addb2b_prompt"),
                ],
                [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
            ]
        )

    def credits_keyboard():
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("👥 Low Balances", callback_data="nav:credits"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ]
            ]
        )

    # ─── Formatting helpers (mirrors BotMother exactly) ──────────────────────

    def format_result_html(text: str) -> str:
        """Bold the first non-empty line."""
        lines = text.splitlines()
        first_idx = next(
            (i for i, line in enumerate(lines) if line.strip()), None
        )
        if first_idx is None:
            return ""
        formatted = []
        for i, line in enumerate(lines):
            escaped = escape(line)
            formatted.append(f"<b>{escaped}</b>" if i == first_idx else escaped)
        return "\n".join(formatted)

    def chunk_text(text: str, chunk_size: int = 3200) -> list[str]:
        if not text:
            return [""]
        return [text[i: i + chunk_size] for i in range(0, len(text), chunk_size)]

    def status_badge(status: str) -> str:
        return f"{STATUS_EMOJI.get(status, '•')} {status}"

    def owner_label(row: Any) -> str:
        username = row["owner_username"]
        if username:
            return f"@{username}"
        first = str(row["owner_first_name"] or "").strip()
        last = str(row["owner_last_name"] or "").strip()
        full = " ".join(p for p in (first, last) if p)
        return full or "Owner"

    def format_bot_list(bots: list) -> str:
        if not bots:
            return "No bots yet."
        lines = ["<b>🤖 Child Bots</b>", ""]
        for b in bots:
            badge = escape(status_badge(str(b["status"])))
            name = escape(str(b["name"]))
            owner = escape(owner_label(b))
            uname = str(b["bot_username"] or "").strip().lstrip("@")
            line = f"{badge}  <b>{name}</b>"
            if uname:
                line += f"\n<i>@{escape(uname)}</i>"
            line += f"\n<i>Owner: {owner}</i>"
            lines.append(line)
        return "\n".join(lines)

    def format_bot_detail(b: Any) -> str:
        name = escape(str(b["name"]))
        status = escape(status_badge(str(b["status"])))
        owner = escape(owner_label(b))
        uname = str(b["bot_username"] or "").strip().lstrip("@")
        pid = b["pid"]
        created = b["created_at"]
        lines = [
            f"<b>📦 {name}</b>",
            "",
            f"<b>Status</b>\n{status}",
            f"<b>Owner</b>\n{owner}",
        ]
        if uname:
            lines.append(f"<b>Username</b>\n@{escape(uname)}")
        if pid:
            lines.append(f"<b>PID</b>\n<code>{pid}</code>")
        if created:
            import datetime
            dt = datetime.datetime.utcfromtimestamp(int(created))
            lines.append(f"<b>Created</b>\n{dt.strftime('%Y-%m-%d %H:%M UTC')}")
        return "\n".join(lines)

    def format_logs(rows: list) -> str:
        if not rows:
            return "(no logs)"
        lines = []
        for row in rows:
            line = str(row["line"]).replace("\n", " ")
            lines.append(f"[{row['stream']}] {line}")
        text = "\n".join(lines)
        return text[-3500:] if len(text) > 3500 else text

    def format_users_summary(users: list, total_count: int | None = None) -> str:
        if not users:
            return "No users yet."
        total = len(users) if total_count is None else total_count
        lines = ["<b>👥 Users</b>", f"Total: <code>{total}</code>", ""]
        for u in users:
            uname = str(u["username"] or "").strip()
            first = str(u["first_name"] or "").strip()
            last = str(u["last_name"] or "").strip()
            display = f"@{uname}" if uname else " ".join(p for p in (first, last) if p) or str(u["user_id"])
            lines.append(f"<code>{u['user_id']}</code>  {escape(display)}")
        return "\n".join(lines)

    def format_admins_list(admins: list) -> str:
        if not admins:
            return "<b>⚙️ Admin List</b>\n\nNo admins yet."
        lines = ["<b>⚙️ Admin List</b>", ""]
        for a in admins:
            role = str(a["role"])
            icon = "🛡️" if role == ROLE_ADMIN else "🏢"
            uname = str(a["username"] or "").strip()
            first = str(a["first_name"] or "").strip()
            display = f"@{uname}" if uname else first or str(a["user_id"])
            lines.append(f"{icon} <b>{escape(display)}</b>  <code>{a['user_id']}</code>  <i>{role}</i>")
        return "\n".join(lines)

    def format_broadcast_history(broadcasts: list) -> str:
        if not broadcasts:
            return "<b>📜 Broadcast History</b>\n\nNo broadcasts yet."
        lines = ["<b>📜 Broadcast History</b>", ""]
        for bc in broadcasts:
            import datetime
            dt = datetime.datetime.utcfromtimestamp(int(bc["created_at"]))
            target = str(bc["target_group"])
            total = bc["total_targets"]
            sent = bc["sent_count"]
            fail = bc["fail_count"]
            status = str(bc["status"])
            sender_name = str(bc["sender_username"] or bc["sender_first_name"] or bc["sent_by"])
            msg_preview = str(bc["message_text"] or "")[:40].replace("\n", " ")
            lines.append(
                f"<b>{dt.strftime('%m-%d %H:%M')}</b>  {escape(target)}\n"
                f"  By: {escape(sender_name)}  |  {sent}/{total} sent  |  {fail} failed\n"
                f"  <i>{escape(msg_preview)}…</i>"
            )
        return "\n\n".join(lines)

    def user_display(row: Any) -> str:
        username = str(row["username"] or "").strip()
        first = str(row["first_name"] or "").strip()
        last = str(row["last_name"] or "").strip()
        return f"@{username}" if username else " ".join(p for p in (first, last) if p) or str(row["user_id"])

    def format_credit_dashboard(page: int = 0) -> str:
        summary = db.credit_summary()
        all_accounts = db.list_credit_accounts(1000)
        accounts, page, total_pages = page_slice(all_accounts, page)
        ledger = db.recent_credit_ledger(8)
        lines = [
            "<b>💳 Credit Dashboard</b>",
            "",
            f"<b>Accounts</b>\n<code>{summary['account_count']}</code>",
            f"<b>Total issued</b>\n<code>{summary['total_issued']}</code>",
            f"<b>Total spent</b>\n<code>{summary['total_spent']}</code>",
            f"<b>Runtime spent</b>\n<code>{summary['runtime_spent']}</code>",
            f"<b>Low balances</b>\n<code>{summary['low_balance_users']}</code>",
            "",
            "<b>Lowest balances</b>",
        ]
        if accounts:
            for account in accounts:
                lines.append(
                    f"<code>{account['user_id']}</code>  {escape(user_display(account))}  "
                    f"<b>{account['balance']}</b>"
                )
        else:
            lines.append("No credit accounts yet.")
        if all_accounts:
            lines.append(f"\n<i>Page {page + 1}/{total_pages} · {len(all_accounts)} accounts</i>")
        lines.append("")
        lines.append("<b>Recent ledger</b>")
        if ledger:
            for row in ledger:
                sign = "+" if int(row["amount"]) > 0 else ""
                lines.append(
                    f"<code>{row['user_id']}</code> {sign}{row['amount']} "
                    f"{escape(str(row['action']))} → <code>{row['balance_after']}</code>"
                )
        else:
            lines.append("No ledger rows yet.")
        lines.append("")
        lines.append("<i>Use /usercredits, /grantcredits, or /setcredits.</i>")
        return "\n".join(lines)

    def format_user_credits(user_id: int) -> str:
        balance = db.credit_balance(user_id, settings.credits_initial_free)
        ledger = db.credit_ledger_for_user(user_id, 12)
        bots = db.list_bots_for_owner(user_id)
        lines = [
            "<b>💳 User Credits</b>",
            "",
            f"<b>User ID</b>\n<code>{user_id}</code>",
            f"<b>Balance</b>\n<code>{balance}</code>",
            f"<b>Active bots</b>\n<code>{len(bots)}</code>",
            "",
            "<b>Recent transactions</b>",
        ]
        if ledger:
            for row in ledger:
                sign = "+" if int(row["amount"]) > 0 else ""
                note = str(row["note"] or "")
                lines.append(
                    f"{sign}{row['amount']}  {escape(str(row['kind']))}/"
                    f"{escape(str(row['action']))} → <code>{row['balance_after']}</code>"
                    + (f"\n<i>{escape(note[:80])}</i>" if note else "")
                )
        else:
            lines.append("No transactions yet.")
        return "\n".join(lines)

    # ─── Core send helpers (exact BotMother pattern) ─────────────────────────

    async def reply_html(message, text: str, reply_markup=None):
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )

    async def edit_message_html(message, text: str, reply_markup=None):
        can_edit = reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup)
        if can_edit:
            try:
                await message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                logger.debug("Could not edit message; sending new one", exc_info=True)
        await reply_html(message, text, reply_markup=reply_markup)

    async def edit_or_reply_html(update: Update, text: str, reply_markup=None):
        query = update.callback_query
        can_edit = reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup)
        if query is not None and update.effective_message is not None and can_edit:
            try:
                await query.edit_message_text(
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                logger.debug("Could not edit callback message; sending new one", exc_info=True)
        await reply_html(update.effective_message, text, reply_markup=reply_markup)

    async def edit_or_reply_result(update: Update, text: str, reply_markup=None):
        await edit_or_reply_html(update, format_result_html(text), reply_markup=reply_markup)

    # ─── Access guard ─────────────────────────────────────────────────────────

    async def deny(update: Update):
        await reply_html(
            update.effective_message,
            "🔒 <b>Access Denied</b>\n\nYou are not authorised to use this bot.\n\n"
            "Contact the owner to request access.",
        )

    async def guard(update: Update) -> bool:
        uid = _user_id(update)
        if not _is_authorized(uid):
            await deny(update)
            return False
        return True

    # ─── /start ───────────────────────────────────────────────────────────────

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = _user_id(update)
        if not _is_authorized(uid):
            await deny(update)
            return
        logger.info("Command /start: user_id=%s", uid)
        # Step 1: remove old keyboard with a plain text message
        await reply_html(
            update.effective_message,
            "🏢 <b>BotMother Management</b>\n\nWelcome back.",
            reply_markup=remove_keyboard,
        )
        # Step 2: send persistent reply keyboard + inline home menu
        await reply_html(
            update.effective_message,
            "Choose an action:",
            reply_markup=reply_keyboard_for(uid),
        )
        await reply_html(
            update.effective_message,
            "<b>🏠 Management Home</b>",
            reply_markup=home_keyboard(uid),
        )

    # ─── Dashboard ────────────────────────────────────────────────────────────

    async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Dashboard: user_id=%s", uid)
        stats = gather_stats(db)
        card = format_stats_card(stats)
        await edit_or_reply_html(
            update,
            card,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔄 Refresh", callback_data="nav:dashboard"),
                        InlineKeyboardButton("📈 Full Stats", callback_data="nav:stats"),
                    ],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            ),
        )

    # ─── Stats ────────────────────────────────────────────────────────────────

    async def full_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Stats: user_id=%s", uid)
        stats = gather_stats(db)
        # Detailed breakdown
        status_lines = [
            f"  {STATUS_EMOJI.get(k, '•')} {escape(k)}: <code>{v}</code>"
            for k, v in sorted(stats["status_counts"].items())
        ]
        text = (
            format_stats_card(stats)
            + "\n\n<b>All Status Counts</b>\n"
            + "\n".join(status_lines)
        )
        await edit_or_reply_html(
            update,
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
            ),
        )

    # ─── Bot list ─────────────────────────────────────────────────────────────

    async def bots_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Bots list: user_id=%s", uid)

        if _is_admin_role(uid):
            bots = db.list_all_bots()
        else:
            bots = db.list_bots_for_owner(uid)

        visible_bots, page, total_pages = page_slice(bots, page)
        text = format_bot_list(visible_bots)
        if bots:
            text += f"\n\n<i>Page {page + 1}/{total_pages} · {len(bots)} total</i>"
        await edit_or_reply_html(
            update,
            text,
            reply_markup=bots_picker_keyboard(bots, page=page),
        )

    # ─── Bot detail + logs ────────────────────────────────────────────────────

    async def show_bot_status(update: Update, bot_id: int) -> None:
        b = db.get_bot(bot_id)
        if b is None:
            await edit_or_reply_html(update, "Bot not found.")
            return
        text = format_bot_detail(b)
        await edit_or_reply_html(
            update,
            text,
            reply_markup=bot_actions_keyboard(bot_id),
        )

    async def show_bot_logs(update: Update, bot_id: int) -> None:
        b = db.get_bot(bot_id)
        if b is None:
            await edit_or_reply_html(update, "Bot not found.")
            return
        rows = db.get_logs(bot_id, 50)
        logs_text = format_logs(rows)
        name = escape(str(b["name"]))
        await edit_or_reply_html(
            update,
            f"<b>📋 Logs — {name}</b>\n\n<pre>{escape(logs_text)}</pre>",
            reply_markup=bot_actions_keyboard(bot_id),
        )

    async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Logs command: user_id=%s args=%s", uid, context.args)
        if context.args:
            try:
                bot_id = int(context.args[0])
            except ValueError:
                await reply_html(update.effective_message, "Usage: /logs &lt;bot_id&gt;")
                return
            await show_bot_logs(update, bot_id)
            return

        if _is_admin_role(uid):
            bots = db.list_all_bots()
        else:
            bots = db.list_bots_for_owner(uid)

        await reply_html(
            update.effective_message,
            "<b>📋 Choose a bot to view logs:</b>",
            reply_markup=bots_picker_keyboard(bots, action="botlogs"),
        )

    # ─── Users ────────────────────────────────────────────────────────────────

    async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Users: user_id=%s", uid)
        users = db.list_all_users()
        visible_users, page, total_pages = page_slice(users, page)
        text = format_users_summary(visible_users, total_count=len(users))
        if users:
            text += f"\n\n<i>Page {page + 1}/{total_pages} · {len(users)} total</i>"
        await edit_or_reply_html(
            update,
            text,
            reply_markup=paged_keyboard("page:users", page, total_pages),
        )

    # ─── Admin management ─────────────────────────────────────────────────────

    async def admins_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await edit_or_reply_html(update, "🔒 This section is for admins only.")
            return
        logger.info("Admins panel: user_id=%s", uid)
        admins = db.list_admins()
        text = format_admins_list(admins)
        await edit_or_reply_html(
            update,
            text,
            reply_markup=admins_keyboard(),
        )

    async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        if not context.args or len(context.args) < 1:
            await reply_html(
                update.effective_message,
                "Usage: /addadmin &lt;user_id&gt; [username]"
            )
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await reply_html(update.effective_message, "Invalid user ID.")
            return
        username = context.args[1].lstrip("@") if len(context.args) > 1 else None
        db.add_admin(target_id, username, None, None, ROLE_ADMIN, uid)
        logger.info("Added admin: added_by=%s target=%s", uid, target_id)
        await reply_html(
            update.effective_message,
            f"✅ <b>Admin added</b>\n\nUser <code>{target_id}</code> now has admin access.",
            reply_markup=back_home_keyboard(),
        )

    async def addb2b_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        if not context.args or len(context.args) < 1:
            await reply_html(
                update.effective_message,
                "Usage: /addb2b &lt;user_id&gt; [username]"
            )
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await reply_html(update.effective_message, "Invalid user ID.")
            return
        username = context.args[1].lstrip("@") if len(context.args) > 1 else None
        db.add_admin(target_id, username, None, None, ROLE_B2B, uid)
        logger.info("Added B2B user: added_by=%s target=%s", uid, target_id)
        await reply_html(
            update.effective_message,
            f"✅ <b>B2B user added</b>\n\nUser <code>{target_id}</code> now has B2B access.",
            reply_markup=back_home_keyboard(),
        )

    async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        if not context.args:
            await reply_html(update.effective_message, "Usage: /removeadmin &lt;user_id&gt;")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await reply_html(update.effective_message, "Invalid user ID.")
            return
        if target_id == settings.owner_id:
            await reply_html(update.effective_message, "Cannot remove the owner.")
            return
        removed = db.remove_admin(target_id)
        logger.info("Removed admin: removed_by=%s target=%s rows=%s", uid, target_id, removed)
        if removed:
            await reply_html(
                update.effective_message,
                f"✅ User <code>{target_id}</code> removed.",
                reply_markup=back_home_keyboard(),
            )
        else:
            await reply_html(
                update.effective_message,
                f"User <code>{target_id}</code> was not in the admin list.",
            )

    async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        await admins_panel(update, context)

    async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await guard(update):
            return ConversationHandler.END
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await edit_or_reply_html(update, "🔒 Admins only.")
            return ConversationHandler.END
        query = update.callback_query
        role = ROLE_ADMIN
        if query is not None and query.data == "admin:addb2b_prompt":
            role = ROLE_B2B
        context.user_data.clear()
        context.user_data["admin_add_role"] = role
        title = "Add Admin" if role == ROLE_ADMIN else "Add B2B User"
        await edit_or_reply_html(
            update,
            f"<b>➕ {title}</b>\n\n"
            "Send the Telegram user ID, optionally followed by username.\n\n"
            "<b>Example</b>\n<code>123456789 @username</code>",
            reply_markup=cancel_keyboard(),
        )
        return ADMIN_ADD_USER

    async def admin_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await guard(update):
            return ConversationHandler.END
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return ConversationHandler.END
        role = str(context.user_data.get("admin_add_role") or ROLE_ADMIN)
        text = (update.effective_message.text or "").strip()
        parts = text.split()
        if not parts:
            await reply_html(
                update.effective_message,
                "Please send a Telegram user ID, optionally followed by username.",
                reply_markup=cancel_keyboard(),
            )
            return ADMIN_ADD_USER
        try:
            target_id = int(parts[0])
        except ValueError:
            await reply_html(
                update.effective_message,
                "Invalid user ID. Send numbers only, for example <code>123456789</code>.",
                reply_markup=cancel_keyboard(),
            )
            return ADMIN_ADD_USER
        username = parts[1].lstrip("@") if len(parts) > 1 else None
        db.add_admin(target_id, username, None, None, role, uid)
        logger.info("Added mgmt access from button flow: added_by=%s target=%s role=%s", uid, target_id, role)
        label = "Admin" if role == ROLE_ADMIN else "B2B user"
        context.user_data.clear()
        await reply_html(
            update.effective_message,
            f"✅ <b>{label} added</b>\n\n"
            f"User <code>{target_id}</code> now has <code>{escape(role)}</code> access.",
            reply_markup=admins_keyboard(),
        )
        return ConversationHandler.END

    async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        accounts = db.list_credit_accounts(1000)
        _, page, total_pages = page_slice(accounts, page)
        await edit_or_reply_html(
            update,
            format_credit_dashboard(page),
            reply_markup=paged_keyboard("page:credits", page, total_pages),
        )

    async def usercredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        if not context.args:
            await reply_html(update.effective_message, "Usage: /usercredits &lt;user_id&gt;")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await reply_html(update.effective_message, "Invalid user ID.")
            return
        await reply_html(
            update.effective_message,
            format_user_credits(target_id),
            reply_markup=credits_keyboard(),
        )

    async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        query = " ".join(context.args).strip().lower()
        if not query:
            await reply_html(
                update.effective_message,
                "<b>🔎 Search</b>\n\nUse <code>/search text</code> to search bots, users, credits, and broadcast history.",
                reply_markup=back_home_keyboard(),
            )
            return
        bots = db.list_all_bots() if _is_admin_role(uid) else db.list_bots_for_owner(uid)
        users = db.list_all_users() if _is_admin_role(uid) else []
        accounts = db.list_credit_accounts(1000) if _is_admin_role(uid) else []
        broadcasts = db.list_broadcasts(1000) if _is_admin_role(uid) else []

        def matches(row: Any, keys: tuple[str, ...]) -> bool:
            return query in " ".join(str(row[key] or "") for key in keys).lower()

        bot_matches_rows = [
            row for row in bots
            if matches(row, ("name", "bot_username", "status", "owner_username", "owner_first_name", "owner_last_name"))
        ][:10]
        user_matches_rows = [
            row for row in users
            if matches(row, ("user_id", "username", "first_name", "last_name"))
        ][:10]
        account_matches_rows = [
            row for row in accounts
            if matches(row, ("user_id", "username", "first_name", "last_name", "balance"))
        ][:10]
        broadcast_matches_rows = [
            row for row in broadcasts
            if matches(row, ("target_group", "message_text", "sent_by"))
        ][:5]

        lines = [
            "<b>🔎 Search Results</b>",
            "",
            f"Query: <code>{escape(query)}</code>",
            "",
            "<b>Bots</b>",
        ]
        if bot_matches_rows:
            for bot in bot_matches_rows:
                lines.append(f"{escape(status_badge(str(bot['status'])))}  <b>{escape(str(bot['name']))}</b>")
        else:
            lines.append("No bot matches.")
        lines.extend(["", "<b>Users</b>"])
        if user_matches_rows:
            for user in user_matches_rows:
                lines.append(f"<code>{user['user_id']}</code>  {escape(user_display(user))}")
        else:
            lines.append("No user matches.")
        lines.extend(["", "<b>Credit Accounts</b>"])
        if account_matches_rows:
            for account in account_matches_rows:
                lines.append(f"<code>{account['user_id']}</code>  {escape(user_display(account))}  <b>{account['balance']}</b>")
        else:
            lines.append("No credit matches.")
        lines.extend(["", "<b>Broadcasts</b>"])
        if broadcast_matches_rows:
            for bc in broadcast_matches_rows:
                preview = str(bc["message_text"] or "")[:60].replace("\n", " ")
                lines.append(f"{escape(str(bc['target_group']))}: <i>{escape(preview)}</i>")
        else:
            lines.append("No broadcast matches.")

        await reply_html(
            update.effective_message,
            "\n".join(lines),
            reply_markup=back_home_keyboard(),
        )

    async def grantcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_admin_role(uid):
            await reply_html(update.effective_message, "🔒 Admins only.")
            return
        if len(context.args) < 2:
            await reply_html(update.effective_message, "Usage: /grantcredits &lt;user_id&gt; &lt;amount&gt; [note]")
            return
        try:
            target_id = int(context.args[0])
            amount = int(context.args[1])
        except ValueError:
            await reply_html(update.effective_message, "User ID and amount must be numbers.")
            return
        if amount <= 0:
            await reply_html(update.effective_message, "Grant amount must be positive.")
            return
        if target_id == uid and not _is_owner(uid):
            await reply_html(update.effective_message, "Admins cannot grant credits to themselves.")
            return
        note = " ".join(context.args[2:]).strip() or "Manual admin grant"
        balance = db.grant_credits(
            target_id,
            amount,
            uid,
            settings.credits_initial_free,
            note=note,
        )
        logger.info("Credits granted: actor=%s target=%s amount=%s balance=%s", uid, target_id, amount, balance)
        await reply_html(
            update.effective_message,
            f"✅ <b>Credits granted</b>\n\nUser <code>{target_id}</code>\nAmount: <code>+{amount}</code>\nBalance: <code>{balance}</code>",
            reply_markup=credits_keyboard(),
        )

    async def setcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        if not _is_owner(uid):
            await reply_html(update.effective_message, "🔒 Owner only.")
            return
        if len(context.args) < 2:
            await reply_html(update.effective_message, "Usage: /setcredits &lt;user_id&gt; &lt;amount&gt; [note]")
            return
        try:
            target_id = int(context.args[0])
            amount = int(context.args[1])
        except ValueError:
            await reply_html(update.effective_message, "User ID and amount must be numbers.")
            return
        note = " ".join(context.args[2:]).strip() or "Owner balance correction"
        balance = db.set_credit_balance(
            target_id,
            amount,
            uid,
            settings.credits_initial_free,
            note=note,
        )
        logger.warning("Credits set: actor=%s target=%s balance=%s", uid, target_id, balance)
        await reply_html(
            update.effective_message,
            f"✅ <b>Credit balance set</b>\n\nUser <code>{target_id}</code>\nBalance: <code>{balance}</code>",
            reply_markup=credits_keyboard(),
        )

    # ─── Broadcast flow (ConversationHandler) ────────────────────────────────

    async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not await guard(update):
            return ConversationHandler.END
        uid = _user_id(update)
        logger.info("Broadcast start: user_id=%s", uid)
        context.user_data.clear()
        query = update.callback_query
        if query:
            await query.answer()
        await edit_or_reply_html(
            update,
            "<b>📢 New Broadcast</b>\n\nWho should receive this message?",
            reply_markup=broadcast_target_keyboard(uid),
        )
        return BROADCAST_TARGET

    async def broadcast_target_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        uid = _user_id(update)

        raw = (query.data if query else "").split(":", 1)
        target = raw[1] if len(raw) == 2 else TARGET_ALL_USERS

        context.user_data["bc_target"] = target
        context.user_data["bc_sender"] = uid

        if target == TARGET_CUSTOM:
            await edit_or_reply_html(
                update,
                "<b>📢 Custom Target</b>\n\nSend a comma-separated list of Telegram chat IDs:",
                reply_markup=cancel_keyboard(),
            )
            return BROADCAST_CUSTOM_IDS

        label = TARGET_LABELS.get(target, target)
        await edit_or_reply_html(
            update,
            f"<b>📢 Broadcast to {escape(label)}</b>\n\n"
            "Now type or send your message.\n\n"
            "<i>You can send a photo with a caption, or plain text.</i>",
            reply_markup=cancel_keyboard(),
        )
        return BROADCAST_COMPOSE

    async def broadcast_custom_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        uid = _user_id(update)
        text = (update.effective_message.text or "").strip()
        ids: list[int] = []
        for part in re.split(r"[\s,;]+", text):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                pass
        if not ids:
            await reply_html(
                update.effective_message,
                "No valid IDs found. Please send comma-separated numbers.",
                reply_markup=cancel_keyboard(),
            )
            return BROADCAST_CUSTOM_IDS

        context.user_data["bc_custom_ids"] = ids
        await reply_html(
            update.effective_message,
            f"<b>📢 Broadcast to {len(ids)} custom IDs</b>\n\n"
            "Now type or send your message.\n\n"
            "<i>You can send a photo with a caption, or plain text.</i>",
            reply_markup=cancel_keyboard(),
        )
        return BROADCAST_COMPOSE

    async def broadcast_compose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        uid = _user_id(update)
        message = update.effective_message

        media_file_id = None
        media_type = None

        if message.photo:
            media_file_id = message.photo[-1].file_id
            media_type = "photo"
            text = (message.caption or "").strip()
        elif message.document:
            media_file_id = message.document.file_id
            media_type = "document"
            text = (message.caption or "").strip()
        else:
            text = (message.text or "").strip()

        if not text and not media_file_id:
            await reply_html(
                message,
                "Message cannot be empty. Please send text or a photo/document with a caption.",
                reply_markup=cancel_keyboard(),
            )
            return BROADCAST_COMPOSE

        context.user_data["bc_text"] = text
        context.user_data["bc_media_file_id"] = media_file_id
        context.user_data["bc_media_type"] = media_type

        # Compute recipient count for preview
        target = str(context.user_data.get("bc_target", TARGET_ALL_USERS))
        custom_ids: list[int] = context.user_data.get("bc_custom_ids", [])

        if target == TARGET_CUSTOM or target == "own_chats":
            recipient_ids = custom_ids or []
            if target == "own_chats":
                recipient_ids = db.get_broadcast_targets_for_owner(uid)
                context.user_data["bc_resolved_ids"] = recipient_ids
        else:
            recipient_ids = db.get_broadcast_targets(target)
            context.user_data["bc_resolved_ids"] = recipient_ids

        count = len(recipient_ids)
        label = TARGET_LABELS.get(target, target)
        media_note = f"\n📎 <i>With {media_type}</i>" if media_type else ""

        preview_text = (
            f"<b>📢 Broadcast Preview</b>\n\n"
            f"<b>Target:</b> {escape(label)}\n"
            f"<b>Recipients:</b> <code>{count}</code>{media_note}\n\n"
            f"<b>Message:</b>\n{escape(text) if text else '<i>(caption only)</i>'}\n\n"
            "Send this broadcast?"
        )
        await reply_html(
            message,
            preview_text,
            reply_markup=confirm_broadcast_keyboard(),
        )
        return BROADCAST_CONFIRM

    async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        uid = _user_id(update)

        text = str(context.user_data.get("bc_text", ""))
        media_file_id = context.user_data.get("bc_media_file_id")
        media_type = context.user_data.get("bc_media_type")
        target = str(context.user_data.get("bc_target", TARGET_ALL_USERS))
        custom_ids: list[int] = context.user_data.get("bc_custom_ids", [])
        resolved_ids: list[int] = context.user_data.get("bc_resolved_ids", [])
        recipient_ids = resolved_ids or custom_ids

        if not recipient_ids:
            await edit_or_reply_html(update, "⚠️ No recipients found. Broadcast cancelled.")
            context.user_data.clear()
            return ConversationHandler.END

        total = len(recipient_ids)
        broadcast_id = db.create_broadcast(
            sent_by=uid,
            target_group=target,
            message_text=text,
            total_targets=total,
            custom_ids=",".join(str(i) for i in custom_ids) if custom_ids else None,
            media_file_id=media_file_id,
            media_type=media_type,
        )

        progress_msg = await edit_or_reply_html(
            update,
            f"📡 <b>Sending…</b>\n<code>0 / {total}</code>",
        )
        # progress_msg may be None if edit succeeded without return
        # So we reply a fresh one to have a message to edit
        prog_message = await update.effective_message.reply_text(
            f"📡 Sending…  0 / {total}"
        )

        # Build progress callback
        async def on_progress(sent: int, failed: int, total_: int):
            try:
                await prog_message.edit_text(
                    f"📡 Sending…  {sent + failed} / {total_}  "
                    f"(✅ {sent}  ❌ {failed})"
                )
            except Exception:
                pass

        # Use mother bot token for sending
        from telegram import Bot as TGBot
        mother_bot = TGBot(token=settings.mother_bot_token)
        try:
            result = await broadcast_message(
                mother_bot,
                recipient_ids,
                text,
                media_file_id=media_file_id,
                media_type=media_type,
                progress_callback=on_progress,
            )
        finally:
            try:
                await mother_bot.shutdown()
            except Exception:
                pass

        db.finish_broadcast(broadcast_id, result.sent, result.failed)
        logger.info(
            "Broadcast done: id=%s sent=%s failed=%s total=%s",
            broadcast_id, result.sent, result.failed, result.total,
        )

        summary = (
            f"✅ <b>Broadcast Complete</b>\n\n"
            f"Sent: <code>{result.sent}</code>\n"
            f"Failed: <code>{result.failed}</code>\n"
            f"Total: <code>{result.total}</code>"
        )
        try:
            await prog_message.edit_text(summary, parse_mode=ParseMode.HTML)
        except Exception:
            await update.effective_message.reply_text(summary, parse_mode=ParseMode.HTML)

        context.user_data.clear()
        return ConversationHandler.END

    # ─── Broadcast history ────────────────────────────────────────────────────

    async def broadcast_history(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
        if not await guard(update):
            return
        uid = _user_id(update)
        logger.info("Broadcast history: user_id=%s", uid)
        broadcasts = db.list_broadcasts(1000)
        visible_broadcasts, page, total_pages = page_slice(broadcasts, page)
        text = format_broadcast_history(visible_broadcasts)
        if broadcasts:
            text += f"\n\n<i>Page {page + 1}/{total_pages} · {len(broadcasts)} total</i>"
        await edit_or_reply_html(
            update,
            text,
            reply_markup=paged_keyboard("page:history", page, total_pages),
        )

    # ─── Cancel ───────────────────────────────────────────────────────────────

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if query:
            await query.answer()
        uid = _user_id(update)
        context.user_data.clear()
        await edit_or_reply_html(
            update,
            "❌ Cancelled.",
            reply_markup=home_keyboard(uid),
        )
        return ConversationHandler.END

    # ─── Reply keyboard button dispatcher ────────────────────────────────────

    REPLY_KEYBOARD_BUTTONS = {
        "📊 Dashboard",
        "📢 Broadcast",
        "🤖 Bots",
        "📋 Logs",
        "📈 Stats",
        "👥 Users",
        "💳 Credits",
        "⚙️ Admins",
        "📜 History",
    }
    reply_button_pattern = (
        "^(" + "|".join(re.escape(b) for b in sorted(REPLY_KEYBOARD_BUTTONS, key=len, reverse=True)) + ")$"
    )

    async def reply_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        if text == "📊 Dashboard":
            await dashboard(update, context)
        elif text == "📢 Broadcast":
            await broadcast_start(update, context)
        elif text == "🤖 Bots":
            await bots_list(update, context)
        elif text == "📋 Logs":
            await logs_command(update, context)
        elif text == "📈 Stats":
            await full_stats(update, context)
        elif text == "👥 Users":
            await users_list(update, context)
        elif text == "💳 Credits":
            await credits_command(update, context)
        elif text == "⚙️ Admins":
            await admins_panel(update, context)
        elif text == "📜 History":
            await broadcast_history(update, context)

    # ─── Inline callback router ───────────────────────────────────────────────

    async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        uid = _user_id(update)
        data = query.data
        logger.info("Callback: user_id=%s data=%s", uid, data)

        if not _is_authorized(uid):
            await edit_or_reply_html(update, "🔒 Access denied.")
            return

        if data == "nav:home":
            await edit_or_reply_html(
                update,
                "<b>🏠 Management Home</b>",
                reply_markup=home_keyboard(uid),
            )
            return
        if data == "nav:dashboard":
            await dashboard(update, context)
            return
        if data == "nav:stats":
            await full_stats(update, context)
            return
        if data == "nav:bots":
            await bots_list(update, context)
            return
        if data == "nav:users":
            await users_list(update, context)
            return
        if data == "nav:credits":
            await credits_command(update, context)
            return
        if data == "nav:admins":
            await admins_panel(update, context)
            return
        if data == "nav:history":
            await broadcast_history(update, context)
            return
        if data.startswith("page:"):
            parts = data.split(":")
            if len(parts) >= 3 and parts[1] == "bots":
                action = parts[2]
                page = int(parts[3]) if len(parts) > 3 else 0
                if _is_admin_role(uid):
                    bots = db.list_all_bots()
                else:
                    bots = db.list_bots_for_owner(uid)
                visible_bots, page, total_pages = page_slice(bots, page)
                text = format_bot_list(visible_bots)
                if bots:
                    text += f"\n\n<i>Page {page + 1}/{total_pages} · {len(bots)} total</i>"
                await edit_or_reply_html(
                    update,
                    text,
                    reply_markup=bots_picker_keyboard(bots, action=action, page=page),
                )
                return
            if len(parts) >= 3 and parts[1] == "users":
                await users_list(update, context, page=int(parts[2]))
                return
            if len(parts) >= 3 and parts[1] == "credits":
                await credits_command(update, context, page=int(parts[2]))
                return
            if len(parts) >= 3 and parts[1] == "history":
                await broadcast_history(update, context, page=int(parts[2]))
                return
        if data == "noop":
            return
        if data == "nav:cancel":
            context.user_data.clear()
            await edit_or_reply_html(
                update,
                "❌ Cancelled.",
                reply_markup=home_keyboard(uid),
            )
            return
        if data == "pick:logs":
            if _is_admin_role(uid):
                bots = db.list_all_bots()
            else:
                bots = db.list_bots_for_owner(uid)
            await edit_or_reply_html(
                update,
                "<b>📋 Choose a bot to view logs:</b>",
                reply_markup=bots_picker_keyboard(bots, action="botlogs"),
            )
            return

        # Per-bot actions
        if data.startswith("botstatus:"):
            bot_id = int(data.split(":", 1)[1])
            await show_bot_status(update, bot_id)
            return
        if data.startswith("botlogs:"):
            bot_id = int(data.split(":", 1)[1])
            await show_bot_logs(update, bot_id)
            return
        if data.startswith("botbroadcast:"):
            bot_id = int(data.split(":", 1)[1])
            b = db.get_bot(bot_id)
            if b is None:
                await edit_or_reply_html(update, "Bot not found.")
                return
            # Pre-fill target with chats that used this bot
            context.user_data.clear()
            context.user_data["bc_sender"] = uid
            context.user_data["bc_target"] = TARGET_CUSTOM
            # Resolve: all chats that had this bot
            with db.session() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT chat_id FROM bots WHERE id = ?", (bot_id,)
                ).fetchall()
            ids = [int(r["chat_id"]) for r in rows]
            context.user_data["bc_resolved_ids"] = ids
            await edit_or_reply_html(
                update,
                f"<b>📢 Broadcast for {escape(str(b['name']))}</b>\n\n"
                f"Recipients: <code>{len(ids)}</code> chat(s)\n\n"
                "Type or send your message:",
                reply_markup=cancel_keyboard(),
            )
            return

    # ─── Error handler ────────────────────────────────────────────────────────

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled error: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ An unexpected error occurred. Please try again."
                )
            except Exception:
                pass

    # ─── post_init / post_shutdown ─────────────────────────────────────────────

    async def post_init(application) -> None:
        commands = [
            BotCommand("start", "Home menu"),
            BotCommand("dashboard", "System dashboard"),
            BotCommand("bots", "List child bots"),
            BotCommand("search", "Search management data"),
            BotCommand("logs", "View bot logs"),
            BotCommand("stats", "Platform statistics"),
            BotCommand("users", "List users"),
            BotCommand("credits", "Credit dashboard"),
            BotCommand("usercredits", "Show user credit detail"),
            BotCommand("grantcredits", "Grant credits"),
            BotCommand("setcredits", "Set credits owner-only"),
            BotCommand("broadcast", "Send a broadcast"),
            BotCommand("history", "Broadcast history"),
            BotCommand("admins", "Manage admins"),
            BotCommand("addadmin", "Add an admin (owner/admin only)"),
            BotCommand("addb2b", "Add a B2B user"),
            BotCommand("removeadmin", "Remove admin/B2B access"),
            BotCommand("cancel", "Cancel current flow"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("Management bot commands registered")

    # ─── Broadcast ConversationHandler ───────────────────────────────────────

    broadcast_conv = ConversationHandler(
        entry_points=[
            CommandHandler("broadcast", broadcast_start),
            CallbackQueryHandler(broadcast_start, pattern=r"^nav:broadcast$"),
        ],
        states={
            BROADCAST_TARGET: [
                CallbackQueryHandler(broadcast_target_chosen, pattern=r"^bctarget:"),
            ],
            BROADCAST_CUSTOM_IDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_custom_ids),
            ],
            BROADCAST_COMPOSE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
                    broadcast_compose,
                ),
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(broadcast_confirm, pattern=r"^bc:confirm$"),
                CallbackQueryHandler(cancel, pattern=r"^nav:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^nav:cancel$"),
        ],
        per_message=False,
    )

    admin_add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_add_start,
                pattern=r"^admin:(add_prompt|addb2b_prompt)$",
            ),
        ],
        states={
            ADMIN_ADD_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_receive),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^nav:cancel$"),
        ],
        per_message=False,
    )

    # ─── Wire application ─────────────────────────────────────────────────────

    application = (
        ApplicationBuilder()
        .token(settings.mgmt_bot_token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("stats", full_stats))
    application.add_handler(CommandHandler("bots", bots_list))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("users", users_list))
    application.add_handler(CommandHandler("credits", credits_command))
    application.add_handler(CommandHandler("usercredits", usercredits_command))
    application.add_handler(CommandHandler("grantcredits", grantcredits_command))
    application.add_handler(CommandHandler("setcredits", setcredits_command))
    application.add_handler(CommandHandler("admins", admins_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("addb2b", addb2b_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("history", broadcast_history))
    application.add_handler(broadcast_conv)
    application.add_handler(admin_add_conv)
    application.add_handler(
        MessageHandler(filters.Regex(reply_button_pattern), reply_button_handler)
    )
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_error_handler(error_handler)

    return application
