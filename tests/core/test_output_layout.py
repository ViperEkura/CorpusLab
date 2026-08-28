"""Output layout tests: dir mode (duckdb + parquet), single-file compat mode,
jsonl mode."""
from __future__ import annotations

import json

import pytest

from corpuslab.config import schema as S
from corpuslab.config.loader import layout_for_path
from corpuslab import engine
from corpuslab.core.store import Store
from tests.conftest import make_config


def test_layout_modes():
    st = S.StorageCfg()
    d = layout_for_path("./out", st)
    assert d["dir_mode"] is True
    assert d["db_path"] == "./out/corpuslab.duckdb"
    assert d["export_path"] == "./out/samples.parquet"   # default: parquet

    f = layout_for_path("./out.duckdb", st)
    assert f["dir_mode"] is False
    assert f["db_path"] == "./out.duckdb"
    assert f["export_path"] is not None                 # lands next to the db

    j = layout_for_path("./out.jsonl", st)
    assert j["export_path"] is None                     # plain-file mode: path IS jsonl

    off = layout_for_path("./out", S.StorageCfg(export_format=None))
    assert off["export_path"] is None                   # state store only

    jl = layout_for_path("./out", S.StorageCfg(export_format="jsonl"))
    assert jl["export_path"] == "./out/samples.jsonl"

    cust = layout_for_path("./out", S.StorageCfg(export_format="jsonl", table="rows"))
    assert cust["export_path"] == "./out/rows.jsonl"


@pytest.mark.asyncio
async def test_dir_mode_produces_duckdb_and_parquet(tmp_path):
    out = tmp_path / "run_out"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.plan.count = 6
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)

    # State store + parquet export exist; jsonl not exported unless configured
    assert (out / "corpuslab.duckdb").exists()
    assert (out / "samples.parquet").exists()
    assert not (out / "samples.jsonl").exists()

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
    cfg.output.storage.export_format = "jsonl"
    cfg.plan.count = 4
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)
    path = out / "samples.jsonl"                        # <dir>/<table>.jsonl
    assert path.exists()
    assert not (out / "samples.parquet").exists()       # one format at a time
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) > 0
    assert "instruction" in lines[0]


@pytest.mark.asyncio
async def test_dir_mode_export_disabled(tmp_path):
    out = tmp_path / "run_out"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.output.storage.export_format = None
    cfg.plan.count = 4
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)
    assert (out / "corpuslab.duckdb").exists()
    assert not (out / "samples.parquet").exists()
    assert not (out / "samples.jsonl").exists()


@pytest.mark.asyncio
async def test_single_file_compat_mode_unchanged(tmp_path):
    """Old configs pointing at xxx.duckdb keep single-file db semantics;
    the export file (default parquet) lands next to the db."""
    out = tmp_path / "out.duckdb"
    cfg = make_config(tmp_path, output_path=str(out))
    cfg.plan.count = 4
    cfg.judge.min_total = 0
    await engine.run_flow(cfg)
    assert out.exists()
    assert (tmp_path / "samples.parquet").exists()
