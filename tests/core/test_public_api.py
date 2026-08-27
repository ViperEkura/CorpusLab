"""Public extension surface: __all__ whitelists (docs/project-structure.md §11)."""
from __future__ import annotations



def test_registry_star_import():
    ns = {}
    exec("from corpuslab.core.registry import *", ns)
    for name in ("register_strategy", "register_stage", "register_source",
                 "register_judge", "register_renderer", "register_sink",
                 "lookup", "available"):
        assert name in ns, f"registry.__all__ missing {name}"


def test_contracts_star_import():
    ns = {}
    exec("from corpuslab.core.contracts import *", ns)
    for name in ("Material", "Source", "Strategy", "StreamingStage",
                 "BatchStage", "Judge", "Sink", "RunReport"):
        assert name in ns


def test_sample_star_import():
    ns = {}
    exec("from corpuslab.core.sample import *", ns)
    for name in ("TaskSpec", "Sample", "Score", "RunReport", "derive_id",
                 "FORMAT_VERSION"):
        assert name in ns


def test_strategy_base_star_import():
    ns = {}
    exec("from corpuslab.strategies.base import *", ns)
    assert "PlanExecuteStrategy" in ns
