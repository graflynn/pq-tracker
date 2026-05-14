"""Local text embeddings for semantic search over the pq-tracker corpus.

Uses fastembed's ONNX port of BAAI/bge-small-en-v1.5 — small (~120MB), runs
on CPU, dimensions=384, vectors are already L2-normalized so cosine = dot.

The embeddings table is keyed by (source_type, source_pq_ref / source_pdf_id,
chunk_index) and rebuildable from source text. Vectors are stored as packed
float32 BLOBs.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384

_model = None


def get_model():
    """Lazy-load and cache the embedding model. First call downloads ~120MB."""
    global _model
    if _model is None:
        # Imported lazily so callers that only need pack/unpack don't pay the
        # fastembed/onnxruntime import cost.
        from fastembed import TextEmbedding
        log.info("loading embedding model %s (first call may download ~120MB)", MODEL_NAME)
        _model = TextEmbedding(MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a batch of texts. Returns float32 array of shape (n, DIMS)."""
    if not texts:
        return np.zeros((0, DIMS), dtype=np.float32)
    model = get_model()
    arr = np.stack(list(model.embed(texts))).astype(np.float32)
    return arr


def pack(v: np.ndarray) -> bytes:
    """Pack a (DIMS,) float32 vector to BLOB."""
    return np.ascontiguousarray(v, dtype=np.float32).tobytes()


def unpack(b: bytes) -> np.ndarray:
    """Unpack a BLOB to (DIMS,) float32. No-copy view."""
    return np.frombuffer(b, dtype=np.float32)


def cosine_topk(query_vec: np.ndarray, matrix: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Top-k cosine similarities. Assumes both are L2-normalized (BGE is)."""
    if matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)
    scores = matrix @ query_vec.astype(np.float32)
    k = min(k, scores.shape[0])
    # argpartition is O(n); then sort the partition. Faster than full argsort for k << n.
    idx = np.argpartition(-scores, k - 1)[:k]
    order = idx[np.argsort(-scores[idx])]
    return order, scores[order]


def chunk_text(text: str, target_chars: int = 1500, overlap: int = 200) -> list[str]:
    """Split a long text into overlapping chunks, preferring sentence breaks.

    target_chars ~ 400 tokens (BGE-small's max is 512). Most question_text fits
    in one chunk; longer answer_text may take 2-3.
    """
    if not text:
        return []
    text = text.strip()
    if len(text) <= target_chars:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text):
        end = min(len(text), i + target_chars)
        if end < len(text):
            # Try to break on a clean boundary inside the back half of the window.
            for sep in ("\n\n", ". ", ".\n", "\n", " "):
                idx = text.rfind(sep, i + target_chars // 2, end)
                if idx > 0:
                    end = idx + len(sep)
                    break
        chunk = text[i:end].strip()
        if chunk:
            out.append(chunk)
        if end >= len(text):
            break
        i = max(i + 1, end - overlap)
    return out


def delete_for_pq(conn: sqlite3.Connection, pq_ref: str, source_type: Optional[str] = None) -> int:
    """Remove existing embeddings for a PQ, optionally scoped to one source_type."""
    if source_type:
        cur = conn.execute(
            "DELETE FROM embeddings WHERE source_pq_ref = ? AND source_type = ?",
            (pq_ref, source_type),
        )
    else:
        cur = conn.execute("DELETE FROM embeddings WHERE source_pq_ref = ?", (pq_ref,))
    return cur.rowcount


def insert_embeddings(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    """Bulk-insert. Each row: (source_type, source_pq_ref, source_pdf_id,
    chunk_index, text_excerpt, model, dims, vector_blob, fetched_at)."""
    conn.executemany(
        """INSERT INTO embeddings(source_type, source_pq_ref, source_pdf_id,
                                  chunk_index, text_excerpt, model, dims,
                                  vector, fetched_at)
                          VALUES (?,?,?,?,?,?,?,?,?)""",
        list(rows),
    )


def load_matrix(conn: sqlite3.Connection, *, source_type: str) -> tuple[list[tuple], np.ndarray]:
    """Load every embedding of a given source_type into a single matrix.

    Returns (metadata_rows, matrix). metadata_rows[i] is
    (id, source_pq_ref, source_pdf_id, chunk_index, text_excerpt) for row i of matrix.

    At our scale (~5k-20k vectors total) it's cheaper to keep this in memory
    per-process than to use a vector extension.
    """
    rows = conn.execute(
        """SELECT id, source_pq_ref, source_pdf_id, chunk_index, text_excerpt, vector
             FROM embeddings WHERE source_type = ? ORDER BY id""",
        (source_type,),
    ).fetchall()
    meta = [(r["id"], r["source_pq_ref"], r["source_pdf_id"], r["chunk_index"], r["text_excerpt"])
            for r in rows]
    if not rows:
        return meta, np.zeros((0, DIMS), dtype=np.float32)
    matrix = np.stack([unpack(r["vector"]) for r in rows]).astype(np.float32)
    return meta, matrix
