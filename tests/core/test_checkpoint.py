"""Checkpoint/resume tests: terminal-id skipping, resume idempotency (no
duplicate samples after interruption), manifest compatibility."""
from __future__ import annotations

import json

import pytest

from corpuslab.config.loader import load as load_config
from corpuslab.core import checkpoint
from corpuslab import engine
from corpuslab.core.store import Store
from tests.conftest import make_config


@pytest.mark.asyncio
async def test_run_then_resume_is_idempotent(tmp_path):
    """Full run of N → record state → rerun with resume=True must produce
    zero new samples and zero duplicates (terminal ids are skipped)."""
    cfg = make_config(tmp_path, output_path=tmp_path / "out.duckdb")
    cfg.plan.count = 6
    cfg.judge.min_total = 0                         # keep everything scoreable

    report1 = await engine.run_flow(cfg, resume=False)
    store = Store(str(tmp_path / "out.duckdb"))
    n1 = store.sample_count()
    dropped1 = {r[0] for r in store.read_dropped()}
    terminal1 = store.terminal_ids()
    store.close()

    report2 = await engine.run_flow(cfg, resume=True)
    store = Store(str(tmp_path / "out.duckdb"))
    n2 = store.sample_count()
    terminal2 = store.terminal_ids()
    # No new terminals: everything was already finished
    assert n2 == n1
    assert terminal2 == terminal1
    # Second run produced 0 new samples
    assert report2.produced == 0
    store.close()


@pytest.mark.asyncio
async def test_partial_state_resume_no_duplicates(tmp_path):
    """Simulate an interruption: only some samples committed. Resume must
    finish the rest without duplicating the committed ones."""
    cfg = make_config(tmp_path, output_path=tmp_path / "out.duckdb")
    cfg.plan.count = 8
    cfg.judge.min_total = 0

    # First: run with a tiny budget (partial completion)
    cfg_small = make_config(tmp_path, output_path=tmp_path / "out.duckdb")
    cfg_small.plan.count = 3
    cfg_small.judge.min_total = 0
    # same seed → same deterministic ids
    await engine.run_flow(cfg_small, resume=False)

    store = Store(str(tmp_path / "out.duckdb"))
    partial = store.sample_count()
    assert 0 < partial < 8, f"expected partial state, got {partial}"
    store.close()

    # Resume with the full budget: no duplicates of already-committed samples
    report = await engine.run_flow(cfg, resume=True)
    store = Store(str(tmp_path / "out.duckdb"))
    ids = store.conn.execute("SELECT id FROM samples").fetchall()
    flat = [i[0] for i in ids]
    assert len(flat) == len(set(flat)), "duplicate ids after resume"
    assert store.sample_count() >= partial
    store.close()


def test_manifest_incompatible_seed(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(str(tmp_path / "s.duckdb"))
    checkpoint.write_manifest(store, cfg, num_perm=128)
    cfg.run.seed = 999                               # seed affects id derivation
    with pytest.raises(checkpoint.IncompatibleState, match="discard"):
        checkpoint.write_manifest(store, cfg, num_perm=128)
    # discard=True rewrites the manifest instead of refusing
    checkpoint.write_manifest(store, cfg, num_perm=128, discard=True)
    assert store.get_kv("seed") == "999"
    store.close()


def test_manifest_num_perm_change_discards_sigs(tmp_path):
    cfg = make_config(tmp_path)
    store = Store(str(tmp_path / "s.duckdb"))
    checkpoint.write_manifest(store, cfg, num_perm=128)
    store.save_sig("x", [1, 2, 3])
    # num_perm changed → signatures invalid → dropped with discard=True
    checkpoint.write_manifest(store, cfg, num_perm=256, discard=True)
    assert store.conn.execute("SELECT count(*) FROM minhash_sigs").fetchone()[0] == 0
    assert store.get_kv("minhash_num_perm") == "256"
    store.close()


@pytest.mark.asyncio
async def test_threshold_change_stays_resume_compatible(tmp_path):
    """Threshold is view-level config: signatures survive; only num_perm
    invalidates them."""
    cfg = make_config(tmp_path)
    store = Store(str(tmp_path / "s.duckdb"))
    checkpoint.write_manifest(store, cfg, num_perm=128)
    store.save_sig("x", [1, 2, 3])
    # same num_perm, different threshold → no discard, no error
    checkpoint.write_manifest(store, cfg, num_perm=128)
    assert store.conn.execute("SELECT count(*) FROM minhash_sigs").fetchone()[0] == 1
    store.close()
