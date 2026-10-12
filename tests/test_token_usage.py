import unittest
from types import SimpleNamespace
from dataclasses import asdict
from book_translator.chat import ChatReply
from book_translator.token_usage import attach_usage


class TokenUsageTests(unittest.TestCase):
    def test_provider_counts_and_reasoning(self):
        cases = [
            ('gemini', SimpleNamespace(prompt_token_count=100, candidates_token_count=20,
                                      thoughts_token_count=10, total_token_count=130)),
            ('openai', dict(input_tokens=100, output_tokens=30, total_tokens=130)),
            ('nvidia', dict(prompt_tokens=100, completion_tokens=30, total_tokens=130)),
        ]
        for provider, raw in cases:
            reply = attach_usage(ChatReply('Answer', None), raw, provider)
            self.assertEqual((reply.token_usage.input, reply.token_usage.output, reply.token_usage.total), (100, 30, 130))
            self.assertNotIn('token_usage', asdict(reply))

    def test_missing_usage_is_not_estimated(self):
        for raw in (None, {}, dict(input_tokens=50), dict(total_tokens=-1), dict(total_tokens=True)):
            reply = attach_usage(ChatReply('Answer', None), raw, 'openai')
            self.assertFalse(hasattr(reply, 'token_usage'))

    def test_total_fallback_and_zero(self):
        for incoming, outgoing in ((5, 7), (0, 0)):
            reply = attach_usage(ChatReply('Answer', None), dict(prompt_tokens=incoming, completion_tokens=outgoing), 'nvidia')
            self.assertEqual(reply.token_usage.total, incoming + outgoing)
