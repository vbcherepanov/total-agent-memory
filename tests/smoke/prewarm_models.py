"""Download the default embedding and cross-encoder models into FASTEMBED_CACHE_PATH.

The test suite blocks network access inside the fast hot path, so the default
text and code models must already be cached before pytest starts. The
cross-encoder is cached too, so the exit-during-model-load regression
(tests/test_exit_join.py) runs instead of skipping. Run from a source checkout
or an unpacked source distribution.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

import config  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402
from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: E402
from memory_core.fastembed_loader import load_model  # noqa: E402
from memory_core.multilingual_rerank import MODEL, register_model  # noqa: E402


def main() -> int:
    cache = os.environ.get("FASTEMBED_CACHE_PATH")
    if not cache:
        print("FASTEMBED_CACHE_PATH must point at the model cache", file=sys.stderr)
        return 2
    models = sorted({config.get_text_embed_model(), config.get_code_embed_model()} - {""})
    for name in models:
        load_model(TextEmbedding, name)
        print(f"cached {name} in {cache}")
    reranker = config.get_cross_rerank_model()
    if reranker == MODEL:
        register_model()
    load_model(TextCrossEncoder, reranker)
    print(f"cached {reranker} in {cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
