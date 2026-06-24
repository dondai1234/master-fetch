"""Local neural reranker (v7 Phase 2).

Runs an ONNX cross-encoder (Apache-2.0 `cross-encoder/ms-marco-MiniLM-L-6-v2`,
22.7M params, trained on MS MARCO passage reranking = query/document relevance)
on the `onnxruntime` we ALREADY ship for OCR. No new runtime. The model + tokenizer
are downloaded ONCE on first neural search into
`~/.master_fetch_cache/models/msmarco-minilm-l6-v2/` (pinned to a specific HF
revision + hash-checked), NOT bundled in the wheel, so the lean install stays small.

Graceful fallback: if onnxruntime/tokenizers are missing (lean install) or the
model download fails / is offline, `get_reranker()` returns None and the caller
falls back to BM25 keyword rerank. Neural mode is an `[all]` extra; lean installs
get a fully functional keyword search.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import asyncio
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("master-fetch.reranker")

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# Pinned revision for reproducibility (downloaded files never shift under us).
MODEL_REV = "c5ee24cb16019beea0893ab7796b1df96625c6b8"
_BASE = f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REV}"
MODEL_FILES = {
    "model.onnx": f"{_BASE}/onnx/model.onnx",
    "tokenizer.json": f"{_BASE}/tokenizer.json",
    "vocab.txt": f"{_BASE}/vocab.txt",
}
MODEL_DIR = Path.home() / ".master_fetch_cache" / "models" / "msmarco-minilm-l6-v2"
MAX_SEQ = 512
# Sanity floor so a truncated/failed download is rejected (real onnx is ~80MB).
MIN_MODEL_BYTES = 50_000_000

_reranker: Optional[object] = None
_reranker_tried: bool = False
_reranker_unavailable_reason: str = ""


class _Reranker:
    """Wraps a warm ONNX InferenceSession + tokenizer for query/doc scoring."""

    def __init__(self, model_path: Path, tokenizer):
        import numpy as np
        import onnxruntime as ort
        so = ort.SessionOptions()
        try:
            so.graph_opt_level = ort.GraphOptLevel.ORT_ENABLE_ALL
        except Exception:
            pass
        self.sess = ort.InferenceSession(
            str(model_path), so, providers=["CPUExecutionProvider"],
        )
        self.input_names = [i.name for i in self.sess.get_inputs()]
        self.tok = tokenizer
        try:
            self.tok.enable_truncation(max_length=MAX_SEQ)
        except Exception:
            pass
        self._np = np

    def _feed(self, input_ids, attn, type_ids):
        arrs = {"input_ids": input_ids, "attention_mask": attn, "token_type_ids": type_ids}
        feed = {}
        for name in self.input_names:
            if name in arrs:
                feed[name] = arrs[name]
        # Positional fallback for any oddly-named inputs.
        canon = [input_ids, attn, type_ids]
        ci = 0
        for name in self.input_names:
            if name in feed:
                continue
            feed[name] = canon[ci % len(canon)]
            ci += 1
        return feed

    def score(self, query: str, docs: list[str]) -> list[float]:
        """Return a sigmoid relevance score (0..1) for each (query, doc) pair."""
        if not docs:
            return []
        np = self._np
        encs = [self.tok.encode(query, d) for d in docs]
        maxlen = max((len(e.ids) for e in encs), default=0) or 1
        n = len(encs)
        input_ids = np.zeros((n, maxlen), dtype=np.int64)
        attn = np.zeros((n, maxlen), dtype=np.int64)
        type_ids = np.zeros((n, maxlen), dtype=np.int64)
        for i, e in enumerate(encs):
            L = len(e.ids)
            input_ids[i, :L] = e.ids
            attn[i, :L] = e.attention_mask
            type_ids[i, :L] = e.type_ids
        try:
            out = self.sess.run(None, self._feed(input_ids, attn, type_ids))
        except Exception as e:
            logger.warning(f"reranker inference failed: {e}")
            return [0.0] * n
        logits = np.asarray(out[0]).reshape(-1)
        scores = []
        for i in range(n):
            try:
                scores.append(1.0 / (1.0 + math.exp(-float(logits[i]))))
            except (OverflowError, ValueError):
                scores.append(0.0 if float(logits[i]) < 0 else 1.0)
        return scores


def _download_file(url: str, dest: Path) -> bool:
    """Stream a file to disk. Returns True on success."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "hound-mcp/7"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
        if total and done < total:
            logger.warning(f"short download for {dest.name}: {done}/{total} bytes")
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(dest)
        return True
    except Exception as e:
        logger.warning(f"download failed for {url}: {e}")
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_model() -> Optional[tuple[Path, Path]]:
    """Ensure model.onnx + tokenizer.json are present + valid. Returns paths or None."""
    onnx = MODEL_DIR / "model.onnx"
    tokjson = MODEL_DIR / "tokenizer.json"
    sha_file = MODEL_DIR / "model.sha256"

    need = []
    if not onnx.exists() or onnx.stat().st_size < MIN_MODEL_BYTES:
        need.append("model.onnx")
    if not tokjson.exists():
        need.append("tokenizer.json")

    if need:
        logger.info(
            "Hound: downloading the local search reranker model (one-time, ~80MB)..."
        )
        for name in need:
            if not _download_file(MODEL_FILES[name], MODEL_DIR / name):
                return None
        if onnx.stat().st_size < MIN_MODEL_BYTES:
            logger.warning("downloaded model.onnx is too small; rejecting")
            return None
        # Record the hash so a later corrupt/partial file is detected.
        try:
            sha_file.write_text(_sha256(onnx))
        except Exception:
            pass

    # Verify the onnx still matches its recorded hash (detect corruption).
    if sha_file.exists():
        try:
            if _sha256(onnx) != sha_file.read_text().strip():
                logger.warning("model.onnx hash mismatch; re-downloading")
                if _download_file(MODEL_FILES["model.onnx"], onnx):
                    sha_file.write_text(_sha256(onnx))
                else:
                    return None
        except Exception:
            pass

    return onnx, tokjson


def get_reranker() -> Optional[_Reranker]:
    """Return a warm singleton reranker, or None if unavailable.

    None means: fall back to BM25. Never raises. Caches the 'tried' state so a
    one-time failure doesn't retry every call (retry again on next process).
    """
    global _reranker, _reranker_tried, _reranker_unavailable_reason
    if _reranker is not None:
        return _reranker
    if _reranker_tried:
        return None
    _reranker_tried = True
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        _reranker_unavailable_reason = (
            f"neural rerank needs hound-mcp[all] ({e.__class__.__name__}: {e})"
        )
        return None
    try:
        paths = _ensure_model()
        if paths is None:
            _reranker_unavailable_reason = "reranker model download failed (offline?)"
            return None
        onnx_path, tok_path = paths
        from tokenizers import Tokenizer
        try:
            tok = Tokenizer.from_file(str(tok_path))
        except Exception:
            # Fallback: build a BERT WordPiece tokenizer from vocab.txt.
            from tokenizers import BertWordPieceTokenizer
            tok = BertWordPieceTokenizer(str(MODEL_DIR / "vocab.txt"), lowercase=True)
        _reranker = _Reranker(onnx_path, tok)
        logger.info("Hound: neural reranker ready (ms-marco-MiniLM-L-6-v2, ONNX).")
        return _reranker
    except Exception as e:
        _reranker_unavailable_reason = f"reranker init failed: {e}"
        logger.warning(f"reranker init failed: {e}")
        return None


def rerank(query: str, results: list) -> Optional[list[tuple]]:
    """Rerank RawResults with the neural cross-encoder. Returns (result, score)
    pairs sorted desc, or None if the reranker is unavailable (caller falls back)."""
    rer = get_reranker()
    if rer is None or not results:
        return None
    docs = [f"{r.title} {r.snippet}" for r in results]
    try:
        scores = rer.score(query, docs)
    except Exception as e:
        logger.warning(f"neural rerank failed: {e}")
        return None
    if len(scores) != len(results):
        return None
    pairs = list(zip(results, scores))
    pairs.sort(key=lambda rs: (-rs[1], rs[0].position))
    return pairs


def unavailable_reason() -> str:
    return _reranker_unavailable_reason


def model_present() -> bool:
    """True if the reranker model + tokenizer are already cached locally (so
    get_reranker() will NOT trigger a download). Used by startup prewarm to warm
    the ONNX session only when it is free to do so."""
    onnx = MODEL_DIR / "model.onnx"
    tokjson = MODEL_DIR / "tokenizer.json"
    return (onnx.exists() and onnx.stat().st_size >= MIN_MODEL_BYTES
            and tokjson.exists())


async def prewarm_reranker() -> None:
    """Best-effort startup prewarm: if the reranker model is already cached, load
    the ONNX session now (in a worker thread) so the first neural search skips
    the ~1-2s init. Does NOT download (skips when the model is absent). Never raises."""
    if not model_present():
        return
    try:
        await asyncio.to_thread(get_reranker)
    except Exception:
        pass


