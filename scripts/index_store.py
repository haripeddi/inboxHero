"""Shared mail-store helpers and vector index I/O for Part 3 RAG."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
INBOX_PATH = ROOT / "inbox.json"
INDEX_DIR = ROOT / "indexes" / "inbox_vectors"
EMBEDDINGS_PATH = INDEX_DIR / "embeddings.npy"
META_PATH = INDEX_DIR / "meta.json"

RETRIEVAL_METHOD = "hybrid_thread_walk_plus_vector"
EMBED_MODEL_NAME = "local_tfidf_v1"


def top_body(body: str) -> str:
    """Newest reply text only (strip Outlook-style quoted history)."""
    lines: list[str] = []
    for line in body.splitlines():
        if re.match(r"^On .+ wrote:\s*$", line) or line.startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def load_inbox(path: Path | None = None) -> list[dict[str, Any]]:
    messages = json.loads((path or INBOX_PATH).read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        raise SystemExit("inbox.json must be a JSON array")
    return messages


def messages_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in messages}


def thread_messages(
    messages: list[dict[str, Any]], thread_id: str
) -> list[dict[str, Any]]:
    thr = [m for m in messages if m.get("thread_id") == thread_id]
    thr.sort(key=lambda m: (m["timestamp"], m["id"]))
    return thr


def embed_text_for_message(msg: dict[str, Any]) -> str:
    """Text that goes into the vector (metadata + body_top)."""
    top = top_body(msg.get("body", ""))
    return (
        f"subject: {msg.get('subject', '')}\n"
        f"from: {msg.get('from', '')}\n"
        f"to: {msg.get('to', '')}\n"
        f"thread: {msg.get('thread_id', '')}\n"
        f"{top}"
    )


_TOKEN_RE = re.compile(r"[a-z0-9$]+(?:'[a-z]+)?", re.I)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _build_vocab(docs: list[list[str]]) -> dict[str, int]:
    vocab: dict[str, int] = {}
    for tokens in docs:
        for t in tokens:
            if t not in vocab:
                vocab[t] = len(vocab)
    return vocab


def _tfidf_matrix(docs: list[list[str]], vocab: dict[str, int]) -> np.ndarray:
    n_docs = len(docs)
    n_terms = len(vocab)
    if n_docs == 0 or n_terms == 0:
        return np.zeros((n_docs, max(n_terms, 1)), dtype=np.float64)

    df = np.zeros(n_terms, dtype=np.float64)
    for tokens in docs:
        for idx in {vocab[t] for t in tokens if t in vocab}:
            df[idx] += 1.0

    idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0
    mat = np.zeros((n_docs, n_terms), dtype=np.float64)
    for i, tokens in enumerate(docs):
        counts = Counter(t for t in tokens if t in vocab)
        if not counts:
            continue
        max_tf = max(counts.values())
        for term, c in counts.items():
            j = vocab[term]
            tf = 0.5 + 0.5 * (c / max_tf)
            mat[i, j] = tf * idf[j]
        norm = np.linalg.norm(mat[i])
        if norm > 0:
            mat[i] /= norm
    return mat


def _tfidf_query(
    query_tokens: list[str], vocab: dict[str, int], idf: np.ndarray
) -> np.ndarray:
    n_terms = len(vocab)
    vec = np.zeros(n_terms, dtype=np.float64)
    counts = Counter(t for t in query_tokens if t in vocab)
    if not counts:
        return vec
    max_tf = max(counts.values())
    for term, c in counts.items():
        j = vocab[term]
        tf = 0.5 + 0.5 * (c / max_tf)
        vec[j] = tf * idf[j]
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


def try_sentence_transformer_encode(texts: list[str]) -> np.ndarray | None:
    """Optional semantic embeddings if sentence-transformers is installed."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        return None
    model = SentenceTransformer("all-MiniLM-L6-v2")
    arr = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(arr, dtype=np.float64)


def build_embeddings(
    messages: list[dict[str, Any]],
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    """
    Embed every message. Prefer sentence-transformers; fall back to local TF-IDF.
    Returns (matrix [N,D], meta rows, backend info).
    """
    texts = [embed_text_for_message(m) for m in messages]
    meta_rows: list[dict[str, Any]] = []
    for m in messages:
        meta_rows.append(
            {
                "id": m["id"],
                "thread_id": m["thread_id"],
                "subject": m["subject"],
                "from": m["from"],
                "to": m["to"],
                "timestamp": m["timestamp"],
                "body_top": top_body(m.get("body", "")),
            }
        )

    st = try_sentence_transformer_encode(texts)
    if st is not None and st.shape[0] == len(messages):
        backend = {
            "embedder": "sentence_transformers",
            "model": "all-MiniLM-L6-v2",
            "retrieval_method": RETRIEVAL_METHOD,
        }
        return st, meta_rows, backend

    tokenized = [tokenize(t) for t in texts]
    vocab = _build_vocab(tokenized)
    mat = _tfidf_matrix(tokenized, vocab)
    # Persist vocab + idf for query-time encoding
    df = np.zeros(len(vocab), dtype=np.float64)
    for tokens in tokenized:
        for idx in {vocab[t] for t in tokens if t in vocab}:
            df[idx] += 1.0
    idf = np.log((1.0 + len(tokenized)) / (1.0 + df)) + 1.0
    backend = {
        "embedder": "tfidf",
        "model": EMBED_MODEL_NAME,
        "retrieval_method": RETRIEVAL_METHOD,
        "vocab": vocab,
        "idf": idf.tolist(),
    }
    return mat, meta_rows, backend


def save_index(
    embeddings: np.ndarray,
    meta_rows: list[dict[str, Any]],
    backend: dict[str, Any],
) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(EMBEDDINGS_PATH, embeddings)
    payload = {
        "message_count": len(meta_rows),
        "backend": {
            k: v
            for k, v in backend.items()
            if k not in {"vocab", "idf"}
        },
        "vocab": backend.get("vocab"),
        "idf": backend.get("idf"),
        "messages": meta_rows,
    }
    META_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_index() -> tuple[np.ndarray, dict[str, Any]]:
    if not EMBEDDINGS_PATH.exists() or not META_PATH.exists():
        raise SystemExit(
            f"Vector index missing. Run: python3 scripts/build_index.py\n"
            f"Expected {EMBEDDINGS_PATH} and {META_PATH}"
        )
    embeddings = np.load(EMBEDDINGS_PATH)
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    return embeddings, meta


def encode_query(query: str, meta: dict[str, Any]) -> np.ndarray:
    backend = meta.get("backend") or {}
    embedder = backend.get("embedder", "tfidf")
    if embedder == "sentence_transformers":
        st = try_sentence_transformer_encode([query])
        if st is None:
            raise SystemExit(
                "Index was built with sentence-transformers but the package "
                "is not installed. Reinstall or rebuild with TF-IDF."
            )
        return st[0]

    vocab = meta.get("vocab") or {}
    idf = np.asarray(meta.get("idf") or [], dtype=np.float64)
    if not vocab or idf.size == 0:
        raise SystemExit("TF-IDF index meta missing vocab/idf; rebuild the index.")
    return _tfidf_query(tokenize(query), vocab, idf)


def cosine_topk(
    query_vec: np.ndarray,
    embeddings: np.ndarray,
    k: int,
    exclude_ids: set[str] | None = None,
    id_list: list[str] | None = None,
) -> list[tuple[int, float]]:
    """Return list of (row_index, score) sorted by score desc."""
    if embeddings.size == 0:
        return []
    # Normalize query if needed
    qn = np.linalg.norm(query_vec)
    q = query_vec / qn if qn > 0 else query_vec
    scores = embeddings @ q
    order = np.argsort(-scores)
    exclude = exclude_ids or set()
    hits: list[tuple[int, float]] = []
    for idx in order:
        mid = id_list[idx] if id_list else str(idx)
        if mid in exclude:
            continue
        score = float(scores[idx])
        if score <= 0 and math.isclose(score, 0.0):
            # keep non-positive only if we still need fillers — skip zeros
            if score == 0.0:
                continue
        hits.append((int(idx), score))
        if len(hits) >= k:
            break
    return hits
