from __future__ import annotations

import time
from typing import Any


def _count(rows: list[Any]) -> int:
    return len(rows)


def _since(epoch: int) -> int:
    """Return a unix timestamp N seconds ago."""
    return int(time.time()) - epoch


DAY = 86400
WEEK = 7 * DAY
MONTH = 30 * DAY


def gather_stats(db: "MgmtDatabase") -> dict:  # type: ignore[name-defined]
    """
    Aggregate stats from the shared BotMother DB.
    Returns a flat dict ready to format into a dashboard card.
    """
    bots = db.list_all_bots(include_deleted=False)
    all_bots_incl_deleted = db.list_all_bots(include_deleted=True)
    users = db.list_all_users()

    now = int(time.time())

    # Bot status breakdown
    status_counts: dict[str, int] = {}
    for b in bots:
        s = str(b["status"])
        status_counts[s] = status_counts.get(s, 0) + 1

    running = status_counts.get("running", 0)
    stopped = status_counts.get("stopped", 0)
    crashed = status_counts.get("crashed", 0)
    interrupted = status_counts.get("interrupted", 0)
    other_status = len(bots) - running - stopped - crashed - interrupted

    # Growth counters
    bots_today = sum(1 for b in bots if now - int(b["created_at"]) < DAY)
    bots_week = sum(1 for b in bots if now - int(b["created_at"]) < WEEK)
    bots_month = sum(1 for b in bots if now - int(b["created_at"]) < MONTH)

    users_today = sum(1 for u in users if now - int(u["first_seen_at"]) < DAY)
    users_week = sum(1 for u in users if now - int(u["first_seen_at"]) < WEEK)
    users_month = sum(1 for u in users if now - int(u["first_seen_at"]) < MONTH)

    active_today = sum(1 for u in users if now - int(u["last_seen_at"]) < DAY)
    active_week = sum(1 for u in users if now - int(u["last_seen_at"]) < WEEK)

    total_deleted = sum(1 for b in all_bots_incl_deleted if b["deleted_at"] is not None)

    return {
        "total_users": len(users),
        "users_today": users_today,
        "users_week": users_week,
        "users_month": users_month,
        "active_today": active_today,
        "active_week": active_week,
        "total_bots": len(bots),
        "total_deleted": total_deleted,
        "bots_today": bots_today,
        "bots_week": bots_week,
        "bots_month": bots_month,
        "running": running,
        "stopped": stopped,
        "crashed": crashed,
        "interrupted": interrupted,
        "other_status": other_status,
        "status_counts": status_counts,
    }


def format_stats_card(stats: dict) -> str:
    """Format the stats dict into an HTML Telegram message."""
    running = stats["running"]
    stopped = stats["stopped"]
    crashed = stats["crashed"]
    interrupted = stats["interrupted"]
    total = stats["total_bots"]

    # Health bar (emoji blocks up to 10 wide)
    health_parts = []
    if total > 0:
        r_blocks = round(running / total * 10)
        c_blocks = round(crashed / total * 10)
        s_blocks = 10 - r_blocks - c_blocks
        health_parts.append("🟢" * r_blocks + "🔴" * c_blocks + "⚫" * s_blocks)

    health_bar = " ".join(health_parts) or "—"

    lines = [
        "<b>📊 BotMother Dashboard</b>",
        "",
        "<b>👥 Users</b>",
        f"  Total: <code>{stats['total_users']}</code>",
        f"  New today: <code>{stats['users_today']}</code>  •  This week: <code>{stats['users_week']}</code>  •  Month: <code>{stats['users_month']}</code>",
        f"  Active today: <code>{stats['active_today']}</code>  •  This week: <code>{stats['active_week']}</code>",
        "",
        "<b>🤖 Child Bots</b>",
        f"  Total: <code>{stats['total_bots']}</code>  •  Deleted: <code>{stats['total_deleted']}</code>",
        f"  New today: <code>{stats['bots_today']}</code>  •  This week: <code>{stats['bots_week']}</code>  •  Month: <code>{stats['bots_month']}</code>",
        "",
        "<b>⚙️ Status Breakdown</b>",
        f"  🟢 Running: <code>{running}</code>",
        f"  ⚫ Stopped: <code>{stopped}</code>",
        f"  🔴 Crashed: <code>{crashed}</code>",
        f"  🟠 Interrupted: <code>{interrupted}</code>",
        "",
        f"<b>Health</b>  {health_bar}",
    ]
    return "\n".join(lines)
