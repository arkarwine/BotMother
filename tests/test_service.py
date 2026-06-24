import asyncio
import tempfile
import unittest
from pathlib import Path

from botmother.ai import AIDecision, AIEnvVar, AIReadinessDecision
from botmother.config import Settings
from botmother.db import Database
from botmother.service import BotService

OLD_BOT_CODE = """async def error_handler(update, context):
    pass

async def setup(application):
    await application.bot.set_my_commands([])

def main():
    application = object()
    application.add_error_handler(error_handler)

VALUE = 'old'
"""

NEW_BOT_CODE = """async def error_handler(update, context):
    pass

async def setup(application):
    await application.bot.set_my_commands([])

def main():
    application = object()
    application.add_error_handler(error_handler)

VALUE = 'new'
"""


class FakeRunner:
    def __init__(self):
        self.active = {}
        self.start_count = 0
        self.stop_count = 0

    async def start_bot(self, bot_id: int) -> None:
        self.start_count += 1
        self.active[bot_id] = object()

    async def stop_bot(self, bot_id: int, mark_stopped: bool = True) -> None:
        self.stop_count += 1
        self.active.pop(bot_id, None)


class FakeGenerator:
    def __init__(self, edited_code: str, env=None, answer: str = "It echoes messages."):
        self.edited_code = edited_code
        self.env = tuple(env or ())
        self.answer = answer
        self.readiness = AIReadinessDecision("ready", "Ready.", ())
        self.current_code = None
        self.edit_prompt = None
        self.answer_history = None
        self.force_code = None
        self.bot_context = None
        self.bot_question = None
        self.refinement_calls = []
        self.new_bot_calls = 0
        self.new_bot_prompt = None
        self.readiness_calls = 0

    def decide_new_bot(self, prompt: str, answer_history, force_code: bool = False):
        self.new_bot_calls += 1
        self.new_bot_prompt = prompt
        self.answer_history = answer_history
        self.force_code = force_code
        return AIDecision("code", "Ready.", (), self.edited_code, self.env)

    def decide_edit(
        self,
        current_code: str,
        edit_prompt: str,
        answer_history,
        force_code: bool = False,
    ):
        self.current_code = current_code
        self.edit_prompt = edit_prompt
        self.answer_history = answer_history
        self.force_code = force_code
        return AIDecision("code", "Ready.", (), self.edited_code, self.env)

    def answer_bot_question(self, bot_context: str, question: str) -> str:
        self.bot_context = bot_context
        self.bot_question = question
        return self.answer

    def check_new_bot_readiness(self, prompt: str, answer_history, decision):
        self.readiness_calls += 1
        return self.readiness

    def refine_code_for_deploy(
        self,
        user_prompt: str,
        current_code: str,
        env_names,
        layer: int,
        total_layers: int,
        validation_error=None,
    ) -> str:
        self.refinement_calls.append(
            {
                "prompt": user_prompt,
                "code": current_code,
                "env_names": list(env_names),
                "layer": layer,
                "total_layers": total_layers,
                "validation_error": validation_error,
            }
        )
        return current_code


def make_settings(tmp: str) -> Settings:
    return Settings(
        mother_bot_token="11111:mother_token_abcdefghijklmnopqrstuvwxyz",
        gemini_api_key="test",
        gemini_model="gemini-3.1-flash-lite",
        db_path=Path(tmp) / "botmother.sqlite3",
        workdir=Path(tmp) / "bots",
        owner_ids={1},
        python_bin="/usr/bin/python3",
        bwrap_bin="bwrap",
        require_bwrap=True,
    )


def make_service(tmp: str, edited_code: str = NEW_BOT_CODE, env=None):
    settings = make_settings(tmp)
    db = Database(settings.db_path)
    db.initialize()
    db.upsert_user(1, "owner", None, None)
    bot_id = db.create_bot(
        1,
        100,
        "Echo",
        "make echo",
        "12345:abcdefghijklmnopqrstuvwxyzABCDE",
        settings.workdir / "1",
    )
    db.add_revision(bot_id, "make echo", OLD_BOT_CODE, "ok", None)
    runner = FakeRunner()
    generator = FakeGenerator(edited_code, env=env)
    return BotService(settings, db, generator, runner), db, runner, generator, bot_id


class ServiceEditTests(unittest.TestCase):
    def test_plan_new_bot_asks_for_localization_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)

            decision = service.plan_new_bot("simple todo bot", [], force_code=False)

            self.assertTrue(decision.needs_questions)
            self.assertEqual(decision.questions[0].id, "localization_languages")
            self.assertEqual(generator.new_bot_calls, 0)

    def test_plan_new_bot_skips_localization_question_when_languages_are_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)

            decision = service.plan_new_bot(
                "simple todo bot in English and Burmese",
                [],
                force_code=False,
            )

            self.assertEqual(decision.type, "code")
            self.assertEqual(generator.new_bot_calls, 1)

    def test_check_new_bot_readiness_skips_extra_ai_after_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)
            decision = AIDecision("code", "Ready.", (), NEW_BOT_CODE, ())

            readiness = service.check_new_bot_readiness(
                "simple todo bot",
                [
                    {
                        "questions": ["Which languages should it support?"],
                        "answer": "English only",
                    }
                ],
                decision,
            )

            self.assertEqual(readiness.type, "ready")
            self.assertEqual(generator.readiness_calls, 0)

    def test_check_new_bot_readiness_calls_ai_without_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)
            decision = AIDecision("code", "Ready.", (), NEW_BOT_CODE, ())

            readiness = service.check_new_bot_readiness("simple todo bot", [], decision)

            self.assertEqual(readiness.type, "ready")
            self.assertEqual(generator.readiness_calls, 1)

    def test_invalid_prompt_edit_does_not_stop_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, _, bot_id = make_service(
                tmp, edited_code="import subprocess\n"
            )
            runner.active[bot_id] = object()

            result = asyncio.run(
                service.edit_bot_with_prompt(1, bot_id, "add shell command support")
            )

            self.assertFalse(result.ok)
            self.assertEqual(runner.stop_count, 0)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["validation_status"], "failed")

    def test_valid_prompt_edit_restarts_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = [AIEnvVar("WEATHER_API_KEY", "secret")]
            service, db, runner, generator, bot_id = make_service(
                tmp, edited_code=NEW_BOT_CODE, env=env
            )
            runner.active[bot_id] = object()

            result = asyncio.run(
                service.edit_bot_with_prompt(1, bot_id, "make it friendlier")
            )

            self.assertTrue(result.ok, result.message)
            self.assertEqual(runner.stop_count, 1)
            self.assertEqual(runner.start_count, 1)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["code"], NEW_BOT_CODE.strip())
            self.assertEqual(db.get_bot_env_vars(bot_id), {"WEATHER_API_KEY": "secret"})
            self.assertEqual(generator.current_code, OLD_BOT_CODE)
            self.assertEqual(generator.edit_prompt, "make it friendlier")
            self.assertEqual(len(generator.refinement_calls), 0)

    def test_get_source_returns_latest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, _, bot_id = make_service(tmp)

            result = service.get_source(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.code, OLD_BOT_CODE)

    def test_ask_bot_uses_context_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, generator, bot_id = make_service(tmp)
            secret_token = "12345:abcdefghijklmnopqrstuvwxyzABCDE"
            db.set_bot_env_vars(bot_id, {"WEATHER_API_KEY": "secret-value"})
            db.add_log(
                bot_id, "stderr", f"failed with {secret_token} and secret-value", 50
            )

            result = service.ask_bot(1, bot_id, "what does it do?")

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.message, "It echoes messages.")
            self.assertEqual(generator.bot_question, "what does it do?")
            self.assertIn("Original prompt", generator.bot_context)
            self.assertIn("Latest source", generator.bot_context)
            self.assertIn("WEATHER_API_KEY", generator.bot_context)
            self.assertNotIn(secret_token, generator.bot_context)
            self.assertNotIn("secret-value", generator.bot_context)

    def test_ask_bot_denies_inaccessible_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, bot_id = make_service(tmp)

            result = service.ask_bot(2, bot_id, "what does it do?")

            self.assertFalse(result.ok)
            self.assertIn("access", result.message)
            self.assertIsNone(generator.bot_question)


if __name__ == "__main__":
    unittest.main()
