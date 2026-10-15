# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_trigger: the pulse generator and the cycle timer, to the clock cycle."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ena.value = 1
    dut.fire.value = 0
    dut.timer_reset.value = 0
    dut.delay.value = 0
    dut.width.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def edge(dut):
    """One clock edge; return `out` as it is after the edge."""
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    return int(dut.out.value)


async def fire_and_record(dut, cycles):
    """Fire on the next edge (edge 0) and return `out` after edges 0 .. cycles - 1."""
    dut.fire.value = 1
    trace = [await edge(dut)]
    dut.fire.value = 0
    trace += [await edge(dut) for _ in range(cycles - 1)]
    return trace


@cocotb.test()
async def test_pulse_position_and_length(dut):
    """Fired on edge E: high from edge E + delay, for width + 1 cycles."""
    await start(dut)
    for delay, width in [(0, 0), (0, 3), (1, 0), (1, 1), (5, 2), (17, 0), (40, 9), (3, 255)]:
        dut.delay.value = delay
        dut.width.value = width
        trace = await fire_and_record(dut, delay + width + 6)
        want = [int(delay <= i <= delay + width) for i in range(len(trace))]
        assert trace == want, f"delay {delay} width {width}: {trace}"
        assert int(dut.busy.value) == 0


@cocotb.test()
async def test_long_delay(dut):
    await start(dut)
    dut.delay.value = 0xFFFF
    dut.width.value = 1
    trace = await fire_and_record(dut, 0xFFFF + 5)
    assert trace.index(1) == 0xFFFF and sum(trace) == 2


@cocotb.test()
async def test_refire_restarts(dut):
    await start(dut)
    dut.delay.value = 4
    dut.width.value = 2
    await fire_and_record(dut, 3)              # still in the delay
    trace = await fire_and_record(dut, 12)     # fired again: the count starts over
    assert trace == [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0]
    await fire_and_record(dut, 6)              # now in the pulse
    trace = await fire_and_record(dut, 9)      # a re-fire cuts the pulse and starts over
    assert trace == [0, 0, 0, 0, 1, 1, 1, 0, 0]


@cocotb.test()
async def test_random_against_model(dut):
    await start(dut)
    rng = random.Random(11)
    delay, width = 3, 2
    age = None  # edges since the last fire
    for i in range(6000):
        if age is None or age > delay + width + 2:
            if rng.random() < 0.2:             # settings change only while idle
                delay, width = rng.randint(0, 12), rng.randint(0, 6)
                dut.delay.value = delay
                dut.width.value = width
                age = None                     # and the old pulse no longer counts
        fire = rng.random() < 0.08
        dut.fire.value = fire
        got = await edge(dut)
        age = 0 if fire else (None if age is None else age + 1)
        want = int(age is not None and delay <= age <= delay + width)
        assert got == want, f"step {i}: out={got}, want {want} (age {age}, delay {delay}, width {width})"


@cocotb.test()
async def test_timer(dut):
    """Reset on edge E: an instruction executing on edge E + k reads k."""
    await start(dut)
    dut.timer_reset.value = 1
    await RisingEdge(dut.clk)                  # edge E
    dut.timer_reset.value = 0
    for k in range(1, 300):
        await RisingEdge(dut.clk)              # edge E + k sees the value from before the edge
        assert int(dut.timer.value) == k
    # It holds while the chip is deselected, and a reset restarts it.
    dut.ena.value = 0
    await Timer(1, unit="ns")
    held = int(dut.timer.value)
    for _ in range(5):
        await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    assert int(dut.timer.value) == held
    dut.ena.value = 1
    dut.timer_reset.value = 1
    await RisingEdge(dut.clk)
    dut.timer_reset.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.timer.value) == 1
