"""Planner tests: weight normalization, remainder, count override,
over-allocation guard, --strategy re-split."""
from __future__ import annotations

import pytest

from corpuslab.core import planner
from tests.conftest import make_config


def _types(alloc):
    return {c.type: n for c, n in alloc}


def test_weight_split_with_remainder(tmp_path):
    cfg = make_config(tmp_path)
    alloc = planner.allocate(cfg.strategies, 10)
    counts = _types(alloc)
    assert sum(counts.values()) == 10
    assert set(counts) == {"topic_driven", "seed_driven"}


def test_weights_auto_normalized(tmp_path):
    cfg = make_config(tmp_path)
    # weights 0.5/0.5 sum to 1; now try with a config where they don't
    strategies = [
        {"type": "topic_driven", "weight": 2, "topics": [{"topic": "t1"}]},
        {"type": "seed_driven", "weight": 1, "seed_file": "x"},
    ]
    # patch strategy cfgs directly (weights 2:1 → 2/3 : 1/3)
    cfg.strategies[0].weight = 2.0
    cfg.strategies[1].weight = 1.0
    cfg.strategies[0].__dict__.update(topics=strategies[0]["topics"]) if False else None
    counts = _types(planner.allocate(cfg.strategies, 9))
    assert sum(counts.values()) == 9
    assert counts["topic_driven"] == 6            # 9 * 2/3
    assert counts["seed_driven"] == 3             # 9 * 1/3


def test_explicit_count_overrides_share(tmp_path):
    cfg = make_config(tmp_path)
    cfg.strategies[0].count = 4                   # explicit override
    counts = _types(planner.allocate(cfg.strategies, 10))
    assert counts["topic_driven"] == 4
    assert counts["seed_driven"] == 6             # remaining re-split


def test_explicit_over_total_is_error(tmp_path):
    cfg = make_config(tmp_path)
    cfg.strategies[0].count = 7
    cfg.strategies[1].count = 7
    with pytest.raises(planner.PlanError, match="exceeds"):
        planner.allocate(cfg.strategies, 10)


def test_missing_plan_count_requires_explicit(tmp_path):
    cfg = make_config(tmp_path)
    with pytest.raises(planner.PlanError, match="missing"):
        planner.allocate(cfg.strategies, None)
    cfg.strategies[0].count = 3
    cfg.strategies[1].count = 5
    counts = _types(planner.allocate(cfg.strategies, None))
    assert counts == {"topic_driven": 3, "seed_driven": 5}


def test_strategy_filter_reallocates(tmp_path):
    cfg = make_config(tmp_path)
    counts = _types(planner.allocate(cfg.strategies, 10, only=["seed_driven"]))
    assert counts == {"seed_driven": 10}
    with pytest.raises(planner.PlanError):
        planner.allocate(cfg.strategies, 10, only=["nope"])
