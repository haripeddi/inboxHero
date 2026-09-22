#!/usr/bin/env python3
"""Build a per-message vector index for the inbox (Part 3 RAG)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from index_store import (  # noqa: E402
    INDEX_DIR,
    build_embeddings,
    load_inbox,
    save_index,
)


def main() -> None:
    messages = load_inbox()
    embeddings, meta_rows, backend = build_embeddings(messages)
    save_index(embeddings, meta_rows, backend)

    ids = {r["id"] for r in meta_rows}
    expected = {m["id"] for m in messages}
    if ids != expected or len(meta_rows) != len(messages):
        raise SystemExit(
            f"Index coverage failed: indexed={len(ids)} inbox={len(expected)}"
        )

    print(f"Wrote index under {INDEX_DIR}")
    print(f"messages indexed: {len(meta_rows)}")
    print(f"embedder: {backend.get('embedder')} ({backend.get('model')})")
    print(f"embedding shape: {embeddings.shape}")


if __name__ == "__main__":
    main()
