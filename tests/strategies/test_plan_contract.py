"""Contract test: every strategy's plan yields unique deterministic ids —
the idempotency foundation of the checkpoint (duplicate ids get silently
swallowed by INSERT OR IGNORE and break resume exactness)."""
from __future__ import annotations

import asyncio

import pytest

from corpuslab.config.loader import load as load_config
from corpuslab.core.context import RunContext
from corpuslab.core.registry import import_builtin_modules, lookup
from tests.conftest import make_config

import_builtin_modules()


async def _collect_ids(strategy_cfg, budget, ctx):
    strat = lookup("strategies", strategy_cfg.type)(strategy_cfg)

    async def noop():
        if False:                              # pragma: no cover
            yield None

    return [s.id async for s in strat.plan(noop(), budget, ctx)]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [1, 7, 23])
async def test_plan_ids_unique(tmp_path, budget):
    tools = [{
        "type": "function",
        "function": {"name": "get_weather", "description": "Get weather",
                     "parameters": {"type": "object", "properties": {}}},
    }]
    cfg = make_config(tmp_path, strategies_yaml=[
        {"type": "topic_driven", "topics": [{"topic": "t1"}, {"topic": "t2"}]},
        {"type": "deep_thinking", "topics": [{"topic": "t1"}]},
        {"type": "seed_driven", "seed_file": str(tmp_path / "seeds.jsonl")},
        {"type": "evol_instruct", "seed_file": str(tmp_path / "seeds.jsonl"),
         "max_rounds": 3, "branch_factor": 2},
        {"type": "document_qa", "document_file": str(tmp_path / "documents.jsonl")},
        {"type": "instruction_backtranslation",
         "document_file": str(tmp_path / "documents.jsonl")},
        {"type": "tool_call", "tools": tools},
    ])
    ctx = RunContext(cfg, store=None, llm=None)
    for scfg in cfg.strategies:
        ids = await _collect_ids(scfg, budget, ctx)
        assert len(ids) == len(set(ids)), (
            f"{scfg.type}: {len(ids)} specs with {len(ids) - len(set(ids))} "
            f"duplicate ids → resume would silently drop them")


@pytest.mark.asyncio
async def test_plan_deterministic_same_seed(tmp_path):
    cfg = make_config(tmp_path)
    ctx_a = RunContext(cfg, store=None, llm=None)
    ctx_b = RunContext(cfg, store=None, llm=None)
    for scfg in cfg.strategies:
        ids_a = await _collect_ids(scfg, 11, ctx_a)
        ids_b = await _collect_ids(scfg, 11, ctx_b)
        assert ids_a == ids_b, f"{scfg.type}: plan is not seed-deterministic"
