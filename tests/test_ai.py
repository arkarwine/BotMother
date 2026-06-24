import unittest

from botmother.ai import AIResponseError, GeminiCodeGenerator, parse_ai_decision


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


if __name__ == "__main__":
    unittest.main()
