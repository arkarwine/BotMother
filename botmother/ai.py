from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


MAX_FOLLOWUP_ROUNDS = 5
MAX_JSON_REPAIR_ATTEMPTS = 2
AI_REFINEMENT_LAYERS = 3
RUNTIME_PROVIDED_ENV = {
    "BOT_TOKEN": "the child Telegram bot token from BotFather; BotMother injects this at launch",
    "BOT_DB_PATH": "the child bot's SQLite database path; BotMother injects this at launch",
    "PATH": "runtime executable search path",
    "PYTHONUNBUFFERED": "runtime logging behavior",
    "PYTHONIOENCODING": "runtime text encoding",
}
RESERVED_ENV_NAMES = set(RUNTIME_PROVIDED_ENV)
RUNTIME_ENV_CONTRACT = "\n".join(f"- {name}: {description}" for name, description in RUNTIME_PROVIDED_ENV.items())


SYSTEM_PROMPT = f"""You generate Telegram bot source code.

Return only raw Python code. Do not use Markdown fences. Do not return JSON. Do not describe the code.

The code must be a complete standalone Python file that can run with:
python bot.py

Runtime contract:
- Read the Telegram token from os.environ["BOT_TOKEN"].
- Read the bot-specific SQLite path from os.environ["BOT_DB_PATH"].
- BotMother already provides these runtime environment variables:
{RUNTIME_ENV_CONTRACT}
- You may use Python standard library, sqlite3, and python-telegram-bot.
- Use polling, not webhooks.
- Prefer python-telegram-bot async ApplicationBuilder.
- Prefer Telegram-native UX over command-heavy text flows.
- Use ReplyKeyboardMarkup for persistent main menus and common user actions.
- Use InlineKeyboardMarkup for choices, confirmations, item selection, pagination, admin actions, and next-step navigation.
- Avoid asking users to type IDs, option names, or command syntax when a button can represent the choice.
- Keep slash commands as fallback entry points, but make primary workflows tappable with buttons and short prompts.
- Create needed SQLite tables yourself inside BOT_DB_PATH.
- Keep the bot simple, robust, and friendly.
- Every child bot must register a global error handler with application.add_error_handler.
- The global error handler must log exceptions and send a friendly fallback message when possible.
- Telegram formatting must work reliably. Prefer ParseMode.HTML with html.escape for dynamic values.
- If using MarkdownV2, escape every dynamic/user-provided value with telegram.helpers.escape_markdown(value, version=2).
- Do not use legacy Markdown parse mode or unescaped user content in Markdown/HTML.

Do not import subprocess, socket, ctypes, importlib, or multiprocessing.
Do not call eval, exec, compile, __import__, os.system, os.remove, os.unlink, os.rmdir, os.rename, os.replace, shutil.move, or shutil.rmtree.
"""


JSON_SYSTEM_PROMPT = f"""{SYSTEM_PROMPT}

Before generating code, decide whether you understand the user's requested bot well enough.

Return exactly one JSON object matching this schema:
{{
  "type": "questions" | "code",
  "message": "complete user-facing message BotMother can send verbatim",
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
- For type "questions", put the full natural-language follow-up message in "message" in the user's language/style.
- Make user-facing messages fluid, concise, and helpful. Avoid robotic labels like "Suggestions:" unless the user explicitly wants that format.
- BotMother will show only "message" to the user. It will not separately print question numbers, labels, suggestions, or follow-up counters.
- Keep "questions" as internal structured state that mirrors the actual questions asked in "message".
- Include practical suggestions naturally inside "message" when helpful. Do not use labels like "Suggestions:".
- Do not mention JSON, schema fields, internal limits, or follow-up counts to the user.
- Use type "code" only when ready to generate a complete standalone bot.py.
- Include env entries only for values the user explicitly provided in this conversation. Do not invent secrets.
- If an external API key or config value is needed and not provided, ask for it.
- Never include runtime-provided env names in env. They are already injected by BotMother: {", ".join(sorted(RESERVED_ENV_NAMES))}.
- The generated code may read os.environ["BOT_TOKEN"] and os.environ["BOT_DB_PATH"], but the JSON env array must not set them.
- When forced to generate, use reasonable defaults and do not ask more questions.
"""


ASK_SYSTEM_PROMPT = """You answer questions from a Telegram bot owner about one generated child bot.

Use the provided bot context: saved prompt, status, latest source, configured env var names, and recent logs.
Answer in the same language/style as the user's question when possible.

Rules:
- Be concise and practical.
- Be fluid and specific. Use short paragraphs and direct next steps.
- Do not expose raw source code unless the user explicitly asks for a tiny snippet.
- Do not reveal tokens, env var values, or secrets.
- If the answer needs more evidence than the context contains, say what is unknown.
- If the user wants to change behavior, suggest tapping Edit Bot and describing the requested change.
- Do not claim the bot can do something unless the context supports it.
"""


READINESS_SYSTEM_PROMPT = f"""You are BotMother's final requirements checker before a generated child Telegram bot asks for its BotFather token.

Return exactly one JSON object matching this schema:
{{
  "type": "ready" | "questions",
  "message": "complete user-facing message BotMother can send verbatim",
  "questions": [
    {{
      "id": "lower_snake_case_id",
      "question": "one clear question",
      "suggestions": ["optional short suggested answer", "optional short suggested answer"]
    }}
  ]
}}

Rules:
- Return JSON only. No Markdown. No prose outside JSON.
- Check only whether essential data is missing for the generated bot to run usefully as requested.
- Essential means the bot would be unusable, unable to authenticate to a required external service, unable to identify required admins/operators, unable to display required payment/contact details, or unable to perform a core requested workflow.
- Do not ask optional preference, UI polish, feature expansion, tone, copywriting, or "nice to have" questions.
- Do not ask for the Telegram/BotFather token. BotMother collects it after this check and injects it as BOT_TOKEN.
- Do not ask for BOT_DB_PATH, PATH, PYTHONUNBUFFERED, or PYTHONIOENCODING. BotMother provides runtime env vars:
{RUNTIME_ENV_CONTRACT}
- Use type "ready" when no essential runtime data is missing.
- Use type "questions" only when one or more essential values are missing.
- Ask 1 to 3 questions at a time.
- For type "questions", put the full natural-language follow-up message in "message" in the user's language/style.
- Make the message feel like a helpful product assistant, not a form.
- BotMother will show only "message" to the user. It will not separately print question numbers, labels, suggestions, or follow-up counters.
- Keep "questions" as internal structured state that mirrors the actual questions asked in "message".
"""


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
QUESTION_KEYS = {"id", "question", "suggestions"}
ENV_KEYS = {"name", "value"}
TOP_LEVEL_KEYS = {"type", "message", "questions", "code", "env"}
READINESS_KEYS = {"type", "message", "questions"}


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


@dataclass(frozen=True)
class AIReadinessDecision:
    type: str
    message: str
    questions: tuple[AIQuestion, ...] = ()

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


def _parse_questions(data: dict[str, Any]) -> tuple[AIQuestion, ...]:
    raw_questions = data.get("questions", [])
    if not isinstance(raw_questions, list):
        raise AIResponseError("AI JSON field 'questions' must be a list.")

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
    return tuple(questions)


def parse_readiness_decision(text: str) -> AIReadinessDecision:
    try:
        data = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"AI returned invalid readiness JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise AIResponseError("AI readiness JSON must be an object.")
    extra = set(data) - READINESS_KEYS
    if extra:
        raise AIResponseError(f"AI readiness JSON has unexpected fields: {', '.join(sorted(extra))}")

    decision_type = _expect_str(data.get("type"), "type")
    if decision_type not in {"ready", "questions"}:
        raise AIResponseError("AI readiness JSON field 'type' must be 'ready' or 'questions'.")
    message = _expect_str(data.get("message", ""), "message", allow_empty=True).strip()
    questions = _parse_questions(data)

    if decision_type == "ready":
        if questions:
            raise AIResponseError("AI readiness JSON type 'ready' must not include questions.")
        return AIReadinessDecision("ready", message, ())

    if not questions:
        raise AIResponseError("AI readiness JSON type 'questions' requires at least one question.")
    if len(questions) > 3:
        raise AIResponseError("AI readiness JSON may ask at most 3 questions at a time.")
    return AIReadinessDecision("questions", message, questions)


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

    def _generate_json_text(self, prompt: str) -> str:
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
            raise AIResponseError("Gemini returned an empty JSON decision.")
        return text

    def _generate_readiness_text(self, prompt: str) -> str:
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "system_instruction": READINESS_SYSTEM_PROMPT,
                "response_mime_type": "application/json",
            },
        )
        text = getattr(response, "text", None)
        if not text:
            logger.error("Gemini returned an empty readiness decision")
            raise AIResponseError("Gemini returned an empty readiness decision.")
        return text

    def _build_json_repair_prompt(self, original_prompt: str, invalid_text: str, error: str, attempt: int) -> str:
        return (
            "Your previous response did not match BotMother's required JSON schema.\n"
            f"Repair attempt {attempt}/{MAX_JSON_REPAIR_ATTEMPTS}.\n\n"
            f"Validation error:\n{error}\n\n"
            "Important runtime environment contract:\n"
            f"{RUNTIME_ENV_CONTRACT}\n\n"
            "Do not include any of those runtime-provided names in the JSON env array. "
            "If your code needs the child bot token, read os.environ[\"BOT_TOKEN\"] in the Python code instead.\n\n"
            "Original task context:\n"
            f"{original_prompt}\n\n"
            "Your invalid response was:\n"
            f"{invalid_text.strip()[:6000]}\n\n"
            "Return one corrected JSON object only. No Markdown and no prose outside JSON."
        )

    def _fallback_questions_after_bad_json(self, error: str) -> AIDecision:
        logger.error("Gemini JSON decision remained invalid after repair attempts: %s", error)
        return AIDecision(
            "questions",
            "I had trouble getting a valid AI plan. Please restate the request with any needed settings.",
            (
                AIQuestion(
                    "clarify_request",
                    "What should this bot do, and are there any extra API keys or settings besides the Telegram token?",
                    ("No extra settings", "I will provide API keys in this chat"),
                ),
            ),
            None,
            (),
        )

    def _fallback_readiness_questions_after_bad_json(self, error: str) -> AIReadinessDecision:
        logger.error("Gemini readiness decision remained invalid after repair attempts: %s", error)
        return AIReadinessDecision(
            "questions",
            "I need one more essential detail before this bot can be launched. Please describe any required admin IDs, API keys, payment/contact details, or external service settings.",
            (
                AIQuestion(
                    "missing_runtime_data",
                    "What essential admin IDs, API keys, payment/contact details, or external service settings does this bot need to run?",
                    ("No extra settings are needed", "I will provide the missing values here"),
                ),
            ),
        )

    def _generate_json_decision(self, prompt: str) -> AIDecision:
        current_prompt = prompt
        last_error = "Unknown AI response error."
        for attempt in range(MAX_JSON_REPAIR_ATTEMPTS + 1):
            text = ""
            try:
                text = self._generate_json_text(current_prompt)
                decision = parse_ai_decision(text)
            except AIResponseError as exc:
                last_error = str(exc)
                if attempt >= MAX_JSON_REPAIR_ATTEMPTS:
                    return self._fallback_questions_after_bad_json(last_error)
                logger.warning(
                    "Gemini JSON decision invalid; requesting repair: attempt=%s error=%s",
                    attempt + 1,
                    exc,
                )
                current_prompt = self._build_json_repair_prompt(prompt, text, last_error, attempt + 1)
                continue
            logger.info(
                "Gemini JSON decision: type=%s questions=%s code_chars=%s env=%s",
                decision.type,
                len(decision.questions),
                len(decision.code or ""),
                len(decision.env),
            )
            return decision
        return self._fallback_questions_after_bad_json(last_error)

    def _build_readiness_repair_prompt(self, original_prompt: str, invalid_text: str, error: str, attempt: int) -> str:
        return (
            "Your previous readiness response did not match BotMother's required JSON schema.\n"
            f"Repair attempt {attempt}/{MAX_JSON_REPAIR_ATTEMPTS}.\n\n"
            f"Validation error:\n{error}\n\n"
            "Remember: return type \"ready\" only when no essential runtime data is missing, "
            "or type \"questions\" only for missing data required to run the bot. "
            "Do not ask for the Telegram token.\n\n"
            "Original readiness context:\n"
            f"{original_prompt}\n\n"
            "Your invalid response was:\n"
            f"{invalid_text.strip()[:6000]}\n\n"
            "Return one corrected JSON object only. No Markdown and no prose outside JSON."
        )

    def _generate_readiness_decision(self, prompt: str) -> AIReadinessDecision:
        current_prompt = prompt
        last_error = "Unknown AI readiness response error."
        for attempt in range(MAX_JSON_REPAIR_ATTEMPTS + 1):
            text = ""
            try:
                text = self._generate_readiness_text(current_prompt)
                decision = parse_readiness_decision(text)
            except AIResponseError as exc:
                last_error = str(exc)
                if attempt >= MAX_JSON_REPAIR_ATTEMPTS:
                    return self._fallback_readiness_questions_after_bad_json(last_error)
                logger.warning(
                    "Gemini readiness decision invalid; requesting repair: attempt=%s error=%s",
                    attempt + 1,
                    exc,
                )
                current_prompt = self._build_readiness_repair_prompt(prompt, text, last_error, attempt + 1)
                continue
            logger.info(
                "Gemini readiness decision: type=%s questions=%s",
                decision.type,
                len(decision.questions),
            )
            return decision
        return self._fallback_readiness_questions_after_bad_json(last_error)

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
            "BotMother will collect the child BotFather token after this planning step and inject it as BOT_TOKEN at runtime. "
            "Do not ask for the Telegram token and do not include BOT_TOKEN in env.\n\n"
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
            "This child bot already has a Telegram token stored by BotMother, and BotMother injects it as BOT_TOKEN at runtime. "
            "Do not ask for the Telegram token and do not include BOT_TOKEN in env.\n\n"
            "Current source code:\n"
            "```python\n"
            f"{current_code.strip()}\n"
            "```\n\n"
            f"User edit request:\n{edit_prompt.strip()}\n\n"
            f"Follow-up history:\n{format_answer_history(answer_history)}\n\n"
            f"Force code now: {'yes' if force_code else 'no'}"
        )
        return self._generate_json_decision(prompt)

    def check_new_bot_readiness(
        self,
        user_prompt: str,
        answer_history: list[dict[str, Any]],
        decision: AIDecision,
    ) -> AIReadinessDecision:
        logger.info(
            "Checking new bot readiness: model=%s prompt_chars=%s answer_rounds=%s code_chars=%s env=%s",
            self.model,
            len(user_prompt),
            len(answer_history),
            len(decision.code or ""),
            len(decision.env),
        )
        env_names = ", ".join(item.name for item in decision.env) if decision.env else "none"
        prompt = (
            "Final readiness check before asking the user for the child BotFather token.\n\n"
            "BotMother will collect the child BotFather token next and inject it as BOT_TOKEN. "
            "Do not ask for the Telegram token.\n\n"
            f"Original request:\n{user_prompt.strip()}\n\n"
            f"Follow-up history:\n{format_answer_history(answer_history)}\n\n"
            f"Generated code message:\n{decision.message.strip()}\n\n"
            f"Provided child env var names and values from prior user answers: {env_names}\n\n"
            "Generated source:\n"
            "```python\n"
            f"{(decision.code or '').strip()}\n"
            "```"
        )
        return self._generate_readiness_decision(prompt)

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

    def refine_code_for_deploy(
        self,
        user_prompt: str,
        current_code: str,
        env_names: list[str],
        layer: int,
        total_layers: int,
        validation_error: str | None = None,
    ) -> str:
        logger.info(
            "Refining child bot code: model=%s layer=%s/%s code_chars=%s validation_error=%s",
            self.model,
            layer,
            total_layers,
            len(current_code),
            validation_error or "-",
        )
        focus = {
            1: "requirements coverage and missing edge cases without adding new requirements",
            2: "runtime reliability, async handler correctness, SQLite safety, and graceful error handling",
            3: "final deployment polish, clear user messages, startup robustness, and no forbidden APIs",
        }.get(layer, "deployment readiness and correctness")
        prompt = (
            "Refine this generated Telegram child bot before deployment.\n\n"
            "Return only the complete standalone Python source file. No Markdown, JSON, explanation, or diff.\n\n"
            f"Layer {layer}/{total_layers} focus: {focus}.\n"
            "Keep the same requested behavior. Do not add optional features or ask questions.\n"
            "Keep the BotMother runtime contract: read BOT_TOKEN and BOT_DB_PATH from os.environ.\n"
            "Do not hardcode tokens or secrets. Do not require env vars except the provided names and BotMother runtime vars.\n"
            "Prefer Telegram-native buttons: ReplyKeyboardMarkup for main menus and InlineKeyboardMarkup for choices, confirmations, lists, pagination, and admin actions.\n"
            "Avoid asking users to type IDs, option names, or command syntax when a button can represent the choice.\n"
            "Keep slash commands as fallback, but make primary workflows tappable with short prompts.\n"
            "Ensure there is a global application.add_error_handler that logs exceptions and sends a friendly fallback message.\n"
            "Ensure Telegram formatting works safely: prefer ParseMode.HTML with html.escape for dynamic values, or escape MarkdownV2 dynamic values with escape_markdown.\n"
            "Keep using only Python standard library, sqlite3, and python-telegram-bot.\n"
            "Do not import subprocess, socket, ctypes, importlib, or multiprocessing.\n"
            "Do not call eval, exec, compile, __import__, os.system, os.remove, os.unlink, os.rmdir, os.rename, os.replace, shutil.move, or shutil.rmtree.\n\n"
            f"Provided child env var names: {', '.join(env_names) if env_names else 'none'}\n"
            f"Validation issue from previous layer: {validation_error or 'none'}\n\n"
            f"Original user request:\n{user_prompt.strip()}\n\n"
            "Current source:\n"
            "```python\n"
            f"{current_code.strip()}\n"
            "```"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": SYSTEM_PROMPT},
        )
        text = getattr(response, "text", None)
        if text and text.strip():
            logger.info("Gemini returned refined code: layer=%s chars=%s", layer, len(text))
            return text
        logger.error("Gemini returned an empty refinement response: layer=%s", layer)
        raise RuntimeError("Gemini returned an empty refinement response.")

    def answer_bot_question(self, bot_context: str, question: str) -> str:
        logger.info(
            "Answering bot question: model=%s context_chars=%s question_chars=%s",
            self.model,
            len(bot_context),
            len(question),
        )
        prompt = (
            "Bot context:\n"
            f"{bot_context.strip()}\n\n"
            "User question:\n"
            f"{question.strip()}"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": ASK_SYSTEM_PROMPT},
        )
        text = getattr(response, "text", None)
        if text and text.strip():
            logger.info("Gemini returned bot answer: chars=%s", len(text))
            return text.strip()
        logger.error("Gemini returned an empty bot answer")
        raise RuntimeError("Gemini returned an empty answer.")
