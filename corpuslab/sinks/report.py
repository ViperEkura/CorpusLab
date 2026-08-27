"""report: run report formatting (project-structure.md §3.8) — drop reason
waterfall, score distribution, cost estimate. Produces the human-readable
summary the CLI prints; RunReport itself lives in core/sample.py."""
from __future__ import annotations

__all__ = ["format_summary", "format_score_distribution", "collect_totals"]

from typing import Any


def collect_totals(report: Any) -> list:
    """Total scores accumulated during the run (works for preview and store
    modes alike; sinks record them on the report)."""
    return getattr(report, "totals", None) or []


def format_score_distribution(report: Any, scores: Any = None) -> str:
    """Buckets of total_score over the committed projection. `scores` is the
    list of totals, provided by sinks after their run (preview passes [])."""
    if not scores:
        return "(no scored samples)"
    totals = [t for t in scores if t > 0]
    if not totals:
        return "(no scored samples)"
    n = len(totals)
    buckets = {"0-5": 0, "5-10": 0, "10-15": 0, "15-20": 0, "20+": 0}
    for t in totals:
        if t < 5:
            buckets["0-5"] += 1
        elif t < 10:
            buckets["5-10"] += 1
        elif t < 15:
            buckets["10-15"] += 1
        elif t < 20:
            buckets["15-20"] += 1
        else:
            buckets["20+"] += 1
    mean = sum(totals) / n
    parts = ", ".join(f"{k}: {v}" for k, v in buckets.items() if v)
    return f"scored={n} mean={mean:.1f} [{parts}]"


def format_cost_estimate(report: Any) -> str:
    """Rudimentary visibility: call and cache counts from the run."""
    return (f"llm_calls={report.llm_calls} retried={report.retried} "
            f"embed_calls={report.embed_calls} cache_hits={report.cache_hits}")


def format_summary(report: Any) -> str:
    """The CLI-facing summary: waterfall + scores + cost."""
    total_in = report.produced + report.drop_total()
    lines = [f"pipeline report: {total_in} in → {report.produced} out",
             report.waterfall(),
             format_score_distribution(report, collect_totals(report)),
             format_cost_estimate(report)]
    return "\n".join(lines)
