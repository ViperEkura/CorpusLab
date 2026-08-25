"""Plan allocation (P5): weight-normalized split, remainder, count override,
--strategy re-split.

Rules (config-design.md §10.3; this module is the normative implementation):
    Priority: CLI --count > plan.count > Σ explicit strategy count
    plan.count present:
        weights auto-normalized; remainder goes to the heaviest weight;
        explicit count overrides that strategy's share
        explicit counts summing above plan.count → error
    plan.count absent:
        sum of explicit counts; any strategy without count → error (the
        message names the strategy)
"""
from __future__ import annotations

from typing import List, Optional, Tuple


class PlanError(ValueError):
    pass


def allocate(strategy_cfgs: list, plan_count: Optional[int],
             cli_count: Optional[int] = None,
             only: Optional[List[str]] = None) -> List[Tuple[Any, int]]:
    """Return [(strategy_cfg, count), ...]; `only` is the --strategy filter."""
    total = cli_count if cli_count is not None else plan_count

    cfgs = [c for c in strategy_cfgs
            if only is None or c.type in only]
    if not cfgs:
        raise PlanError("no strategies participate (check strategies and the "
                        "--strategy filter)")

    if total is not None:
        explicit = [(c, c.count) for c in cfgs if c.count is not None]
        explicit_sum = sum(n for _, n in explicit)
        if explicit_sum > total:
            names = ", ".join(f"{c.type}={n}" for c, n in explicit)
            raise PlanError(
                f"explicit counts sum ({explicit_sum}) exceeds the plan ({total}): "
                f"{names}; this would silently over-produce — adjust plan.count "
                f"or strategy count")
        remaining = total - explicit_sum
        flexible = [c for c in cfgs if c.count is None]
        alloc = {id(c): (c.count or 0) for c in cfgs}
        if flexible:
            wsum = sum(max(c.weight, 0.0) for c in flexible) or 1.0
            shares = []
            for c in flexible:
                share = int(remaining * max(c.weight, 0.0) / wsum)
                shares.append((c, share))
            # Remainder goes to the heaviest weight (ties: first declared)
            rest = remaining - sum(s for _, s in shares)
            if rest and flexible:
                heaviest = max(flexible, key=lambda c: (c.weight, -flexible.index(c)))
                for i, (c, s) in enumerate(shares):
                    if c is heaviest:
                        shares[i] = (c, s + rest)
                        break
            for c, s in shares:
                alloc[id(c)] = s
        out = [(c, alloc[id(c)]) for c in cfgs]
    else:
        missing = [c.type for c in cfgs if c.count is None]
        if missing:
            raise PlanError(
                "when plan.count is absent every strategy needs an explicit "
                f"count; missing: {', '.join(missing)}")
        out = [(c, c.count) for c in cfgs]

    return [(c, max(n, 0)) for c, n in out if n > 0] or ([(cfgs[0], 0)] if cfgs else [])
