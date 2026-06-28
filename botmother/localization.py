from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "my"
SUPPORTED_LOCALES = {"en", "my"}
LOCALES_DIR = Path(__file__).with_name("locales")


def normalize_locale(locale: str | None) -> str:
    value = (locale or "").strip().lower().replace("-", "_")
    if value in {"my", "my_mm", "burmese", "myanmar"}:
        return "my"
    if value.startswith("my_"):
        return "my"
    if value in SUPPORTED_LOCALES:
        return value
    return DEFAULT_LOCALE


@lru_cache(maxsize=8)
def load_locale(locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    path = LOCALES_DIR / f"{locale}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if locale != DEFAULT_LOCALE:
            return load_locale(DEFAULT_LOCALE)
        logger.exception("Missing default locale file: %s", path)
        return {}
    except json.JSONDecodeError:
        logger.exception("Invalid locale JSON: %s", path)
        return load_locale(DEFAULT_LOCALE) if locale != DEFAULT_LOCALE else {}

    if not isinstance(data, dict):
        logger.error("Locale file must contain a JSON object: %s", path)
        return load_locale(DEFAULT_LOCALE) if locale != DEFAULT_LOCALE else {}
    return {str(key): str(value) for key, value in data.items()}


def t(key: str, locale: str = DEFAULT_LOCALE, **values: Any) -> str:
    locale = normalize_locale(locale)
    text = load_locale(locale).get(key)
    if text is None and locale != DEFAULT_LOCALE:
        text = load_locale(DEFAULT_LOCALE).get(key)
    if text is None:
        logger.warning("Missing localization key: %s", key)
        text = key
    return text.format(**values) if values else text
