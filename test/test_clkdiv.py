# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_clkdiv against a cycle-level Python model."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


class DividerModel:
    """Reference model written from docs/isa.md section 1.1, not from the RTL."""

    def __init__(self):
        self.wait = 0  # cycles until the next tick
        self.acc = 0

    def restart(self):
        self.wait = 0
        self.acc = 0

    def step(self, en, div_int, div_frac):
        """Advance one clock cycle and return whether it was a tick."""
        if not en:
            return False
        if self.wait:
            self.wait -= 1
            return False
        period = div_int if div_int else 65536
        self.acc += div_frac
        if self.acc >= 256:
            self.acc -= 256
            period += 1
        self.wait = period - 1
        return True


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.en.value = 0
    dut.restart.value = 0
    dut.div_int.value = 1
    dut.div_frac.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def run(dut, model, cycles, div_int, div_frac, en=1):
    """Run with fixed settings and return the tick pattern, checking the model."""
    dut.div_int.value = div_int
    dut.div_frac.value = div_frac
    dut.en.value = en
    ticks = []
    for i in range(cycles):
        await RisingEdge(dut.clk)
        got = int(dut.tick.value)
        want = model.step(en, div_int, div_frac)
        assert got == want, f"cycle {i}: tick={got}, model={int(want)} (div {div_int}+{div_frac}/256)"
        ticks.append(got)
    return ticks


@cocotb.test()
async def test_integer_division(dut):
    await start(dut)
    for div in (1, 2, 3, 7, 16, 100):
        model = DividerModel()
        dut.restart.value = 1
        await RisingEdge(dut.clk)
        dut.restart.value = 0
        ticks = await run(dut, model, div * 6, div, 0)
        assert ticks[0] == 1, "first enabled cycle must tick"
        assert sum(ticks) == 6
        where = [i for i, t in enumerate(ticks) if t]
        assert all(b - a == div for a, b in zip(where, where[1:]))


@cocotb.test()
async def test_fractional_division(dut):
    await start(dut)
    rng = random.Random(1)
    cases = [(2, 128), (3, 64), (5, 37), (4, 255), (2, 1)]
    cases += [(rng.randint(2, 40), rng.randint(1, 255)) for _ in range(10)]
    for div_int, div_frac in cases:
        model = DividerModel()
        dut.restart.value = 1
        await RisingEdge(dut.clk)
        dut.restart.value = 0
        cycles = (div_int + 1) * 256 * 2
        ticks = await run(dut, model, cycles, div_int, div_frac)
        where = [i for i, t in enumerate(ticks) if t]
        gaps = {b - a for a, b in zip(where, where[1:])}
        assert gaps <= {div_int, div_int + 1}, f"jitter above one cycle: {gaps}"
        # Over 256 ticks the average period is exact.
        assert where[256] - where[0] == div_int * 256 + div_frac


@cocotb.test()
async def test_zero_means_65536(dut):
    await start(dut)
    model = DividerModel()
    ticks = await run(dut, model, 65536 + 3, 0, 0)
    assert [i for i, t in enumerate(ticks) if t] == [0, 65536]


@cocotb.test()
async def test_enable_holds_and_restart(dut):
    await start(dut)
    model = DividerModel()
    rng = random.Random(2)
    # Dropping `en` freezes the phase; it does not reset it.
    for _ in range(200):
        await run(dut, model, rng.randint(1, 9), 5, 77, en=rng.randint(0, 1))
    # Restart from the middle of a period: the next enabled cycle ticks.
    await run(dut, model, 2, 9, 0)
    dut.restart.value = 1
    await RisingEdge(dut.clk)
    dut.restart.value = 0
    model.restart()
    ticks = await run(dut, model, 10, 9, 0)
    assert ticks[0] == 1 and sum(ticks) == 2
