# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_cfg_spi: an SPI master on one side, a register bus model on the other."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from host import SpiTransport


class BusModel:
    """A 128-byte memory on the register bus, logging every access."""

    def __init__(self, dut):
        self.dut = dut
        self.mem = [(7 * a + 3) & 0xFF for a in range(128)]
        self.log = []

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            addr = int(dut.bus_addr.value)
            if int(dut.bus_we.value):
                self.mem[addr] = int(dut.bus_wdata.value)
                self.log.append(("w", addr, self.mem[addr]))
            if int(dut.bus_re.value):
                self.log.append(("r", addr))
            await Timer(1, unit="ns")
            dut.bus_rdata.value = self.mem[int(dut.bus_addr.value)]


async def start(dut, half_period=4):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.bus_rdata.value = 0

    def set_pins(sck, csn, mosi):
        dut.sck.value = sck
        dut.csn.value = csn
        dut.mosi.value = mosi

    spi = SpiTransport(dut.clk, set_pins, lambda: int(dut.miso.value), half_period)
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    bus = BusModel(dut)
    cocotb.start_soon(bus.run())
    for _ in range(4):
        await RisingEdge(dut.clk)
    return spi, bus


@cocotb.test()
async def test_single_write_and_read(dut):
    spi, bus = await start(dut)
    await spi.write(0x12, [0xA5])
    assert bus.log == [("w", 0x12, 0xA5)]
    bus.log.clear()
    assert await spi.read(0x12, 1) == bytes([0xA5])
    assert bus.log == [("r", 0x12)], "exactly one read strobe per byte read"


@cocotb.test()
async def test_burst_increments_the_address(dut):
    spi, bus = await start(dut)
    data = [0x11, 0x22, 0x33, 0x44, 0x55]
    await spi.write(0x30, data)
    assert bus.log == [("w", 0x30 + i, d) for i, d in enumerate(data)]
    bus.log.clear()
    assert await spi.read(0x30, 5) == bytes(data)
    assert bus.log == [("r", 0x30 + i) for i in range(5)]


@cocotb.test()
async def test_address_wraps(dut):
    spi, bus = await start(dut)
    await spi.write(0x7E, [1, 2, 3])
    assert bus.log == [("w", 0x7E, 1), ("w", 0x7F, 2), ("w", 0x00, 3)]


@cocotb.test()
async def test_aborted_frame_has_no_effect(dut):
    spi, bus = await start(dut)
    # Command byte and four bits of data, then CSn goes high.
    spi.set_pins(0, 0, 0)
    await spi._wait()
    for bit in [1, 0, 0, 0, 0, 1, 0, 1] + [1, 1, 1, 1]:
        spi.set_pins(0, 0, bit)
        await spi._wait()
        spi.set_pins(1, 0, bit)
        await spi._wait()
    spi.set_pins(0, 1, 0)
    await spi._wait(10)
    assert bus.log == []
    # The next frame starts cleanly.
    await spi.write(0x05, [0x77])
    assert bus.log == [("w", 0x05, 0x77)]


@cocotb.test()
async def test_miso_is_low_outside_reads(dut):
    spi, bus = await start(dut)
    assert int(dut.miso.value) == 0
    assert await spi.transfer(bytes([0x80 | 0x10, 0xFF, 0xFF]), 0) == b""
    seen = await spi.transfer(bytes([0x80 | 0x10]), 2)  # reading during a write frame
    assert seen == bytes([0, 0])
    # SCK edges while CSn is high do nothing.
    for _ in range(20):
        spi.set_pins(1, 1, 1)
        await spi._wait()
        spi.set_pins(0, 1, 1)
        await spi._wait()
    assert [e for e in bus.log if e[0] == "r"] == []
    assert int(dut.miso.value) == 0


@cocotb.test()
async def test_random_frames_at_several_speeds(dut):
    rng = random.Random(8)
    spi, bus = await start(dut)
    shadow = list(bus.mem)
    for half_period in (4, 5, 9):
        spi.half = half_period
        for _ in range(40):
            addr, n = rng.getrandbits(7), rng.randint(1, 6)
            if rng.random() < 0.5:
                data = [rng.getrandbits(8) for _ in range(n)]
                await spi.write(addr, data)
                for i, d in enumerate(data):
                    shadow[(addr + i) & 0x7F] = d
            else:
                got = await spi.read(addr, n)
                assert list(got) == [shadow[(addr + i) & 0x7F] for i in range(n)], (
                    f"read {n} at {addr:#x}, SCK = clk/{2 * half_period}")
        assert bus.mem == shadow
