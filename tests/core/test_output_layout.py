"""Output layout tests: dir mode (duckdb + parquet), single-file compat mode,
jsonl mode."""
from __future__ import annotations

import json

import pytest

from corpuslab.config import schema as S
from corpuslab.config.loader import layout_for_path, output_layout
from corpuslab import engine
from corpuslab.core.store import Store
from tests.conftest import make_config


def test_layout_modes():
    st = S.StorageCfg()
    d = layout_for_path("./out", st)
    assert d["dir_mode"] is True
    assert d["db_path"] == "./out/corpuslab.duckdb"
    assert d["parquet_path"] == "./out/samples.parquet"
    assert d["jsonl_path"] == "./out/samples.jsonl"

    f = layout_for_path("./out.duckdb", st)
    assert f["dir_mode"] is False
    assert f["db_path"] == "./out.duckdb"
    assert f["parquet_path"] is None

    j = layout_for_path("./out.jsonl", st)
    assert j["jsonl_path"] == "./out.jsonl"

    no_pq = layout_for_path("./out", S.StorageCfg(export_parquet=False))
    assert no_pq["parquet_path"] is None

    rel = layout_for_path("./out", S.StorageCfg(export_jsonl="extra.jsonl"))
    assert rel["jsonl_path"] == "./out/extra.jsonl"


@pytest.mark.asyncio
async def test_dir_mode_produces_duckdb_and_parquet(tmp_path):
    out = tmp_path / "run_out"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.plan.count = 6
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)

    # State store + parquet export exist
    assert (out / "corpuslab.duckdb").exists()
    assert (out / "samples.parquet").exists()

    # Parquet roundtrip: readable, same row count, flattenable columns
    store = Store(str(out / "corpuslab.duckdb"))
    n = store.sample_count()
    store.close()
    import duckdb
    con = duckdb.connect()
    rows = con.execute("SELECT id, instruction, output, total_score "
                       "FROM 'samples.parquet'".replace(
                           "'samples.parquet'",
                           f"'{out / 'samples.parquet'}'")).fetchall()
    assert len(rows) == n
    assert all(r[1] for r in rows)              # instruction column populated
    con.close()


@pytest.mark.asyncio
async def test_dir_mode_jsonl_export(tmp_path):
    out = tmp_path / "run_out"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.output.storage.export_jsonl = "rows.jsonl"
    cfg.plan.count = 4
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)
    path = out / "rows.jsonl"
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) > 0
    assert "instruction" in lines[0]


@pytest.mark.asyncio
async def test_single_file_compat_mode_unchanged(tmp_path):
    """Old configs pointing at xxx.duckdb keep single-file behavior."""
    out = tmp_path / "out.duckdb"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.plan.count = 4
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)
    assert out.exists()
    assert not (tmp_path / "samples.parquet").exists()
