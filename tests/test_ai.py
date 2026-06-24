import unittest

from botmother.ai import AIResponseError, parse_ai_decision


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


if __name__ == "__main__":
    unittest.main()
