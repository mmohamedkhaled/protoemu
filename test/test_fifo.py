# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_fifo against a Python queue."""

import random
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

DEPTH = 4


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.clear.value = 0
    dut.push.value = 0
    dut.pop.value = 0
    dut.din.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


def check(dut, queue, what):
    assert int(dut.level.value) == len(queue), what
    assert int(dut.empty.value) == (len(queue) == 0), what
    assert int(dut.full.value) == (len(queue) == DEPTH), what
    if queue:
        assert int(dut.dout.value) == queue[0], what


async def cycle(dut, queue, push=0, din=0, pop=0, clear=0):
    """Apply one cycle of inputs, update the model, and check the result."""
    dut.push.value = push
    dut.din.value = din
    dut.pop.value = pop
    dut.clear.value = clear
    await RisingEdge(dut.clk)
    dut.push.value = 0
    dut.pop.value = 0
    dut.clear.value = 0
    if clear:
        queue.clear()
    else:
        # Both decisions use the state before the edge.
        can_push, can_pop = len(queue) < DEPTH, len(queue) > 0
        if pop and can_pop:
            queue.popleft()
        if push and can_push:
            queue.append(din)
    await RisingEdge(dut.clk)
    check(dut, queue, f"push={push} pop={pop} clear={clear}")


@cocotb.test()
async def test_fill_and_drain(dut):
    await start(dut)
    queue = deque()
    check(dut, queue, "reset")
    for i in range(DEPTH):
        await cycle(dut, queue, push=1, din=0x1000_0000 + i)
    assert int(dut.full.value) == 1
    await cycle(dut, queue, push=1, din=0xDEAD_BEEF)  # ignored
    for _ in range(DEPTH):
        await cycle(dut, queue, pop=1)
    assert int(dut.empty.value) == 1
    await cycle(dut, queue, pop=1)  # ignored


@cocotb.test()
async def test_push_and_pop_together(dut):
    await start(dut)
    queue = deque()
    await cycle(dut, queue, push=1, din=1)
    for i in range(20):
        await cycle(dut, queue, push=1, din=100 + i, pop=1)
        assert len(queue) == 1
    # When full, the pop happens and the push is dropped.
    for i in range(DEPTH):
        await cycle(dut, queue, push=1, din=200 + i)
    await cycle(dut, queue, push=1, din=999, pop=1)
    assert len(queue) == DEPTH - 1 and 999 not in queue


@cocotb.test()
async def test_random(dut):
    await start(dut)
    rng = random.Random(6)
    queue = deque()
    for _ in range(5000):
        await cycle(dut, queue, push=rng.random() < 0.5, din=rng.getrandbits(32),
                    pop=rng.random() < 0.45, clear=rng.random() < 0.01)
