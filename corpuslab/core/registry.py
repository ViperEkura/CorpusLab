"""Registry (S3): decorator registration + entry-point autodiscovery.

Built-in plugins register via decorators, so they work even when entry points
are missing (no `pip install`); third-party packages inject via entry points
without touching corpuslab source.
"""

from __future__ import annotations
__all__ = [
    "register",
    "register_strategy",
    "register_stage",
    "register_judge",
    "register_source",
    "register_renderer",
    "register_sink",
    "lookup",
    "available",
    "import_builtin_modules",
]

import importlib
import logging
from typing import Dict

log = logging.getLogger("corpuslab.registry")

_REGISTRIES: Dict[str, Dict[str, type]] = {
    "strategies": {},
    "stages": {},
    "sources": {},
    "judges": {},
    "renderers": {},
    "sinks": {},
}
_LOADED_ENTRY_POINTS = False


def _get(group: str) -> Dict[str, type]:
    global _LOADED_ENTRY_POINTS
    if not _LOADED_ENTRY_POINTS:
        _LOADED_ENTRY_POINTS = True
        _load_entry_points()
    return _REGISTRIES[group]


def _load_entry_points() -> None:
    try:
        from importlib.metadata import entry_points
    except Exception:                                   # pragma: no cover
        return
    try:
        eps = entry_points()
        for group, reg in _REGISTRIES.items():
            selected = eps.select(group=f"corpuslab.{group}") if hasattr(eps, "select") \
                else eps.get(f"corpuslab.{group}", [])
            for ep in selected:
                try:
                    if ep.name not in reg:              # built-ins take precedence
                        reg[ep.name] = ep.load()
                except Exception as e:                  # pragma: no cover
                    log.warning("failed to load entry point %s.%s: %s", group, ep.name, e)
    except Exception as e:                              # pragma: no cover
        log.warning("entry point discovery failed: %s", e)


def register(group: str, name: str):
    def deco(obj):
        _REGISTRIES[group][name] = obj
        return obj
    return deco


def register_strategy(name: str):
    return register("strategies", name)


def register_stage(name: str, scheduling: str = "streaming"):
    """The scheduling declaration is documentation only; the real scheduling
    form is decided by which protocol method the stage implements."""

    def deco(obj):
        obj.scheduling = scheduling
        _REGISTRIES["stages"][name] = obj
        return obj
    return deco


def register_judge(name: str):
    return register("judges", name)


def register_source(name: str):
    return register("sources", name)


def register_renderer(name: str):
    return register("renderers", name)


def register_sink(name: str):
    return register("sinks", name)


def lookup(group: str, name: str) -> type:
    reg = _get(group)
    if name not in reg:
        raise KeyError(f"unregistered {group} plugin: {name!r} (available: {sorted(reg)})")
    return reg[name]


def available(group: str) -> Dict[str, type]:
    return dict(_get(group))


def import_builtin_modules() -> None:
    """Import built-in plugin modules to trigger decorator registration (idempotent)."""
    for mod in (
        "corpuslab.strategies.topic_driven",
        "corpuslab.strategies.deep_thinking",
        "corpuslab.strategies.seed_driven",
        "corpuslab.strategies.evol_instruct",
        "corpuslab.strategies.document_qa",
        "corpuslab.strategies.backtranslation",
        "corpuslab.strategies.tool_call",
        "corpuslab.stages.length",
        "corpuslab.stages.exact_dedup",
        "corpuslab.stages.stats",
        "corpuslab.stages.minhash",
        "corpuslab.stages.semantic",
        "corpuslab.sources.file",
        "corpuslab.judges.llm_judge",
        "corpuslab.judges.aggregate",
        "corpuslab.sinks.duckdb_sink",
        "corpuslab.sinks.jsonl_sink",
    ):
        try:
            importlib.import_module(mod)
        except Exception as e:                          # pragma: no cover
            log.warning("failed to register built-in module %s: %s", mod, e)
