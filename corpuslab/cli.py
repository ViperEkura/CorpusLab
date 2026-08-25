"""corpuslab CLI: run / clean / score / validate.

The CLI layer holds no business logic: parse → load → validate → assemble →
hand over to the engine (S1)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Optional

from corpuslab import engine
from corpuslab.config import validate as vld
from corpuslab.config.loader import ConfigError, load

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_BREAKER = 3
EXIT_MISSING_INPUT = 4

log = logging.getLogger("corpuslab.cli")


def _find_config(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for cand in ("./corpuslab.yaml", "./corpuslab.yml"):
        if os.path.exists(cand):
            return cand
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="corpuslab",
                                description="Declarative LLM training-data pipeline (SFT + pretraining corpora)"
                                            "(source → synthesize → clean → judge → DuckDB)")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="full pipeline: Source → Pipeline → Judge → Sink")
    pr.add_argument("-c", "--config")
    pr.add_argument("--count", type=int, help="override plan.count")
    pr.add_argument("--preview", action="store_true", help="small batch, no "
                                                           "state writes (still calls the LLM)")
    pr.add_argument("--resume", action="store_true", help="resume from the state store")
    pr.add_argument("--strategy", action="append", help="filter to these strategy types")
    pr.add_argument("--discard-state", action="store_true",
                    help="drop incompatible state instead of refusing")

    pc = sub.add_parser("clean", help="FileSource → Pipeline → Sink")
    pc.add_argument("input")
    pc.add_argument("-o", "--output")
    pc.add_argument("-c", "--config")
    pc.add_argument("--input-format", default="flat",
                    choices=["flat", "alpaca", "chatml", "sharegpt", "openai"])
    pc.add_argument("--field-map", help="JSON mapping of foreign → canonical fields")

    ps = sub.add_parser("score", help="FileSource → Judge → Sink")
    ps.add_argument("input")
    ps.add_argument("-o", "--output")
    ps.add_argument("-c", "--config")
    ps.add_argument("--input-format", default="flat",
                    choices=["flat", "alpaca", "chatml", "sharegpt", "openai"])
    ps.add_argument("--field-map", help="JSON mapping of foreign → canonical fields")

    pv = sub.add_parser("validate", help="static config validation")
    pv.add_argument("-c", "--config")
    pv.add_argument("mode", nargs="?", default="run",
                    choices=["run", "clean", "score"])
    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    path = _find_config(args.config)
    if path is None:
        print("config not found: use -c or place ./corpuslab.yaml",
              file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load(path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    if args.command == "validate":
        issues = vld.check(cfg, args.mode)
        errs = 0
        for level, msg in issues:
            print(f"[{level}] {msg}")
            errs += level == "error"
        print(f"validate: {len(issues)} issue(s), {errs} error(s) → "
              f"{'FAIL' if errs else 'OK'}")
        return EXIT_CONFIG if errs else EXIT_OK

    input_path = getattr(args, "input", None)
    issues = vld.check(cfg, args.command, input_path=input_path)
    errors = [m for lv, m in issues if lv == "error"]
    if errors:
        for m in errors:
            print(f"[error] {m}", file=sys.stderr)
        for lv, m in issues:
            if lv == "warning":
                print(f"[warning] {m}", file=sys.stderr)
        if any("does not exist" in m for m in errors):
            return EXIT_MISSING_INPUT
        return EXIT_CONFIG
    for lv, m in issues:
        if lv == "warning":
            print(f"[warning] {m}", file=sys.stderr)

    field_map = None
    if getattr(args, "field_map", None):
        try:
            field_map = json.loads(args.field_map)
        except json.JSONDecodeError as e:
            print(f"--field-map is not valid JSON: {e}", file=sys.stderr)
            return EXIT_CONFIG

    from corpuslab.core.registry import import_builtin_modules
    import_builtin_modules()

    try:
        if args.command == "run":
            report = asyncio.run(engine.run_flow(
                cfg, cli_count=args.count, only=args.strategy,
                preview=args.preview, resume=args.resume,
                discard_state=args.discard_state))
        elif args.command == "clean":
            report = asyncio.run(engine.clean_flow(
                cfg, args.input, args.output, args.input_format, field_map,
                resume=False))
        else:
            report = asyncio.run(engine.score_flow(
                cfg, args.input, args.output, args.input_format, field_map))
    except engine.CircuitBreakerOpen:
        print("run aborted: circuit breaker open (state store retained)",
              file=sys.stderr)
        return EXIT_BREAKER
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    print(report.summary())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
