"""DuckDB state store (S4: the single persistence point).

One .duckdb file carries all state and output (docs/checkpoint-design.md §4):
samples / events / pending / embeddings / fingerprints / minhash_sigs /
scores / dropped / planned / kv (manifest).

Atomicity = DuckDB transactions: effects and markers commit together, so a
crash never leaves a half-applied write. All connection use stays on the
event loop (single writer, no locks).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

import duckdb

from corpuslab.core.sample import Sample, TaskSpec

SCHEMA_VERSION = "1"

_DDL = [
    """CREATE TABLE IF NOT EXISTS samples(
        id VARCHAR PRIMARY KEY, strategy VARCHAR, payload VARCHAR,
        rendered VARCHAR, total_score DOUBLE, created_at TIMESTAMP DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS events(
        seq BIGINT, t VARCHAR, id VARCHAR, strategy VARCHAR, data VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS pending(
        id VARCHAR PRIMARY KEY, strategy VARCHAR, sample VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS embeddings(
        text_hash VARCHAR, model VARCHAR, vec DOUBLE[], PRIMARY KEY(text_hash, model))""",
    """CREATE TABLE IF NOT EXISTS fingerprints(
        hash VARCHAR PRIMARY KEY, sample_id VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS minhash_sigs(
        sample_id VARCHAR PRIMARY KEY, sig VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS scores(
        id VARCHAR, endpoint VARCHAR, scores VARCHAR, total DOUBLE,
        PRIMARY KEY(id, endpoint))""",
    """CREATE TABLE IF NOT EXISTS dropped(
        id VARCHAR PRIMARY KEY, strategy VARCHAR, stage VARCHAR, reason VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS planned(
        id VARCHAR PRIMARY KEY, strategy VARCHAR, spec VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS kv(k VARCHAR PRIMARY KEY, v VARCHAR)""",
]

# cache_cleanup tiers: clear only the truly transient tables. Terminal sets
# (samples, dropped, planned) and dedup state (fingerprints, minhash_sigs),
# scores and embeddings all persist — otherwise a later --resume would
# regenerate every previously-dropped sample (wasted LLM money) or let
# duplicates slip through. The invariant: a rerun never produces duplicate
# samples and never re-spends money on already-finished ids.
_PROGRESS_TABLES = ["events", "pending"]


class Store:
    """DuckDB state store. open() is idempotent; all writes are wrapped in transactions."""

    def __init__(self, path: str, table: str = "samples"):
        self.path = path
        self.table = table
        self._seq = int(time.time() * 1000) << 20      # monotonic event sequence
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = duckdb.connect(path)
        for ddl in _DDL:
            self.conn.execute(ddl)
        # User-facing table name is configurable (default: samples)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table}("
            f"id VARCHAR PRIMARY KEY, strategy VARCHAR, payload VARCHAR, "
            f"rendered VARCHAR, total_score DOUBLE, created_at TIMESTAMP DEFAULT now())")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ── Transactions ──────────────────────────────────────
    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ── Event audit log ───────────────────────────────────
    def event(self, t: str, sid: str = "", strategy: str = "",
              data: Optional[dict] = None) -> None:
        self._seq += 1
        self.conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            [self._seq, t, sid, strategy,
             json.dumps(data or {}, ensure_ascii=False)])

    # ── Manifest (kv) ─────────────────────────────────────
    def set_kv(self, k: str, v: str) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM kv WHERE k=?", [k])
            self.conn.execute("INSERT INTO kv VALUES (?, ?)", [k, v])

    def get_kv(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", [k]).fetchone()
        return row[0] if row else None

    def manifest(self) -> dict:
        rows = self.conn.execute("SELECT k, v FROM kv").fetchall()
        return dict(rows)

    # ── planned (Plan products, idempotent) ───────────────
    def mark_planned(self, spec: TaskSpec) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO planned VALUES (?, ?, ?)",
                [spec.id, spec.strategy, spec.to_json()])
            self.event("planned", spec.id, spec.strategy,
                       {"payload_keys": sorted(spec.payload)})

    def planned_ids(self) -> set:
        rows = self.conn.execute("SELECT id FROM planned").fetchall()
        return {r[0] for r in rows}

    # ── Terminal sets ─────────────────────────────────────
    def sample_ids(self) -> set:
        rows = self.conn.execute(f"SELECT id FROM {self.table}").fetchall()
        return {r[0] for r in rows}

    def dropped_ids(self) -> set:
        rows = self.conn.execute("SELECT id FROM dropped").fetchall()
        return {r[0] for r in rows}

    def terminal_ids(self) -> set:
        """samples ∪ dropped — the "already finished" set (resume skip list)."""
        return self.sample_ids() | self.dropped_ids()

    # ── pending (batch-barrier disk backpressure) ─────────
    def save_pending(self, sample: Sample) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?)",
                [sample.id, sample.strategy, sample.to_json()])

    def load_pending(self) -> list:
        rows = self.conn.execute(
            "SELECT sample FROM pending ORDER BY strategy, id").fetchall()
        return [Sample.from_json(r[0]) for r in rows]

    def clear_pending(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self.transaction():
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.conn.execute(
                    f"DELETE FROM pending WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk)

    # ── Streaming dedup state ─────────────────────────────
    def has_fingerprint(self, fp: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM fingerprints WHERE hash=?", [fp]).fetchone() is not None

    def add_fingerprint(self, fp: str, sid: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO fingerprints VALUES (?, ?)", [fp, sid])

    def fingerprint_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM fingerprints").fetchone()[0]

    def save_sig(self, sid: str, sig: list) -> None:
        # uint64 signatures are stored as JSON strings to avoid float precision loss
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO minhash_sigs VALUES (?, ?)",
                [sid, json.dumps([int(x) for x in sig])])

    def load_sigs(self) -> list:
        rows = self.conn.execute(
            "SELECT sample_id, sig FROM minhash_sigs").fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    # ── Score cache (per endpoint; partial judge results survive too) ──
    def save_score(self, sid: str, endpoint: str, scores: dict, total: float) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?)",
                [sid, endpoint, json.dumps(scores, ensure_ascii=False), float(total)])

    def load_score(self, sid: str, endpoint: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT scores, total FROM scores WHERE id=? AND endpoint=?",
            [sid, endpoint]).fetchone()
        if not row:
            return None
        return {"scores": json.loads(row[0]), "total": row[1]}

    # ── Embedding content-addressed cache ─────────────────
    def load_embeddings(self, text_hashes: list, model: str) -> dict:
        if not text_hashes:
            return {}
        out = {}
        for i in range(0, len(text_hashes), 500):
            chunk = text_hashes[i:i + 500]
            rows = self.conn.execute(
                f"SELECT text_hash, vec FROM embeddings "
                f"WHERE model=? AND text_hash IN ({','.join('?' * len(chunk))})",
                [model, *chunk]).fetchall()
            out.update({h: v for h, v in rows})
        return out

    def save_embeddings(self, text_hash_model_vec: list) -> None:
        """[(text_hash, model, vec), ...]"""
        if not text_hash_model_vec:
            return
        with self.transaction():
            for h, m, v in text_hash_model_vec:
                self.conn.execute(
                    "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?)", [h, m, list(v)])

    # ── Terminal writes ───────────────────────────────────
    def commit_sample(self, sample: Sample, rendered: dict,
                      total_score: float = 0.0) -> None:
        """Transaction: projection + committed event + pending cleanup (atomic)."""
        with self.transaction():
            self.conn.execute(
                f"INSERT OR REPLACE INTO {self.table} VALUES (?, ?, ?, ?, ?, now())",
                [sample.id, sample.strategy, sample.to_json(),
                 json.dumps(rendered, ensure_ascii=False), float(total_score)])
            self.event("committed", sample.id, sample.strategy)
            self.conn.execute("DELETE FROM pending WHERE id=?", [sample.id])

    def drop_sample(self, sid: str, strategy: str, stage: str, reason: str) -> None:
        """Transaction: dropped record + event + pending cleanup (atomic).

        Note: fingerprints/signatures recorded by earlier dedup stages are
        deliberately kept — otherwise this sample's duplicates would slip
        through on resume (the output is a projection, not the state).
        """
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO dropped VALUES (?, ?, ?, ?)",
                [sid, strategy, stage, reason])
            self.event("dropped", sid, strategy, {"stage": stage, "reason": reason})
            self.conn.execute("DELETE FROM pending WHERE id=?", [sid])

    # ── Reading the projection ────────────────────────────
    def read_samples(self) -> list:
        rows = self.conn.execute(
            f"SELECT payload FROM {self.table} ORDER BY created_at, id").fetchall()
        return [Sample.from_json(r[0]) for r in rows]

    def read_rendered(self) -> list:
        rows = self.conn.execute(
            f"SELECT rendered FROM {self.table} ORDER BY created_at, id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def export_parquet(self, path: str) -> int:
        """Flatten the canonical payload into a columnar Parquet file
        (DuckDB native COPY). Returns the number of rows written."""
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn.execute(f"""
            COPY (
                SELECT id, strategy,
                       json_extract_string(payload, '$.instruction') AS instruction,
                       json_extract_string(payload, '$.output') AS output,
                       json_extract_string(payload, '$.reasoning') AS reasoning,
                       json_extract_string(payload, '$.messages') AS messages,
                       json_extract_string(payload, '$.tools') AS tools,
                       json_extract_string(payload, '$.metadata') AS metadata,
                       total_score
                FROM {self.table}
                ORDER BY created_at, id
            ) TO ? (FORMAT PARQUET)""", [path])
        return self.conn.execute(
            f"SELECT count(*) FROM read_parquet(?)", [path]).fetchone()[0]

    def sample_count(self) -> int:
        return self.conn.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]

    def read_dropped(self) -> list:
        return self.conn.execute(
            "SELECT id, strategy, stage, reason FROM dropped").fetchall()

    # ── Cleanup ───────────────────────────────────────────
    def cache_cleanup(self) -> None:
        """On success, clear only transient tables (audit events, in-flight
        pending). Terminal sets and dedup state persist so later runs are
        exact: no duplicate samples, no re-spent LLM calls
        (checkpoint-design.md §4)."""
        with self.transaction():
            for t in _PROGRESS_TABLES:
                self.conn.execute(f"DELETE FROM {t}")

    def stats(self) -> dict:
        out: dict[str, Any] = {"table": self.table}
        for t in [self.table, *_PROGRESS_TABLES, "embeddings", "scores"]:
            out[t] = self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        return out
