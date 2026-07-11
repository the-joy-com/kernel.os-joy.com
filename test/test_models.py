"""The model map and its local token counting.

These hold whether tiktoken loaded its encoding or fell back to the character estimate:
both count and truncate on the same measure, so the assertions are about the counter's contract
(positive, monotonic, and a truncation that actually caps), not about an exact token number.
"""

from services import models


def test_the_map_carries_provider_and_optimal_context():
    # The registry the budget guard reads: each model names its provider and the window it reads well.
    qwen = models.MODELS["qwen3.5:4b"]
    assert qwen.provider == "ollama"
    # The optimal is the effective window, deliberately well below qwen3.5:4b's ~262K advertised maximum.
    assert 0 < qwen.optimal_context_tokens < 262_144


def test_count_tokens_is_positive_and_grows_with_text():
    # A non-empty text has a positive count, and more text counts as more tokens.
    short = models.count_tokens("a short line")
    longer = models.count_tokens("a short line, and then a good deal more text after it than before")
    assert short > 0
    assert longer > short


def test_truncate_tokens_caps_and_leaves_short_text_whole():
    # Over the cap, the result measures within it; under the cap, the text is returned untouched.
    long_text = "word " * 200
    capped = models.truncate_tokens(long_text, 10)
    assert models.count_tokens(capped) <= 10

    short_text = "just a few words"
    assert models.truncate_tokens(short_text, 1000) == short_text
