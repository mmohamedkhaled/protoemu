# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_shift (ISR and OSR) against a Python model."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

M32 = 0xFFFFFFFF


class ShiftModel:
    """Written from docs/isa.md sections 4.3 and 4.4 using plain shifts."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.isr = 0
        self.isr_count = 0
        self.osr = 0
        self.osr_count = 32

    def shift_in(self, right, n, data):
        bits = data & ((1 << n) - 1)
        if right:
            self.isr = ((self.isr >> n) | (bits << (32 - n))) & M32
        else:
            self.isr = ((self.isr << n) | bits) & M32
        self.isr_count = min(32, self.isr_count + n)

    def shift_out(self, right, n):
        if right:
            bits = self.osr & ((1 << n) - 1)
            self.osr >>= n
        else:
            bits = self.osr >> (32 - n)
            self.osr = (self.osr << n) & M32
        self.osr_count = min(32, self.osr_count + n)
        return bits


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for name in ("clear", "in_dir", "in_shift", "in_data", "isr_load", "isr_load_data",
                 "isr_clear", "out_dir", "out_shift", "osr_load", "osr_load_data"):
        getattr(dut, name).value = 0
    dut.in_n.value = 1
    dut.out_n.value = 1
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


def check_state(dut, model, what):
    got = (int(dut.isr.value), int(dut.isr_count.value), int(dut.osr.value), int(dut.osr_count.value))
    want = (model.isr, model.isr_count, model.osr, model.osr_count)
    assert got == want, f"after {what}: got {[hex(v) for v in got]}, want {[hex(v) for v in want]}"


async def edge(dut):
    """One clock edge with the current inputs, then drop the strobes."""
    await RisingEdge(dut.clk)
    for name in ("clear", "in_shift", "isr_load", "isr_clear", "out_shift", "osr_load"):
        getattr(dut, name).value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_reset_state(dut):
    await start(dut)
    check_state(dut, ShiftModel(), "reset")


@cocotb.test()
async def test_shift_in_every_width(dut):
    await start(dut)
    rng = random.Random(3)
    model = ShiftModel()
    for right in (0, 1):
        for n in range(1, 33):
            preload, data = rng.getrandbits(32), rng.getrandbits(32)
            dut.isr_load.value = 1
            dut.isr_load_data.value = preload
            await edge(dut)
            model.isr, model.isr_count = preload, 0
            dut.in_dir.value = right
            dut.in_n.value = n
            dut.in_data.value = data
            dut.in_shift.value = 1
            model.shift_in(right, n, data)
            await RisingEdge(dut.clk)
            # The combinational preview must equal what gets registered.
            assert int(dut.isr_shifted.value) == model.isr
            assert int(dut.isr_count_next.value) == model.isr_count
            dut.in_shift.value = 0
            await RisingEdge(dut.clk)
            check_state(dut, model, f"in dir={right} n={n}")


@cocotb.test()
async def test_shift_out_every_width(dut):
    await start(dut)
    rng = random.Random(4)
    model = ShiftModel()
    for right in (0, 1):
        for n in range(1, 33):
            preload = rng.getrandbits(32)
            dut.osr_load.value = 1
            dut.osr_load_data.value = preload
            await edge(dut)
            model.osr, model.osr_count = preload, 0
            check_state(dut, model, "osr load")
            dut.out_dir.value = right
            dut.out_n.value = n
            dut.out_shift.value = 1
            want_bits = model.shift_out(right, n)
            await RisingEdge(dut.clk)
            assert int(dut.out_data.value) == want_bits, f"out dir={right} n={n}"
            assert int(dut.osr_count_next.value) == model.osr_count
            dut.out_shift.value = 0
            await RisingEdge(dut.clk)
            check_state(dut, model, f"out dir={right} n={n}")


@cocotb.test()
async def test_random_operations(dut):
    await start(dut)
    rng = random.Random(5)
    model = ShiftModel()
    for i in range(4000):
        op = rng.choice(["in", "in", "in", "out", "out", "out", "isr_load", "isr_clear",
                         "osr_load", "clear", "both", "idle"])
        n_in, n_out = rng.randint(1, 32), rng.randint(1, 32)
        d_in, d_load = rng.getrandbits(32), rng.getrandbits(32)
        right_in, right_out = rng.randint(0, 1), rng.randint(0, 1)
        dut.in_dir.value = right_in
        dut.out_dir.value = right_out
        dut.in_n.value = n_in
        dut.out_n.value = n_out
        dut.in_data.value = d_in
        dut.isr_load_data.value = d_load
        dut.osr_load_data.value = d_load
        if op in ("in", "both"):
            dut.in_shift.value = 1
            model.shift_in(right_in, n_in, d_in)
        if op in ("out", "both"):
            dut.out_shift.value = 1
            model.shift_out(right_out, n_out)
        if op == "isr_load":
            dut.isr_load.value = 1
            model.isr, model.isr_count = d_load, 0
        if op == "isr_clear":
            # A clear in the same cycle as a shift wins (autopush).
            dut.isr_clear.value = 1
            dut.in_shift.value = 1
            model.isr, model.isr_count = 0, 0
        if op == "osr_load":
            # A load in the same cycle as a shift wins (autopull refill).
            dut.osr_load.value = 1
            dut.out_shift.value = 1
            model.osr, model.osr_count = d_load, 0
        if op == "clear":
            dut.clear.value = 1
            model.clear()
        await edge(dut)
        check_state(dut, model, f"step {i} ({op})")
