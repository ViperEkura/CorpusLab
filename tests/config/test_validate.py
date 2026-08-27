"""Validate tests: conflicts, chain legality, resource existence,
per-subcommand required sets (docs/project-structure.md §9)."""
from __future__ import annotations

from corpuslab.config import validate as vld
from tests.conftest import make_config


def test_validate_run_requires(tmp_path):
    cfg = make_config(tmp_path)
    issues = vld.check(cfg, "run")
    assert not vld.has_errors(issues)
    cfg.strategies = []
    issues = vld.check(cfg, "run")
    assert any("strategies" in m for lv, m in issues if lv == "error")


def test_validate_clean_does_not_require_strategies(tmp_path):
    cfg = make_config(tmp_path)
    inp = tmp_path / "in.jsonl"
    inp.write_text('{"instruction": "q", "output": "a"}', encoding="utf-8")
    issues = vld.check(cfg, "clean", input_path=str(inp))
    assert not vld.has_errors(issues)
    # But missing input is an error
    issues = vld.check(cfg, "clean", input_path=str(tmp_path / "nope.jsonl"))
    assert vld.has_errors(issues)


def test_validate_score_needs_judge(tmp_path):
    cfg = make_config(tmp_path)
    cfg.judge.dimensions = []
    cfg.judge.scorers = []
    issues = vld.check(cfg, "score")
    assert any("judge" in m for lv, m in issues if lv == "error")


def test_validate_undeclared_endpoint_ref(tmp_path):
    cfg = make_config(tmp_path)
    cfg.judge.endpoint = "ghost"
    issues = vld.check(cfg, "run")
    assert any("ghost" in m for lv, m in issues if lv == "error")


def test_validate_batch_then_streaming_warns(tmp_path):
    cfg = make_config(tmp_path, pipeline_yaml=[
        {"type": "semantic_dedup", "threshold": 0.85},
        {"type": "stats"},                            # streaming after batch
    ])
    issues = vld.check(cfg, "run")
    assert any("streaming benefits are lost" in m for lv, m in issues
               if lv == "warning")


def test_yield_errors(tmp_path):
    cfg = make_config(tmp_path)
    cfg.plan.count = None
    issues = vld.check(cfg, "run")
    assert any("plan allocation" in m for lv, m in issues if lv == "error")


def test_resource_existence_seed_file(tmp_path):
    cfg = make_config(tmp_path, strategies_yaml=[
        {"type": "seed_driven", "seed_file": str(tmp_path / "missing.jsonl")},
    ])
    issues = vld.check(cfg, "run")
    assert any("does not exist" in m and "seed_file" in m
               for lv, m in issues if lv == "error")


def test_weight_sum_warning(tmp_path):
    cfg = make_config(tmp_path)
    for s in cfg.strategies:
        s.weight = 2.0                                # sum 4 → normalized
    issues = vld.check(cfg, "run")
    assert any("auto-normalized" in m for lv, m in issues if lv == "warning")
