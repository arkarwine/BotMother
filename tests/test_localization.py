import unittest

from botmother.localization import normalize_locale, t


class LocalizationTests(unittest.TestCase):
    def test_normalize_myanmar_locale(self):
        self.assertEqual(normalize_locale("my"), "my")
        self.assertEqual(normalize_locale("my-MM"), "my")
        self.assertEqual(normalize_locale("burmese"), "my")

    def test_myanmar_translation_loads(self):
        self.assertIn("ရှာမတွေ့", t("bot_not_found", locale="my"))
        self.assertIn("Bot အသစ်", t("button.new_bot", locale="my"))

    def test_unknown_locale_falls_back_to_english(self):
        self.assertIn("Bot not found", t("bot_not_found", locale="fr"))


if __name__ == "__main__":
    unittest.main()
