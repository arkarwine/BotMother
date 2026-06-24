from __future__ import annotations

import re


TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,}$")


def is_valid_telegram_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token.strip()))


def mask_token(token: str) -> str:
    token = token.strip()
    if ":" not in token:
        return "***"
    bot_id, secret = token.split(":", 1)
    if len(secret) <= 8:
        return f"{bot_id}:***"
    return f"{bot_id}:{secret[:4]}...{secret[-4:]}"

