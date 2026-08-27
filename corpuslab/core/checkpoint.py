"""resume: manifest compatibility check, terminal sets, LSH index rebuild
(docs/checkpoint-design.md §6/§7)."""
from __future__ import annotations

import hashlib
from typing import Any, Optional

import numpy as np
from datasketch import MinHash, MinHashLSH

from corpuslab.config.loader import dumps_state_relevant
from corpuslab.core.sample import FORMAT_VERSION
from corpuslab.core.store import SCHEMA_VERSION


class IncompatibleState(Exception):
    def __init__(self, msg: str, discardable: Optional[list] = None):
        super().__init__(msg)
        self.discardable = discardable or []


def config_fingerprint(cfg: Any) -> str:
    """Fingerprint of state-relevant config (plan/preview are excluded:
    changing them keeps resume compatible)."""
    return hashlib.sha256(dumps_state_relevant(cfg).encode()).hexdigest()[:16]


def write_manifest(store: Any, cfg: Any, *, num_perm: Optional[int] = None,
                   embedding_model: Optional[str] = None, discard: bool = False) -> None:
    want = {
        "version": SCHEMA_VERSION,
        "format_version": FORMAT_VERSION,
        "config_hash": config_fingerprint(cfg),
        "seed": str(getattr(getattr(cfg, "run", None), "seed", "") or ""),
    }
    if num_perm:
        want["minhash_num_perm"] = str(num_perm)
    if embedding_model:
        want["embedding_model"] = embedding_model

    have = store.manifest()
    problems: list = []
    if have.get("version") not in (None, want["version"]):
        problems.append(("version", have.get("version"), want["version"], None))

    if have.get("format_version") not in (None, want["format_version"]):
        problems.append(("format_version", have.get("format_version"),
                         want["format_version"], None))

    if have.get("config_hash") not in (None, want["config_hash"]):
        problems.append(("config_hash", have.get("config_hash"), want["config_hash"], None))

    if have.get("seed") not in (None, want["seed"]):
        problems.append(("seed", have.get("seed"), want["seed"], None))

    # num_perm change → signatures invalid (dimension changed); drop only minhash_sigs
    if num_perm and have.get("minhash_num_perm") not in (None, str(num_perm)):
        problems.append(("minhash_num_perm", have.get("minhash_num_perm"),
                         str(num_perm), "minhash_sigs"))
    if embedding_model and have.get("embedding_model") not in (None, embedding_model):
        problems.append(("embedding_model", have.get("embedding_model"),
                         embedding_model, "embeddings"))

    if problems:
        fatal = [p for p in problems if p[3] is None]
        discardable = [p[3] for p in problems if p[3]]
        if fatal and not discard:
            raise IncompatibleState(
                "state store incompatible with current config: "
                + "; ".join(f"{p[0]} {p[1]!r} → {p[2]!r}" for p in fatal)
                + " (rerun with --discard-state to drop it)",
                discardable=discardable)
        if discardable:
            if "minhash_sigs" in discardable:
                store.conn.execute("DELETE FROM minhash_sigs")
            if "embeddings" in discardable:
                store.conn.execute("DELETE FROM embeddings")

    for k, v in want.items():
        store.set_kv(k, v)


def restore(store: Any, ctx: Any) -> dict:
    """resume reconciliation: terminal sets + LSH index rebuild. Returns a
    recovery summary."""
    terminal = store.terminal_ids()
    planned = store.planned_ids()

    # LSH index rebuild: signatures are state, the index is a view (threshold
    # comes from current config)
    lsh = None
    num_perm = None
    for stage in getattr(ctx, "_minhash_meta", []) or []:
        num_perm = stage["num_perm"]
        lsh = MinHashLSH(threshold=stage["threshold"], num_perm=num_perm)
        for sid, sig in store.load_sigs():
            m = MinHash(num_perm=num_perm)
            # datasketch hashes are uint32 — a wider dtype breaks band
            # hashing and makes every resume query miss
            m.hashvalues = np.array(sig, dtype=np.uint32)
            try:
                lsh.insert(sid, m)
            except ValueError:
                pass                        # duplicate insert (idempotent replay)

    return {
        "terminal": terminal,
        "planned": planned,
        "lsh": lsh,
        "num_perm": num_perm,
        "pending": store.load_pending() if store is not None else [],
    }
