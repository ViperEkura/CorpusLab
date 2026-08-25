"""Config tests: unknown keys, alias hints, format derivation, per-subcommand
validation."""
from __future__ import annotations

import json

import pytest
import yaml

from corpuslab.config import validate as vld
from corpuslab.config.loader import ConfigError, derive_format, load
from tests.conftest import make_config


def test_unknown_key_rejected(tmp_path):
    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text(yaml.dump({
        "llm": {"model": "m", "totally_unknown": 1},
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="totally_unknown"):
        load(str(cfg_path))


def test_legacy_alias_hint(tmp_path):
    cfg_path = tmp_path / "alias.yaml"
    cfg_path.write_text(yaml.dump({
        "llm": {"model": "m"},
        "strategies": [
            {"type": "topic_driven", "topics": [{"topic": "t"}],
             "total_count": 5},                      # legacy alias
        ],
        "output": {"path": str(tmp_path / "o.duckdb")},
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="total_count"):
        load(str(cfg_path))


def test_format_derivation(tmp_path):
    cfg = make_config(tmp_path)                       # no tool_call → alpaca
    assert derive_format(cfg) == "alpaca"
    cfg.output.format = "chatml"
    assert derive_format(cfg) == "chatml"             # explicit wins


def test_format_derivation_tool_call(tmp_path):
    tools = [{
        "type": "function",
        "function": {"name": "get_weather",
                     "description": "Get weather",
                     "parameters": {"type": "object", "properties": {}}},
    }]
    cfg = make_config(tmp_path, strategies_yaml=[
        {"type": "tool_call", "weight": 1.0, "tools": tools},
    ])
    assert derive_format(cfg) == "openai"


def test_endpoint_resolution_merges_fields(tmp_path):
    cfg = make_config(tmp_path, extra_yaml=yaml.dump({
        "endpoints": {"pro": {"model": "m-pro", "concurrency": 4}},
    }) if False else "{}")
    # Direct construction (extra_yaml with endpoints needs pydantic shapes)
    from corpuslab.config.loader import resolve_endpoint
    import corpuslab.config.schema as S
    ep = S.LlmCfg(model="m-pro", concurrency=4)
    cfg.endpoints["pro"] = ep
    resolved = resolve_endpoint(cfg, "pro")
    assert resolved.model == "m-pro"                  # diff declared
    assert resolved.concurrency == 4                   # diff declared
    assert resolved.lang == cfg.llm.lang               # inherited from llm
    assert resolve_endpoint(cfg, "llm").model == "fake-model"


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
