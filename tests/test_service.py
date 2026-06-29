import asyncio
from dataclasses import replace
import tempfile
import time
import unittest
from pathlib import Path

from botmother.ai import AIDecision, AIEnvVar, AIReadinessDecision
from botmother.config import Settings
from botmother.db import Database
from botmother.service import BotService

OLD_BOT_CODE = """from typing import Any

async def error_handler(update, context):
    pass

async def setup(application):
    await application.bot.set_my_commands([])

def main():
    application: Any = object()
    application.add_error_handler(error_handler)

VALUE = 'old'
"""

NEW_BOT_CODE = """from typing import Any

async def error_handler(update, context):
    pass

async def setup(application):
    await application.bot.set_my_commands([])

def main():
    application: Any = object()
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
        self.edit_code_prompt = None
        self.answer_history = None
        self.force_code = None
        self.bot_context = None
        self.bot_question = None
        self.coding_brief_calls = []
        self.new_bot_calls = 0
        self.new_bot_prompt = None
        self.user_context = None
        self.readiness_calls = 0

    def decide_new_bot(
        self,
        prompt: str,
        answer_history,
        force_code: bool = False,
        user_context: str = "",
    ):
        self.new_bot_calls += 1
        self.new_bot_prompt = prompt
        self.user_context = user_context
        self.answer_history = answer_history
        self.force_code = force_code
        return AIDecision("code", "Ready.", (), self.edited_code, self.env)

    def decide_edit(
        self,
        current_code: str,
        edit_prompt: str,
        answer_history,
        force_code: bool = False,
        user_context: str = "",
    ):
        self.current_code = current_code
        self.edit_prompt = edit_prompt
        self.user_context = user_context
        self.answer_history = answer_history
        self.force_code = force_code
        return AIDecision("code", "Ready.", (), self.edited_code, self.env)

    def answer_bot_question(self, bot_context: str, question: str) -> str:
        self.bot_context = bot_context
        self.bot_question = question
        return self.answer

    def check_new_bot_readiness(
        self, prompt: str, answer_history, decision, user_context: str = ""
    ):
        self.readiness_calls += 1
        self.user_context = user_context
        return self.readiness

    def generate_code(self, prompt: str, user_context: str = ""):
        self.new_bot_prompt = prompt
        self.user_context = user_context
        return self.edited_code

    def build_coding_brief(self, prompt: str, user_context: str = ""):
        self.coding_brief_calls.append({"prompt": prompt, "user_context": user_context})
        return f"Full English implementation prompt:\n{prompt}"

    def edit_code(self, current_code: str, edit_prompt: str, user_context: str = ""):
        self.current_code = current_code
        self.edit_code_prompt = edit_prompt
        self.user_context = user_context
        return self.edited_code


class SlowCodeGenerator(FakeGenerator):
    def generate_code(self, prompt: str, user_context: str = ""):
        time.sleep(0.2)
        return super().generate_code(prompt, user_context=user_context)


def make_settings(tmp: str) -> Settings:
    return Settings(
        mother_bot_token="11111:mother_token_abcdefghijklmnopqrstuvwxyz",
        openrouter_api_key="test",
        openrouter_model="",
        openrouter_interaction_model="google/gemini-2.5-pro",
        openrouter_coding_model="deepseek/deepseek-v4-pro",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_app_name="BotMother tests",
        openrouter_app_url="",
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
    def test_create_bot_code_generation_times_out_without_saving_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(tmp),
                openrouter_coding_timeout_seconds=0.05,
            )
            db = Database(settings.db_path)
            db.initialize()
            db.upsert_user(1, "owner", None, None)
            runner = FakeRunner()
            service = BotService(settings, db, SlowCodeGenerator(NEW_BOT_CODE), runner)
            decision = AIDecision(
                "code",
                "Ready.",
                (),
                "Full English implementation prompt",
                (),
            )

            result = asyncio.run(
                service.create_bot_from_decision(
                    1,
                    100,
                    "make slow bot",
                    "12345:abcdefghijklmnopqrstuvwxyzABCDE",
                    decision,
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("timed out", result.message)
            self.assertEqual(db.list_bots(), [])
            self.assertEqual(runner.start_count, 0)

    def test_plan_new_bot_delegates_without_forced_localization_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)

            decision = service.plan_new_bot(
                "simple todo bot",
                [],
                force_code=False,
                user_context="Telegram user ID: 1",
            )

            self.assertEqual(decision.type, "code")
            self.assertEqual(generator.new_bot_calls, 1)
            self.assertEqual(generator.user_context, "Telegram user ID: 1")

    def test_plan_new_bot_still_passes_language_requests_to_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, generator, _ = make_service(tmp)

            decision = service.plan_new_bot(
                "simple todo bot in English and Burmese",
                [],
                force_code=False,
            )

            self.assertEqual(decision.type, "code")
            self.assertEqual(generator.new_bot_calls, 1)

    def test_check_new_bot_readiness_runs_after_followups(self):
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
            self.assertEqual(generator.readiness_calls, 1)

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

    def test_get_source_returns_latest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, _, bot_id = make_service(tmp)

            result = service.get_source(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.code, OLD_BOT_CODE)

    def test_bot_dashboard_includes_validation_and_runtime_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, _, bot_id = make_service(tmp)
            runner.active[bot_id] = object()
            db.add_log(bot_id, "stderr", "boom", 50)

            result = service.bot_dashboard(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertIn("Process", result.message)
            self.assertIn("active", result.message)
            self.assertIn("Validation", result.message)
            self.assertIn("PASS: Syntax", result.message)
            self.assertIn("boom", result.message)

    def test_validation_report_returns_layered_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, _, bot_id = make_service(tmp)

            result = service.validation_report(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertIn("Validation report", result.message)
            self.assertIn("PASS: Syntax", result.message)

    def test_auto_fix_uses_logs_validation_and_existing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, generator, bot_id = make_service(tmp)
            runner.active[bot_id] = object()
            db.add_log(bot_id, "stderr", "NameError: missing_start", 50)

            result = asyncio.run(service.auto_fix_bot(1, bot_id, "Telegram user ID: 1"))

            self.assertTrue(result.ok, result.message)
            self.assertIn("NameError: missing_start", generator.edit_prompt)
            self.assertIn("Current validation report", generator.edit_prompt)
            self.assertIn("Auto Fix", db.latest_revision(bot_id)["prompt"])
            self.assertEqual(generator.current_code, OLD_BOT_CODE)

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

    def test_non_owner_paid_action_reserves_and_settles(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, _, _ = make_service(tmp)
            db.upsert_user(2, "user", None, None)

            gate = service.reserve_paid_action(2, "new_bot")

            self.assertTrue(gate.ok, gate.message)
            self.assertEqual(service.credit_balance(2), 40)
            service.settle_paid_action(gate.reservation_id)
            self.assertEqual(service.credit_balance(2), 40)

    def test_non_owner_paid_action_blocks_when_balance_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, _, _, _ = make_service(tmp)
            db.upsert_user(2, "user", None, None)
            db.set_credit_balance(2, 2, None, service.settings.credits_initial_free)

            gate = service.reserve_paid_action(2, "new_bot")

            self.assertFalse(gate.ok)
            self.assertEqual(gate.balance, 2)
            self.assertIn("Not enough credits", gate.message)

    def test_owner_bypasses_credit_charge(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, _, _ = make_service(tmp)

            gate = service.reserve_paid_action(1, "new_bot")

            self.assertTrue(gate.ok)
            self.assertTrue(gate.exempt)
            self.assertIsNone(gate.reservation_id)

    def test_runtime_billing_stops_bots_when_balance_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, _, _ = make_service(tmp)
            db.upsert_user(2, "user", None, None)
            bot_id = db.create_bot(
                2,
                200,
                "Paid bot",
                "make paid",
                "99999:abcdefghijklmnopqrstuvwxyzABCDE",
                service.settings.workdir / "paid",
            )
            db.add_revision(bot_id, "make paid", OLD_BOT_CODE, "ok", None)
            db.mark_started(bot_id, 123)
            runner.active[bot_id] = object()
            db.set_credit_balance(2, 0, None, service.settings.credits_initial_free)
            db.accrue_runtime_credits(
                2, 1, 1000, service.settings.credit_runtime_seconds_per_credit, 50
            )

            import unittest.mock

            with unittest.mock.patch("time.time", return_value=1000 + service.settings.credit_runtime_seconds_per_credit):
                stopped = asyncio.run(service.bill_runtime_once())

            self.assertEqual(stopped, [bot_id])
            self.assertEqual(runner.stop_count, 1)


if __name__ == "__main__":
    unittest.main()
