"""Internal data-format contract tests (docs/data-format.md §2)."""
from __future__ import annotations

import json

import pytest

from corpuslab.core import checkpoint
from corpuslab.core.sample import FORMAT_VERSION, Sample
from corpuslab.core.store import Store
from tests.conftest import make_sample


def test_valid_single_turn():
    s = make_sample(1)
    assert s.validate() == []                    # no problems


def test_valid_multi_turn():
    s = Sample(id="m1", strategy="tool_call", messages=[{"role": "user",
                                                         "content": "hi"}],
               tools=[{"type": "function"}])
    assert s.validate() == []


@pytest.mark.parametrize("kw", [
    {"id": ""},                                   # C1
    {"strategy": ""},                             # C2
    {"instruction": "", "output": ""},            # C3 neither form
    {"messages": [{"role": "user"}], "instruction": "x"},   # C3 both forms
    {"metadata": "nope"},                         # C4 (rejected at parse or validate)
])
def test_invalid_samples(kw):
    base = dict(id="x", strategy="topic_driven",
                instruction="q", output="a", metadata={"lineage": {}})
    base.update(kw)
    if "messages" in kw and "instruction" in kw:
        base["output"] = ""
    with pytest.raises(ValueError):
        Sample.from_dict(base)                    # rejects at parse or validate


def test_from_dict_validates():
    with pytest.raises(ValueError, match="one form"):
        Sample.from_dict({"id": "x", "strategy": "s"})
    with pytest.raises(ValueError, match="non-empty"):
        Sample.from_dict({"id": "", "strategy": "s",
                          "instruction": "q", "output": "a"})


def test_roundtrip_valid():
    s = make_sample(7)
    s2 = Sample.from_json(s.to_json())
    assert s2.to_dict() == s.to_dict()
    assert s2.validate() == []


def test_manifest_carries_format_version(tmp_path):
    cfg = __import__("tests.conftest", fromlist=["make_config"]).make_config(tmp_path)
    store = Store(str(tmp_path / "s.duckdb"))
    checkpoint.write_manifest(store, cfg, num_perm=128)
    assert store.get_kv("format_version") == FORMAT_VERSION
    store.close()
