"""Normalize reported usage; never estimate token counts from character lengths."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenUsage:
    input: int
    output: int
    total: int


def attach_usage(result, usage, provider):
    def count(name):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return value if type(value) is int and value >= 0 else None
    if provider == 'gemini':
        incoming, outgoing, total = count('prompt_token_count'), count('candidates_token_count'), count('total_token_count')
        thoughts = count('thoughts_token_count') or 0
        if outgoing is not None:
            outgoing += thoughts
    else:
        incoming = count('input_tokens') if provider == 'openai' else count('prompt_tokens')
        outgoing = count('output_tokens') if provider == 'openai' else count('completion_tokens')
        total = count('total_tokens')
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    if total is not None:
        object.__setattr__(result, 'token_usage', TokenUsage(incoming or 0, outgoing or 0, total))
    return result
