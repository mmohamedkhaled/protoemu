# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_imem."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


async def write_byte(dut, addr, sel, data):
    dut.waddr.value = addr
    dut.wsel.value = sel
    dut.wdata.value = data
    dut.we.value = 1
    await RisingEdge(dut.clk)
    dut.we.value = 0


async def write_word(dut, addr, word):
    await write_byte(dut, addr, 0, word & 0xFF)
    await write_byte(dut, addr, 1, word >> 8)


@cocotb.test()
async def test_write_then_read_both_ports(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.we.value = 0
    dut.raddr0.value = 0
    dut.raddr1.value = 0
    rng = random.Random(7)
    words = [rng.getrandbits(16) for _ in range(32)]
    for addr, word in enumerate(words):
        await write_word(dut, addr, word)
    await RisingEdge(dut.clk)
    for _ in range(200):
        a0, a1 = rng.getrandbits(5), rng.getrandbits(5)
        dut.raddr0.value = a0
        dut.raddr1.value = a1
        await Timer(1, unit="ns")  # reads are combinational
        assert int(dut.rdata0.value) == words[a0]
        assert int(dut.rdata1.value) == words[a1]


@cocotb.test()
async def test_byte_writes_are_independent(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.we.value = 0
    dut.raddr0.value = 9
    dut.raddr1.value = 10
    await write_word(dut, 9, 0x1234)
    await write_word(dut, 10, 0xABCD)
    await write_byte(dut, 9, 1, 0x56)   # high byte only
    await write_byte(dut, 10, 0, 0xEF)  # low byte only
    await RisingEdge(dut.clk)
    assert int(dut.rdata0.value) == 0x5634
    assert int(dut.rdata1.value) == 0xABEF
    # Without `we` nothing is written.
    dut.waddr.value = 9
    dut.wdata.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert int(dut.rdata0.value) == 0x5634
