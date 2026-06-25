import unittest

from botmother.ai import AIDecision, AIResponseError, GeminiCodeGenerator, parse_ai_decision, parse_readiness_decision


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if not self.texts:
            raise AssertionError("No fake Gemini responses left.")
        return FakeResponse(self.texts.pop(0))


class FakeClient:
    def __init__(self, texts):
        self.models = FakeModels(texts)


def make_generator(texts):
    generator = GeminiCodeGenerator.__new__(GeminiCodeGenerator)
    generator.api_key = "test"
    generator.model = "test-model"
    generator._client = FakeClient(texts)
    return generator


class AIDecisionTests(unittest.TestCase):
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
        repair_prompt = generator._client.models.calls[1]["contents"]
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
        self.assertEqual(len(generator._client.models.calls), 2)
        repair_prompt = generator._client.models.calls[1]["contents"]
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
        self.assertEqual(len(generator._client.models.calls), 3)

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

    def test_check_new_bot_readiness_uses_strict_json(self):
        generator = make_generator(
            [
                """
                {
                  "type": "ready",
                  "message": "Ready.",
                  "questions": []
                }
                """
            ]
        )
        code_decision = AIDecision("code", "Ready.", (), "print('ok')", ())

        decision = generator.check_new_bot_readiness("make an echo bot", [], code_decision)

        self.assertEqual(decision.type, "ready")
        call = generator._client.models.calls[0]
        self.assertEqual(call["config"]["response_mime_type"], "application/json")
        self.assertIn("Final readiness check", call["contents"])
        self.assertIn("Do not ask for the Telegram token", call["contents"])

    def test_refine_code_for_deploy_returns_raw_python(self):
        generator = make_generator(["print('refined')"])

        code = generator.refine_code_for_deploy(
            "make an echo bot",
            "print('original')",
            ["WEATHER_API_KEY"],
            2,
            3,
            "old error",
        )

        self.assertEqual(code, "print('refined')")
        call = generator._client.models.calls[0]
        self.assertIn("Layer 2/3", call["contents"])
        self.assertIn("WEATHER_API_KEY", call["contents"])
        self.assertIn("old error", call["contents"])


if __name__ == "__main__":
    unittest.main()
