"""Store tests: transaction atomicity, idempotent upserts, cleanup tiers."""
from __future__ import annotations


import pytest

from corpuslab.core.store import Store
from tests.conftest import make_sample


def test_commit_is_atomic_and_idempotent(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    s = make_sample(1)
    store.commit_sample(s, {"instruction": "x"}, total_score=3.5)
    # Re-commit the same id: upsert, not duplicate
    store.commit_sample(s, {"instruction": "x2"}, total_score=4.0)
    assert store.sample_count() == 1
    assert store.read_rendered()[0] == {"instruction": "x2"}
    # Event log recorded both commits
    n = store.conn.execute(
        "SELECT count(*) FROM events WHERE t='committed'").fetchone()[0]
    assert n == 2
    store.close()


def test_drop_keeps_fingerprints_but_clears_pending(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    s = make_sample(2)
    store.save_pending(s)
    store.add_fingerprint(s.fingerprint(), s.id)
    assert store.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1

    store.drop_sample(s.id, s.strategy, "stats", "ngram_diversity")
    # Pending cleared (terminal), fingerprint kept (dedup state must survive)
    assert store.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert store.has_fingerprint(s.fingerprint())
    assert s.id in store.dropped_ids()
    assert store.terminal_ids() == {s.id}
    store.close()


def test_minhash_sig_roundtrip_preserves_uint64(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    sig = [2**64 - 1, 0, 12345678901234567890, 42]
    store.save_sig("sample-1", sig)
    loaded = store.load_sigs()
    assert loaded == [("sample-1", [2**64 - 1, 0, 12345678901234567890, 42])]
    store.close()


def test_score_cache_per_endpoint(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    store.save_score("id-1", "pro", {"correctness": 8}, 8.0)
    store.save_score("id-1", "flash", {"correctness": 6}, 6.0)
    assert store.load_score("id-1", "pro")["scores"]["correctness"] == 8
    assert store.load_score("id-1", "flash")["scores"]["correctness"] == 6
    assert store.load_score("id-1", "other") is None
    store.close()


def test_cache_cleanup_tiers(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    s = make_sample(3)
    store.commit_sample(s, {"k": 1})
    store.add_fingerprint(s.fingerprint(), s.id)
    store.save_sig(s.id, [1, 2, 3])
    store.save_embeddings([("h1", "m1", [0.1, 0.2])])
    store.event("planned", s.id)
    store.drop_sample(make_sample(4).id, "topic_driven", "stats", "x")

    store.cache_cleanup()

    assert store.sample_count() == 1                    # kept
    assert store.conn.execute(                          # kept (cross-run asset)
        "SELECT count(*) FROM embeddings").fetchone()[0] == 1
    assert store.fingerprint_count() == 1               # kept (dedup history)
    assert store.conn.execute(
        "SELECT count(*) FROM minhash_sigs").fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT count(*) FROM dropped").fetchone()[0] == 1      # terminal history
    assert store.conn.execute(
        "SELECT count(*) FROM events").fetchone()[0] == 0       # transient cleared
    assert store.conn.execute(
        "SELECT count(*) FROM pending").fetchone()[0] == 0
    store.close()


def test_transaction_rollback(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.conn.execute("INSERT INTO kv VALUES ('k', 'v')")
            raise RuntimeError("boom")
    assert store.get_kv("k") is None
    store.close()
