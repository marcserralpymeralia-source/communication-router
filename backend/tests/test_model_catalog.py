from __future__ import annotations

import unittest

from app.agent.model_catalog import completion_token_parameter, openai_model_capabilities, supports_custom_temperature


class OpenAIModelCapabilitiesTests(unittest.TestCase):
    def test_luna_uses_modern_completion_limit_without_custom_temperature(self):
        capabilities = openai_model_capabilities("gpt-5.6-luna")
        self.assertEqual(capabilities.completion_token_parameter, "max_completion_tokens")
        self.assertFalse(capabilities.supports_custom_temperature)
        self.assertEqual(completion_token_parameter("gpt-5.6-luna"), "max_completion_tokens")
        self.assertFalse(supports_custom_temperature("gpt-5.6-luna"))

    def test_legacy_model_keeps_sampling_and_token_compatibility(self):
        capabilities = openai_model_capabilities("gpt-4.1")
        self.assertEqual(capabilities.completion_token_parameter, "max_tokens")
        self.assertTrue(capabilities.supports_custom_temperature)

    def test_capabilities_are_case_and_whitespace_insensitive(self):
        self.assertFalse(supports_custom_temperature(" GPT-5.6-LUNA "))


if __name__ == "__main__":
    unittest.main()
