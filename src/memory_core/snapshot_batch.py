from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from memory_core.snapshot_semantic import SnapshotEmbeddingUnavailable, SnapshotVector
from memory_core.telemetry import counters, op_timer

OLLAMA_EMBED_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class SnapshotEncoder:
    identity: SnapshotVector
    encode: Callable[[Sequence[str]], Sequence[Sequence[float]]]


@dataclass(frozen=True)
class SnapshotBatch:
    records: tuple[tuple[str, str], ...]
    encoders: dict[str, SnapshotEncoder]

    @op_timer("snapshot_batch_inference_ms")
    def execute(self) -> list[SnapshotVector | SnapshotEmbeddingUnavailable]:
        output: list[SnapshotVector | SnapshotEmbeddingUnavailable | None] = [None] * len(self.records)
        groups: dict[str, list[int]] = {}
        for index, (_text, desired) in enumerate(self.records):
            groups.setdefault(desired if desired in self.encoders else "text", []).append(index)
        pending_text = groups.pop("text", [])
        for space, indices in groups.items():
            try:
                self._encode(space, indices, output)
            except Exception:
                # The primary text encoder is the same fallback used by ordinary saves.
                counters.bump("snapshot_batch_space_fallbacks")
                pending_text.extend(indices)
        if pending_text:
            try:
                self._encode("text", pending_text, output)
            except Exception as error:
                for index in pending_text:
                    output[index] = SnapshotEmbeddingUnavailable("text", str(error))
        if any(value is None for value in output):
            raise RuntimeError("Snapshot batch left an input without a vector or diagnostic")
        counters.bump("snapshot_batch_records", len(output))
        return output

    def _encode(self, space: str, indices: list[int], output: list[SnapshotVector | SnapshotEmbeddingUnavailable | None]) -> None:
        encoder = self.encoders.get(space)
        if encoder is None:
            raise SnapshotEmbeddingUnavailable(space, "Historical batch encoder unavailable")
        vectors = list(encoder.encode([self.records[index][0] for index in indices]))
        if len(vectors) != len(indices):
            raise ValueError("Historical encoder returned the wrong vector count")
        identity = encoder.identity
        for index, vector in zip(indices, vectors, strict=True):
            if len(vector) != len(identity.values):
                raise ValueError("Historical batch dimensions differ from the query encoder")
            output[index] = SnapshotVector(list(vector), identity.model, identity.provider, space, identity.identity_key)


def ollama_vectors(texts: Sequence[str], *, url: str, model: str) -> list[list[float]]:
    vectors = []
    for text in texts:
        request = urllib.request.Request(url.rstrip("/") + "/api/embeddings",
            data=json.dumps({"model": model, "prompt": text}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        counters.bump("network_calls")
        with urllib.request.urlopen(request, timeout=OLLAMA_EMBED_TIMEOUT_SECONDS) as response:
            vectors.append(json.load(response)["embedding"])
    return vectors
