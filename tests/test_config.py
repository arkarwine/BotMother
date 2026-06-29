import os
import unittest
from unittest.mock import patch

from botmother.config import Settings


class SettingsTests(unittest.TestCase):
    def test_coding_provider_pin_normalizes_provider_names(self):
        env = {
            "MOTHER_BOT_TOKEN": "11111:mother_token_abcdefghijklmnopqrstuvwxyz",
            "OPENROUTER_API_KEY": "sk-test",
            "OPENROUTER_CODING_PROVIDER_ONLY": "novita,fireworks,siliconflow",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(env_file="missing.env")

        self.assertEqual(
            settings.openrouter_coding_provider_only,
            ("Novita", "Fireworks", "SiliconFlow"),
        )

    def test_coding_provider_pin_defaults_to_nitro_provider_order(self):
        env = {
            "MOTHER_BOT_TOKEN": "11111:mother_token_abcdefghijklmnopqrstuvwxyz",
            "OPENROUTER_API_KEY": "sk-test",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(env_file="missing.env")

        self.assertEqual(
            settings.openrouter_coding_provider_only,
            ("Novita", "Fireworks", "SiliconFlow"),
        )


if __name__ == "__main__":
    unittest.main()
