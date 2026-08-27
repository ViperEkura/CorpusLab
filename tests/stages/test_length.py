"""length stage: boundary values (min/max inclusive), messages samples."""
from __future__ import annotations

import pytest

from corpuslab.config import schema as S
from corpuslab.stages.length import LengthStage
from tests.conftest import make_sample


class FakeCtx:
    def __init__(self):
        from corpuslab.core.sample import RunReport
        self.report = RunReport()
        self.store = None
        self.preview = True

    def drop(self, sample, stage, reason):
        self.report.drop(stage, reason)


async def _collect(stage, samples):
    async def stream():
        for s in samples:
            yield s
    ctx = FakeCtx()
    out = [s async for s in stage.apply_stream(stream(), ctx)]
    return out, ctx.report


@pytest.mark.asyncio
async def test_boundaries_inclusive():
    stage = LengthStage(S.LengthStageCfg(type="length", instruction=[5, 10],
                                         output=[2, 20]))
    ok = make_sample(0, instruction="12345", output="ok")          # both at min
    dropped_i = make_sample(1, instruction="1234", output="ok")    # instr < 5
    dropped_o = make_sample(2, instruction="12345", output="o")    # out < 2
    long_i = make_sample(3, instruction="x" * 11, output="ok")
    out, report = await _collect(stage, [ok, dropped_i, dropped_o, long_i])
    assert out == [ok]
    assert report.dropped["length"]["instruction_len:4"] == 1
    assert report.dropped["length"]["output_len:1"] == 1
    assert "instruction_len:11" in report.dropped["length"]


@pytest.mark.asyncio
async def test_messages_sample_length():
    stage = LengthStage(S.LengthStageCfg(type="length"))
    sample = make_sample(7)
    sample.instruction = ""
    sample.output = ""
    sample.messages = [{"role": "system", "content": "sys"},
                       {"role": "user", "content": "query text"},
                       {"role": "assistant", "content": "answer"}]
    out, _ = await _collect(stage, [sample])
    assert out == [sample]
    # user message below the instruction floor → drop
    sample.messages[1]["content"] = "hi"
    out, report = await _collect(stage, [sample])
    assert not out
