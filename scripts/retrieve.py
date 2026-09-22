"""Hybrid retrieval: thread walk + vector top-k (Part 3)."""

from __future__ import annotations

from typing import Any

from index_store import (
    RETRIEVAL_METHOD,
    cosine_topk,
    encode_query,
    load_inbox,
    load_index,
    messages_by_id,
    thread_messages,
    top_body,
)


def hybrid_retrieve(
    target_id: str,
    query: str | None = None,
    k: int = 8,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Retrieve evidence for drafting a reply to target_id.

    1) Walk the entire same thread_id (chronological).
    2) Vector-search the full mail store for additional context.
    3) Merge / dedupe by message id.
    """
    inbox = messages if messages is not None else load_inbox()
    by_id = messages_by_id(inbox)
    if target_id not in by_id:
        raise SystemExit(f"Unknown target message id: {target_id}")

    target = by_id[target_id]
    embeddings, meta = load_index()
    meta_msgs: list[dict[str, Any]] = meta["messages"]
    id_list = [m["id"] for m in meta_msgs]
    id_to_row = {mid: i for i, mid in enumerate(id_list)}

    # --- 1) Thread walk ---
    thr = thread_messages(inbox, target["thread_id"])
    thread_ids = [m["id"] for m in thr]
    thread_hits: list[dict[str, Any]] = []
    for m in thr:
        thread_hits.append(
            {
                "id": m["id"],
                "thread_id": m["thread_id"],
                "subject": m["subject"],
                "from": m["from"],
                "timestamp": m["timestamp"],
                "body_top": top_body(m.get("body", "")),
                "source": "thread_walk",
                "score": 1.0 if m["id"] != target_id else 0.0,
            }
        )

    # --- 2) Vector search ---
    q = query or _default_query(target)
    qvec = encode_query(q, meta)
    exclude = {target_id}  # don't cite the target as evidence of itself
    # Prefer hits outside the thread for cross-thread context, but allow in-thread
    # re-ranking; merge step dedupes.
    top = cosine_topk(
        qvec,
        embeddings,
        k=k,
        exclude_ids=exclude,
        id_list=id_list,
    )
    vector_hits: list[dict[str, Any]] = []
    vector_hit_ids: list[str] = []
    for row_idx, score in top:
        mid = id_list[row_idx]
        row = meta_msgs[row_idx]
        vector_hit_ids.append(mid)
        vector_hits.append(
            {
                "id": mid,
                "thread_id": row["thread_id"],
                "subject": row["subject"],
                "from": row["from"],
                "timestamp": row["timestamp"],
                "body_top": row["body_top"],
                "source": "vector",
                "score": score,
            }
        )

    # --- 3) Merge / dedupe (thread first, then vector-only extras) ---
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for hit in thread_hits + vector_hits:
        if hit["id"] in seen:
            for existing in merged:
                if existing["id"] == hit["id"]:
                    if hit["source"] == "vector":
                        existing["source"] = "thread_walk+vector"
                        existing["score"] = max(
                            float(existing.get("score") or 0), hit["score"]
                        )
                    break
            continue
        seen.add(hit["id"])
        merged.append(hit)

    return {
        "retrieval_method": RETRIEVAL_METHOD,
        "target_id": target_id,
        "query": q,
        "thread_id": target["thread_id"],
        "thread_ids_read": thread_ids,
        "vector_hit_ids": vector_hit_ids,
        "hits": merged,
        "mail_store_ids": set(by_id.keys()),
        "embedding_dim": int(embeddings.shape[1]) if embeddings.ndim == 2 else 0,
        "index_row": id_to_row.get(target_id),
    }


def _default_query(target: dict[str, Any]) -> str:
    return f"{target.get('subject', '')}\n{top_body(target.get('body', ''))}"


def validate_citations(
    cited_ids: list[str],
    retrieved_ids: set[str],
    mail_store_ids: set[str],
) -> None:
    """Fail if a citation was never read or does not exist in the mail store."""
    for cid in cited_ids:
        if cid not in mail_store_ids:
            raise SystemExit(
                f"Citation check failed: {cid} is not in the mail store"
            )
        if cid not in retrieved_ids:
            raise SystemExit(
                f"Citation check failed: {cid} was cited but never retrieved"
            )


def evidence_contains(hits: list[dict[str, Any]], needle: str) -> bool:
    blob = "\n".join(h.get("body_top", "") for h in hits)
    return needle.lower() in blob.lower()
