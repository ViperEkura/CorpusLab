"""Shared fixtures: fake clients, config factories, sample factory."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corpuslab.config.loader import load as load_config  # noqa: E402
from corpuslab.core.sample import Sample, derive_id  # noqa: E402
from corpuslab.testing import FakeEmbedding, FakeLLM  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    monkeypatch.setenv("CORPUSLAB_FAKE_LLM", "1")
    monkeypatch.setenv("CORPUSLAB_FAKE_EMBED", "1")


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def fake_embed():
    return FakeEmbedding()


def make_sample(n: int, *, instruction: str = None, output: str = None,
                strategy: str = "topic_driven") -> Sample:
    return Sample(
        id=derive_id("test", strategy, n),
        strategy=strategy,
        instruction=instruction or f"instruction number {n} with unique content {n * 7919}",
        output=output or f"answer to {n}: " + " ".join(f"tok{n}-{i}" for i in range(20)),
    )


def make_config(tmp_path, *, strategies_yaml=None, pipeline_yaml=None,
                judge_yaml=None, extra_yaml="", output_path=None):
    from corpuslab.config import schema as S  # noqa: F401
    import yaml

    seeds = tmp_path / "seeds.jsonl"
    seeds.write_text("\n".join(json.dumps({
        "id": f"seed:{i}",
        "instruction": f"seed instruction {i} " + " ".join(f"s{i}w{j}" for j in range(15)),
        "output": f"seed output {i} " + " ".join(f"s{i}o{j}" for j in range(15)),
    }) for i in range(4)), encoding="utf-8")

    doc = tmp_path / "documents.jsonl"
    doc.write_text("\n".join(json.dumps({
        "id": f"doc-{i}",
        "text": f"Document {i}. " + (f"Paragraph {i} with facts. " * 12),
    }) for i in range(3)), encoding="utf-8")

    if strategies_yaml is None:
        strategies_yaml = [
            {"type": "topic_driven", "weight": 0.5, "topics": [
                {"topic": "Python basics", "weight": 2},
                {"topic": "machine learning", "weight": 1},
            ]},
            {"type": "seed_driven", "weight": 0.5, "seed_file": str(seeds)},
        ]
    if pipeline_yaml is None:
        pipeline_yaml = [
            {"type": "length", "instruction": [5, 4000], "output": [10, 8000]},
            {"type": "exact_dedup"},
            {"type": "stats", "min_ngram_diversity": 0.2},
        ]
    if judge_yaml is None:
        judge_yaml = {
            "dimensions": [
                {"name": "correctness", "label": "Accuracy", "max": 10},
                {"name": "helpfulness", "label": "Usefulness", "max": 10},
            ],
            "min_total": 10,
        }

    raw = {
        "run": {"seed": 42},
        "llm": {"model": "fake-model", "lang": "en", "concurrency": 4},
        "plan": {"count": 10},
        "strategies": strategies_yaml,
        "pipeline": pipeline_yaml,
        "judge": judge_yaml,
        "output": {"path": str(output_path or (tmp_path / "out.duckdb"))},
        **yaml.safe_load(extra_yaml or "{}"),
    }
    cfg_path = tmp_path / "corpuslab.yaml"
    cfg_path.write_text(yaml.dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(str(cfg_path))
