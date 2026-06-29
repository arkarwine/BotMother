import json
import unittest
from unittest.mock import patch

from botmother.ai import (
    HYBRID_RESPONSE_DELIMITER,
    AIDecision,
    AIResponseError,
    OpenRouterCodeGenerator,
    parse_ai_decision,
    parse_readiness_decision,
)


class FakeGenerator(OpenRouterCodeGenerator):
    def __init__(self, texts):
        self.api_key = "test"
        self.model = "test-model"
        self.interaction_model = "interaction-model"
        self.coding_model = "coding-model"
        self.base_url = "https://openrouter.ai/api/v1"
        self.app_name = "BotMother tests"
        self.app_url = ""
        self.texts = list(texts)
        self.calls = []

    def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
        model: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "json_mode": json_mode,
                "model": model,
            }
        )
        if not self.texts:
            raise AssertionError("No fake OpenRouter responses left.")
        return self.texts.pop(0)


def make_generator(texts):
    return FakeGenerator(texts)


class FakeHTTPResponse:
    body = b'{"choices":[{"message":{"content":"hello"}}]}'

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class FakeHTTPResponseWithBody(FakeHTTPResponse):
    def __init__(self, body: bytes):
        self.body = body


class FakeStreamingHTTPResponse(FakeHTTPResponse):
    def __init__(self, lines):
        self.lines = [
            line if isinstance(line, bytes) else line.encode("utf-8") for line in lines
        ]

    def __iter__(self):
        return iter(self.lines)


class AIDecisionTests(unittest.TestCase):
    def test_openrouter_chat_posts_openai_compatible_payload(self):
        generator = OpenRouterCodeGenerator(
            api_key="sk-test",
            model="fallback-model",
            interaction_model="google/gemini-2.5-pro",
            coding_model="deepseek/deepseek-v4-pro",
            app_name="BotMother tests",
            app_url="https://example.test",
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse()) as mocked:
            text = generator._chat("system", "user", json_mode=True)

        self.assertEqual(text, "hello")
        request = mocked.call_args.args[0]
        body = request.data.decode("utf-8")
        payload = json.loads(body)
        self.assertEqual(request.headers["Authorization"], "Bearer sk-test")
        self.assertEqual(request.headers["X-title"], "BotMother tests")
        self.assertEqual(request.headers["Http-referer"], "https://example.test")
        self.assertEqual(request.headers["X-openrouter-metadata"], "enabled")
        self.assertEqual(payload["model"], "fallback-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_completion_tokens"], 6000)
        self.assertEqual(payload["reasoning"], {"exclude": True, "effort": "minimal"})
        self.assertNotIn("provider", payload)

    def test_openrouter_chat_uses_coding_budget_for_coding_model(self):
        generator = OpenRouterCodeGenerator(
            api_key="sk-test",
            model="fallback-model",
            interaction_model="interaction-model",
            coding_model="coding-model",
            interaction_max_tokens=1111,
            coding_max_tokens=2222,
            interaction_reasoning_effort="minimal",
            coding_reasoning_effort="low",
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse()) as mocked:
            generator._chat("system", "user", model="coding-model")

        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "coding-model:nitro")
        self.assertEqual(payload["max_completion_tokens"], 2222)
        self.assertEqual(payload["reasoning"], {"exclude": True, "effort": "low"})
        self.assertEqual(
            payload["provider"],
            {
                "order": ["Novita", "Fireworks", "SiliconFlow"],
                "allow_fallbacks": False,
            },
        )

    def test_openrouter_chat_allows_custom_coding_provider_order(self):
        generator = OpenRouterCodeGenerator(
            api_key="sk-test",
            model="fallback-model",
            interaction_model="interaction-model",
            coding_model="coding-model",
            coding_provider_only=("Fireworks", "Novita"),
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse()) as mocked:
            generator._chat("system", "user", model="coding-model")

        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "coding-model:nitro")
        self.assertEqual(payload["provider"]["order"], ["Fireworks", "Novita"])

    def test_openrouter_chat_replaces_existing_coding_model_variant_with_nitro(self):
        generator = OpenRouterCodeGenerator(
            api_key="sk-test",
            model="fallback-model",
            interaction_model="interaction-model",
            coding_model="coding-model:floor",
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse()) as mocked:
            generator._chat("system", "user", model="coding-model:floor")

        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "coding-model:nitro")

    def test_openrouter_chat_normalizes_list_content(self):
        generator = OpenRouterCodeGenerator(api_key="sk-test", model="test-model")
        body = b'{"choices":[{"message":{"content":[{"type":"text","text":"hello"},{"type":"text","text":"world"}]}}]}'

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponseWithBody(body)):
            text = generator._chat("system", "user")

        self.assertEqual(text, "hello\nworld")

    def test_generate_code_result_exposes_openrouter_usage(self):
        generator = OpenRouterCodeGenerator(api_key="sk-test", model="test-model")
        body = (
            b'{"id":"abc","model":"test-model","usage":{"prompt_tokens":11,'
            b'"completion_tokens":22,"total_tokens":33},"choices":[{"finish_reason":"stop",'
            b'"message":{"content":"print(1)"}}]}'
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponseWithBody(body)):
            result = generator.generate_code_result("make an echo bot")

        self.assertEqual(result.text, "print(1)")
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage.prompt_tokens, 11)
        self.assertEqual(result.usage.completion_tokens, 22)
        self.assertEqual(result.usage.total_tokens, 33)

    def test_generate_code_rejects_token_limited_partial_output(self):
        generator = OpenRouterCodeGenerator(api_key="sk-test", model="test-model")
        body = (
            b'{"usage":{"prompt_tokens":11,"completion_tokens":24000,"total_tokens":24011},'
            b'"choices":[{"finish_reason":"length","message":{"content":"def main("}}]}'
        )

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponseWithBody(body)):
            with self.assertRaises(AIResponseError) as caught:
                generator.generate_code_result("make a huge bot")

        self.assertIn("token limit", str(caught.exception))

    def test_streaming_answer_parses_sse_chunks_and_usage(self):
        generator = OpenRouterCodeGenerator(api_key="sk-test", model="test-model")
        lines = [
            'data: {"id":"abc","model":"test-model","choices":[{"delta":{"content":"Hel"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            'data: {"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        deltas = []

        with patch(
            "urllib.request.urlopen", return_value=FakeStreamingHTTPResponse(lines)
        ) as mocked:
            result = generator.answer_bot_question_streaming_result(
                "Bot context", "Question?", on_delta=deltas.append
            )

        self.assertEqual(result.text, "Hello")
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage.total_tokens, 5)
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_generate_code_result_streams_code_deltas(self):
        generator = OpenRouterCodeGenerator(
            api_key="sk-test",
            model="fallback-model",
            coding_model="coding-model",
        )
        lines = [
            'data: {"id":"abc","model":"coding-model:nitro","choices":[{"delta":{"content":"print"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"(1)"}}]}\n\n',
            'data: {"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        deltas = []

        with patch(
            "urllib.request.urlopen", return_value=FakeStreamingHTTPResponse(lines)
        ) as mocked:
            result = generator.generate_code_result(
                "make tiny bot", on_delta=deltas.append
            )

        self.assertEqual(result.text, "print(1)")
        self.assertEqual(deltas, ["print", "(1)"])
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "coding-model:nitro")
        self.assertTrue(payload["stream"])

    def test_json_decision_streams_only_visible_text_after_delimiter(self):
        generator = OpenRouterCodeGenerator(api_key="sk-test", model="test-model")
        json_text = json.dumps(
            {
                "type": "questions",
                "message": "",
                "questions": [
                    {
                        "id": "admin_id",
                        "question": "Which admin Telegram user ID should manage it?",
                        "suggestions": [],
                    }
                ],
                "code": None,
                "env": [],
            }
        )
        visible_text = "Need admin ID?\n\nWhich admin Telegram user ID should manage it?"
        text = json_text + "\n" + HYBRID_RESPONSE_DELIMITER + "\n" + visible_text
        split = text.index(HYBRID_RESPONSE_DELIMITER) + len(HYBRID_RESPONSE_DELIMITER) + 2
        lines = [
            "data: "
            + json.dumps({"choices": [{"delta": {"content": text[:split]}}]})
            + "\n\n",
            "data: "
            + json.dumps({"choices": [{"delta": {"content": text[split:]}}]})
            + "\n\n",
            'data: {"usage":{"prompt_tokens":8,"completion_tokens":6,"total_tokens":14},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        deltas = []

        with patch(
            "urllib.request.urlopen", return_value=FakeStreamingHTTPResponse(lines)
        ) as mocked:
            decision = generator.decide_new_bot(
                "make admin bot", [], on_delta=deltas.append
            )

        self.assertTrue(decision.needs_questions)
        self.assertEqual("".join(deltas), visible_text)
        self.assertEqual(decision.message, visible_text)
        self.assertIsNotNone(decision.ai_usage)
        self.assertEqual(decision.ai_usage.total_tokens, 14)
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertNotIn("response_format", payload)

    def test_empty_json_response_falls_back_instead_of_crashing(self):
        generator = make_generator(["", "", ""])

        decision = generator._generate_json_decision("Original newbot prompt")

        self.assertTrue(decision.needs_questions)
        self.assertEqual(decision.questions[0].id, "clarify_request")

    def test_model_routing_uses_interaction_and_coding_models(self):
        generator = make_generator(
            [
                "Full English implementation prompt. Target UI language: Myanmar/Burmese. Admin user id: 1.",
                """
                {
                  "type": "ready",
                  "message": "Ready.",
                  "questions": []
                }
                """,
                "print('code')",
            ]
        )
        code_decision = AIDecision("code", "Ready.", (), "print('ok')", ())

        implementation_prompt = generator.build_coding_brief("make bot", "Telegram user ID: 1")
        generator.check_new_bot_readiness("make bot", [], code_decision)
        generator.generate_code(implementation_prompt)

        self.assertEqual(generator.calls[0]["model"], "interaction-model")
        self.assertIn("Telegram user ID: 1", generator.calls[0]["user_prompt"])
        self.assertEqual(generator.calls[1]["model"], "interaction-model")
        self.assertEqual(generator.calls[2]["model"], "coding-model")
        self.assertNotIn("Requester context", generator.calls[2]["user_prompt"])
        self.assertIn("Target UI language", generator.calls[2]["user_prompt"])
        self.assertIn("Admin user id: 1", generator.calls[2]["user_prompt"])

    def test_parse_questions_decision(self):
        decision = parse_ai_decision(
            """
            {
              "type": "questions",
              "message": "I need details.",
              "questions": [
                {
                  "id": "weather_source",
                  "question": "Which weather provider should I use?",
                  "suggestions": ["OpenWeather", "No external API"]
                }
              ],
              "code": null,
              "env": []
            }
            """
        )

        self.assertTrue(decision.needs_questions)
        self.assertEqual(decision.questions[0].id, "weather_source")
        self.assertEqual(decision.questions[0].suggestions, ("OpenWeather", "No external API"))

    def test_parse_questions_decision_allows_more_than_three_questions(self):
        questions = [
            {"id": f"q_{index}", "question": f"Question {index}?", "suggestions": []}
            for index in range(5)
        ]
        payload = {
            "type": "questions",
            "message": "I need a few details.",
            "questions": questions,
            "code": None,
            "env": [],
        }

        decision = parse_ai_decision(json.dumps(payload))

        self.assertTrue(decision.needs_questions)
        self.assertEqual(len(decision.questions), 5)

    def test_parse_code_decision_with_env(self):
        decision = parse_ai_decision(
            """
            {
              "type": "code",
              "message": "Ready.",
              "questions": [],
              "code": "import os\\nprint(os.environ['WEATHER_API_KEY'])",
              "env": [{"name": "WEATHER_API_KEY", "value": "secret"}]
            }
            """
        )

        self.assertFalse(decision.needs_questions)
        self.assertIn("WEATHER_API_KEY", decision.code)
        self.assertEqual(decision.env[0].name, "WEATHER_API_KEY")
        self.assertEqual(decision.env[0].value, "secret")

    def test_reject_extra_top_level_key(self):
        with self.assertRaises(AIResponseError):
            parse_ai_decision(
                """
                {
                  "type": "code",
                  "message": "Ready.",
                  "questions": [],
                  "code": "print('ok')",
                  "env": [],
                  "extra": true
                }
                """
            )

    def test_reject_questions_with_code(self):
        with self.assertRaises(AIResponseError):
            parse_ai_decision(
                """
                {
                  "type": "questions",
                  "message": "I need details.",
                  "questions": [{"id": "x", "question": "What?", "suggestions": []}],
                  "code": "print('bad')",
                  "env": []
                }
                """
            )

    def test_reject_question_like_message_with_empty_questions(self):
        with self.assertRaises(AIResponseError) as caught:
            parse_ai_decision(
                """
                {
                  "type": "code",
                  "message": "I can help. Could you please clarify the following?",
                  "questions": [],
                  "code": "print('ok')",
                  "env": []
                }
                """
            )

        self.assertIn("questions array is empty", str(caught.exception))

    def test_reject_soft_detail_request_with_empty_questions(self):
        with self.assertRaises(AIResponseError) as caught:
            parse_ai_decision(
                """
                {
                  "type": "code",
                  "message": "I will build your e-commerce bot. To ensure it fits your needs perfectly, please provide a few more details:",
                  "questions": [],
                  "code": "print('ok')",
                  "env": []
                }
                """
            )

        self.assertIn("questions array is empty", str(caught.exception))

    def test_reject_readiness_detail_request_with_empty_questions(self):
        with self.assertRaises(AIResponseError) as caught:
            parse_readiness_decision(
                """
                {
                  "type": "ready",
                  "message": "Before launch, I need a few more details.",
                  "questions": []
                }
                """
            )

        self.assertIn("questions array is empty", str(caught.exception))

    def test_reject_choice_message_without_suggestions(self):
        with self.assertRaises(AIResponseError) as caught:
            parse_ai_decision(
                """
                {
                  "type": "questions",
                  "message": "Please choose one of the following product management methods.",
                  "questions": [{"id": "products", "question": "How should products be managed?", "suggestions": []}],
                  "code": null,
                  "env": []
                }
                """
            )

        self.assertIn("choose from options", str(caught.exception))

    def test_reject_burmese_choice_question_without_suggestions(self):
        with self.assertRaises(AIResponseError) as caught:
            parse_readiness_decision(
                """
                {
                  "type": "questions",
                  "message": "ကုန်ပစ္စည်းစာရင်းကို စနစ်တကျစီမံခန့်ခွဲနိုင်ဖို့အတွက် အောက်ပါနည်းလမ်းများထဲမှ တစ်ခုကို ရွေးချယ်ပေးပါ။",
                  "questions": [{"id": "product_management", "question": "ကုန်ပစ္စည်းစာရင်းကို ဘယ်လိုစီမံခန့်ခွဲချင်ပါသလဲ?", "suggestions": []}]
                }
                """
            )

        self.assertIn("choose from options", str(caught.exception))

    def test_json_generation_repairs_empty_question_message(self):
        bad = """
        {
          "type": "code",
          "message": "I can help. Could you please clarify the following?",
          "questions": [],
          "code": "print('ok')",
          "env": []
        }
        """
        fixed = """
        {
          "type": "questions",
          "message": "Who are the admins?",
          "questions": [{"id": "admin_ids", "question": "Who are the admins?", "suggestions": []}],
          "code": null,
          "env": []
        }
        """
        generator = make_generator([bad, fixed])

        decision = generator._generate_json_decision("Original newbot prompt")

        self.assertTrue(decision.needs_questions)
        self.assertEqual(decision.questions[0].id, "admin_ids")
        repair_prompt = generator.calls[1]["user_prompt"]
        self.assertIn("questions array is empty", repair_prompt)

    def test_reject_reserved_env_name(self):
        with self.assertRaises(AIResponseError):
            parse_ai_decision(
                """
                {
                  "type": "code",
                  "message": "Ready.",
                  "questions": [],
                  "code": "print('ok')",
                  "env": [{"name": "BOT_TOKEN", "value": "bad"}]
                }
                """
            )

    def test_json_generation_repairs_reserved_runtime_env(self):
        bad = """
        {
          "type": "code",
          "message": "Ready.",
          "questions": [],
          "code": "import os\\nprint(os.environ['BOT_TOKEN'])",
          "env": [{"name": "BOT_TOKEN", "value": "12345:bad"}]
        }
        """
        fixed = """
        {
          "type": "code",
          "message": "Ready.",
          "questions": [],
          "code": "import os\\nprint(os.environ['BOT_TOKEN'])",
          "env": []
        }
        """
        generator = make_generator([bad, fixed])

        decision = generator._generate_json_decision("Original newbot prompt")

        self.assertEqual(decision.type, "code")
        self.assertEqual(decision.env, ())
        self.assertEqual(len(generator.calls), 2)
        repair_prompt = generator.calls[1]["user_prompt"]
        self.assertIn("Env var #1 uses reserved runtime name 'BOT_TOKEN'", repair_prompt)
        self.assertIn("BOT_TOKEN", repair_prompt)
        self.assertIn('os.environ["BOT_TOKEN"]', repair_prompt)

    def test_json_generation_falls_back_after_repeated_bad_json(self):
        bad = """
        {
          "type": "code",
          "message": "Ready.",
          "questions": [],
          "code": "print('ok')",
          "env": [{"name": "BOT_TOKEN", "value": "bad"}]
        }
        """
        generator = make_generator([bad, bad, bad])

        decision = generator._generate_json_decision("Original newbot prompt")

        self.assertTrue(decision.needs_questions)
        self.assertEqual(decision.questions[0].id, "clarify_request")
        self.assertEqual(len(generator.calls), 3)

    def test_parse_readiness_ready(self):
        decision = parse_readiness_decision(
            """
            {
              "type": "ready",
              "message": "Ready.",
              "questions": []
            }
            """
        )

        self.assertFalse(decision.needs_questions)
        self.assertEqual(decision.message, "Ready.")

    def test_parse_readiness_questions(self):
        decision = parse_readiness_decision(
            """
            {
              "type": "questions",
              "message": "Admin ID လိုပါတယ်။",
              "questions": [{"id": "admin_id", "question": "Admin user ID?", "suggestions": []}]
            }
            """
        )

        self.assertTrue(decision.needs_questions)
        self.assertEqual(decision.questions[0].id, "admin_id")

    def test_parse_readiness_questions_allows_more_than_three_questions(self):
        questions = [
            {"id": f"q_{index}", "question": f"Question {index}?", "suggestions": []}
            for index in range(4)
        ]
        payload = {
            "type": "questions",
            "message": "Missing launch details.",
            "questions": questions,
        }

        decision = parse_readiness_decision(json.dumps(payload))

        self.assertTrue(decision.needs_questions)
        self.assertEqual(len(decision.questions), 4)

    def test_check_new_bot_readiness_uses_hybrid_prompt(self):
        generator = make_generator(
            [
                """
                {
                  "type": "ready",
                  "message": "",
                  "questions": []
                }
                <<<BOTMOTHER_RESPONSE_TEXT>>>
                Ready.
                """
            ]
        )
        code_decision = AIDecision("code", "Ready.", (), "print('ok')", ())

        decision = generator.check_new_bot_readiness("make an echo bot", [], code_decision)

        self.assertEqual(decision.type, "ready")
        self.assertEqual(decision.message, "Ready.")
        call = generator.calls[0]
        self.assertFalse(call["json_mode"])
        self.assertIn("Final readiness check", call["user_prompt"])
        self.assertIn("Do not ask for the Telegram token", call["user_prompt"])
        self.assertIn("English implementation prompt", call["user_prompt"])


if __name__ == "__main__":
    unittest.main()
