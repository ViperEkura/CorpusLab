"""Schema tests: unknown keys rejected, legacy alias hints, required
constraints (docs/project-structure.md §9)."""
from __future__ import annotations

import pytest
import yaml

from corpuslab.config.loader import ConfigError, load


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


def test_required_topic_topics_list(tmp_path):
    cfg_path = tmp_path / "need-topics.yaml"
    cfg_path.write_text(yaml.dump({
        "llm": {"model": "m"},
        "strategies": [{"type": "topic_driven"}],
        "output": {"path": str(tmp_path / "o.duckdb")},
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="topics"):
        load(str(cfg_path))


def test_required_seed_file(tmp_path):
    cfg_path = tmp_path / "need-seed.yaml"
    cfg_path.write_text(yaml.dump({
        "llm": {"model": "m"},
        "strategies": [{"type": "seed_driven"}],
        "output": {"path": str(tmp_path / "o.duckdb")},
    }), encoding="utf-8")
    with pytest.raises(ConfigError):
        load(str(cfg_path))


def test_ratio_bounds_validator(tmp_path):
    cfg_path = tmp_path / "bad-bounds.yaml"
    cfg_path.write_text(yaml.dump({
        "llm": {"model": "m"},
        "plan": {"count": 4},
        "strategies": [{"type": "evol_instruct",
                        "seed_file": "s.jsonl",
                        "ratio_bounds": [5.0, 0.5]}],
        "output": {"path": str(tmp_path / "o.duckdb")},
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="ratio_bounds"):
        load(str(cfg_path))
