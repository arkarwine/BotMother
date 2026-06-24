from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


MAX_FOLLOWUP_ROUNDS = 5


SYSTEM_PROMPT = """You generate Telegram bot source code.

Return only raw Python code. Do not use Markdown fences. Do not return JSON. Do not describe the code.

The code must be a complete standalone Python file that can run with:
python bot.py

Runtime contract:
- Read the Telegram token from os.environ["BOT_TOKEN"].
- Read the bot-specific SQLite path from os.environ["BOT_DB_PATH"].
- You may use Python standard library, sqlite3, and python-telegram-bot.
- Use polling, not webhooks.
- Prefer python-telegram-bot async ApplicationBuilder.
- Create needed SQLite tables yourself inside BOT_DB_PATH.
- Keep the bot simple, robust, and friendly.

Do not import subprocess, socket, ctypes, importlib, or multiprocessing.
Do not call eval, exec, compile, __import__, os.system, os.remove, os.unlink, os.rmdir, os.rename, os.replace, shutil.move, or shutil.rmtree.
"""


JSON_SYSTEM_PROMPT = f"""{SYSTEM_PROMPT}

Before generating code, decide whether you understand the user's requested bot well enough.

Return exactly one JSON object matching this schema:
{{
  "type": "questions" | "code",
  "message": "short user-facing message",
  "questions": [
    {{
      "id": "lower_snake_case_id",
      "question": "one clear question",
      "suggestions": ["optional short suggested answer", "optional short suggested answer"]
    }}
  ],
  "code": "complete Python source when type is code, otherwise null",
  "env": [
    {{"name": "UPPER_SNAKE_ENV_NAME", "value": "user provided value"}}
  ]
}}

Rules:
- Return JSON only. No Markdown. No prose outside JSON.
- Use type "questions" when important requirements, behavior, storage, commands, admin policy, schedules, external services, or env vars are unclear.
- Ask 1 to 3 questions at a time.
- Ask only questions that materially change the implementation.
- Include practical suggestions when helpful.
- Use type "code" only when ready to generate a complete standalone bot.py.
- Include env entries only for values the user explicitly provided in this conversation. Do not invent secrets.
- If an external API key or config value is needed and not provided, ask for it.
- When forced to generate, use reasonable defaults and do not ask more questions.
"""


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
QUESTION_KEYS = {"id", "question", "suggestions"}
ENV_KEYS = {"name", "value"}
TOP_LEVEL_KEYS = {"type", "message", "questions", "code", "env"}
RESERVED_ENV_NAMES = {"BOT_TOKEN", "BOT_DB_PATH", "PATH", "PYTHONUNBUFFERED", "PYTHONIOENCODING"}


class AIResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class AIQuestion:
    id: str
    question: str
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIEnvVar:
    name: str
    value: str


@dataclass(frozen=True)
class AIDecision:
    type: str
    message: str
    questions: tuple[AIQuestion, ...] = ()
    code: str | None = None
    env: tuple[AIEnvVar, ...] = ()

    @property
    def needs_questions(self) -> bool:
        return self.type == "questions"


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = FENCE_RE.fullmatch(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _expect_str(value: Any, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AIResponseError(f"AI JSON field '{field}' must be a string.")
    if not allow_empty and not value.strip():
        raise AIResponseError(f"AI JSON field '{field}' must not be empty.")
    return value.strip() if not allow_empty else value


def parse_ai_decision(text: str) -> AIDecision:
    try:
        data = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"AI returned invalid JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise AIResponseError("AI JSON must be an object.")
    extra = set(data) - TOP_LEVEL_KEYS
    if extra:
        raise AIResponseError(f"AI JSON has unexpected fields: {', '.join(sorted(extra))}")

    decision_type = _expect_str(data.get("type"), "type")
    if decision_type not in {"questions", "code"}:
        raise AIResponseError("AI JSON field 'type' must be 'questions' or 'code'.")

    message = _expect_str(data.get("message", ""), "message", allow_empty=True).strip()
    raw_questions = data.get("questions", [])
    raw_env = data.get("env", [])
    code = data.get("code")

    if not isinstance(raw_questions, list):
        raise AIResponseError("AI JSON field 'questions' must be a list.")
    if not isinstance(raw_env, list):
        raise AIResponseError("AI JSON field 'env' must be a list.")

    questions: list[AIQuestion] = []
    for index, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            raise AIResponseError(f"Question #{index + 1} must be an object.")
        extra_question = set(item) - QUESTION_KEYS
        if extra_question:
            raise AIResponseError(f"Question #{index + 1} has unexpected fields: {', '.join(sorted(extra_question))}")
        suggestions = item.get("suggestions", [])
        if not isinstance(suggestions, list) or not all(isinstance(s, str) for s in suggestions):
            raise AIResponseError(f"Question #{index + 1} suggestions must be a list of strings.")
        questions.append(
            AIQuestion(
                id=_expect_str(item.get("id"), f"questions[{index}].id"),
                question=_expect_str(item.get("question"), f"questions[{index}].question"),
                suggestions=tuple(s.strip() for s in suggestions if s.strip()),
            )
        )

    env: list[AIEnvVar] = []
    for index, item in enumerate(raw_env):
        if not isinstance(item, dict):
            raise AIResponseError(f"Env var #{index + 1} must be an object.")
        extra_env = set(item) - ENV_KEYS
        if extra_env:
            raise AIResponseError(f"Env var #{index + 1} has unexpected fields: {', '.join(sorted(extra_env))}")
        name = _expect_str(item.get("name"), f"env[{index}].name")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", name):
            raise AIResponseError(f"Env var #{index + 1} name must be UPPER_SNAKE_CASE.")
        if name in RESERVED_ENV_NAMES:
            raise AIResponseError(f"Env var #{index + 1} uses reserved runtime name '{name}'.")
        env.append(AIEnvVar(name=name, value=_expect_str(item.get("value"), f"env[{index}].value", allow_empty=True)))

    if decision_type == "questions":
        if not questions:
            raise AIResponseError("AI JSON type 'questions' requires at least one question.")
        if len(questions) > 3:
            raise AIResponseError("AI JSON may ask at most 3 questions at a time.")
        if code not in {None, ""}:
            raise AIResponseError("AI JSON type 'questions' must not include code.")
        if env:
            raise AIResponseError("AI JSON type 'questions' must not include env values.")
        return AIDecision("questions", message, tuple(questions), None, ())

    if not isinstance(code, str) or not code.strip():
        raise AIResponseError("AI JSON type 'code' requires non-empty code.")
    if questions:
        raise AIResponseError("AI JSON type 'code' must not include questions.")
    return AIDecision("code", message, (), code, tuple(env))


def format_answer_history(answer_history: list[dict[str, Any]]) -> str:
    if not answer_history:
        return "No follow-up questions have been answered yet."
    parts = []
    for index, item in enumerate(answer_history, start=1):
        questions = item.get("questions", [])
        answer = item.get("answer", "")
        parts.append(f"Round {index} questions:\n" + "\n".join(f"- {q}" for q in questions))
        parts.append(f"User answer:\n{answer}")
    return "\n\n".join(parts)


@dataclass
class GeminiCodeGenerator:
    api_key: str
    model: str

    def __post_init__(self) -> None:
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("google-genai is not installed. Run: pip install -r requirements.txt") from exc
        self._client = genai.Client(api_key=self.api_key)

    def _generate_json_decision(self, prompt: str) -> AIDecision:
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "system_instruction": JSON_SYSTEM_PROMPT,
                "response_mime_type": "application/json",
            },
        )
        text = getattr(response, "text", None)
        if not text:
            logger.error("Gemini returned an empty JSON decision")
            raise RuntimeError("Gemini returned an empty JSON decision.")
        decision = parse_ai_decision(text)
        logger.info("Gemini JSON decision: type=%s questions=%s code_chars=%s env=%s", decision.type, len(decision.questions), len(decision.code or ""), len(decision.env))
        return decision

    def decide_new_bot(self, user_prompt: str, answer_history: list[dict[str, Any]], force_code: bool = False) -> AIDecision:
        logger.info(
            "Planning new bot: model=%s prompt_chars=%s answer_rounds=%s force_code=%s",
            self.model,
            len(user_prompt),
            len(answer_history),
            force_code,
        )
        prompt = (
            "The user wants to create a new Telegram bot.\n\n"
            f"Original request:\n{user_prompt.strip()}\n\n"
            f"Follow-up history:\n{format_answer_history(answer_history)}\n\n"
            f"Force code now: {'yes' if force_code else 'no'}"
        )
        return self._generate_json_decision(prompt)

    def decide_edit(
        self,
        current_code: str,
        edit_prompt: str,
        answer_history: list[dict[str, Any]],
        force_code: bool = False,
    ) -> AIDecision:
        logger.info(
            "Planning edit: model=%s code_chars=%s prompt_chars=%s answer_rounds=%s force_code=%s",
            self.model,
            len(current_code),
            len(edit_prompt),
            len(answer_history),
            force_code,
        )
        prompt = (
            "The user wants to edit an existing Telegram bot.\n\n"
            "Current source code:\n"
            "```python\n"
            f"{current_code.strip()}\n"
            "```\n\n"
            f"User edit request:\n{edit_prompt.strip()}\n\n"
            f"Follow-up history:\n{format_answer_history(answer_history)}\n\n"
            f"Force code now: {'yes' if force_code else 'no'}"
        )
        return self._generate_json_decision(prompt)

    def generate_code(self, user_prompt: str) -> str:
        logger.info("Generating child bot code: model=%s prompt_chars=%s", self.model, len(user_prompt))
        prompt = (
            "Build this Telegram bot from the user's request. "
            "Return only the complete Python source file.\n\n"
            f"User request:\n{user_prompt.strip()}"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": SYSTEM_PROMPT},
        )
        text = getattr(response, "text", None)
        if text:
            logger.info("Gemini returned generated code: chars=%s", len(text))
            return text
        logger.error("Gemini returned an empty response")
        raise RuntimeError("Gemini returned an empty response.")

    def edit_code(self, current_code: str, edit_prompt: str) -> str:
        logger.info(
            "Editing child bot code: model=%s code_chars=%s prompt_chars=%s",
            self.model,
            len(current_code),
            len(edit_prompt),
        )
        prompt = (
            "Modify the existing Telegram bot source code according to the user's request. "
            "Return only the complete updated Python source file.\n\n"
            "Current source code:\n"
            "```python\n"
            f"{current_code.strip()}\n"
            "```\n\n"
            f"User edit request:\n{edit_prompt.strip()}"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": SYSTEM_PROMPT},
        )
        text = getattr(response, "text", None)
        if text:
            logger.info("Gemini returned edited code: chars=%s", len(text))
            return text
        logger.error("Gemini returned an empty edit response")
        raise RuntimeError("Gemini returned an empty edit response.")
