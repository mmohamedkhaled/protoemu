# SPDX-License-Identifier: Apache-2.0
"""Whole-chip tests.

Each test loads a program from firmware/ through the SPI configuration port,
exactly as a host would, and checks what appears on the pads against an
independent reference model of the protocol (test/models). Only the chip's
pins are used, so the same tests run on the gate-level netlist.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

import asm
import host as H
from board import Board
from host import Host, chip_spi
from models.i2c import I2cMemory
from models.jtag import JtagTap
from models.spi import SpiTarget
from models.swd import SwdTarget, parity
from models.timing import PulseTrain, Responder, SpiBusDriver
from models.uart import UartReceiver, UartTransmitter
from models.ws2812 import Ws2812Receiver

FIRMWARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
CLK_HZ = 50_000_000


async def setup(dut):
    """Start the clock, reset the chip, and return (host, board)."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0b010  # CSn high
    dut.uio_in.value = 0xFF
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 4)
    board = Board(dut)
    cocotb.start_soon(board.run())
    h = Host(chip_spi(dut))
    assert await h.read(H.ID) == H.ID_VALUE
    return h, board


async def load_firmware(h, name, sm, origin=0, clkdiv=(1, 0), **pins):
    """Assemble firmware/<name>.pio, load it over SPI and configure state machine `sm`."""
    prog = asm.assemble_file(os.path.join(FIRMWARE, name + ".pio"), origin=origin)
    await h.load_program(prog.words, origin)
    await h.configure(sm, clkdiv=clkdiv, **dict(prog.sm_config(), **pins))
    return prog


async def wait_until(dut, condition, cycles=200_000, what="condition"):
    for _ in range(cycles // 16):
        if condition():
            return
        await ClockCycles(dut.clk, 16)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------- UART


@cocotb.test()
async def test_uart_tx(dut):
    h, board = await setup(dut)
    TX, DIV = 3, 6                      # 8 ticks per bit, 6 clocks per tick
    rx = board.attach(UartReceiver(TX, bit_cycles=8 * DIV))
    await load_firmware(h, "uart_tx", sm=0, clkdiv=(DIV, 0), out_base=TX, side_base=TX)
    await h.set_pins(out=1 << TX, dirs=1 << TX)
    await h.start(0)

    message = [0x55, 0x00, 0xFF, 0xA3, 0x80, 0x01, ord("J"), ord("S")]
    for byte in message:
        await h.tx_wait(0, byte)
    await wait_until(dut, lambda: len(rx.bytes) == len(message), what="all UART bytes")

    assert rx.bytes == message
    assert rx.framing_errors == 0
    # Cycle-exact timing: every edge on the line is a whole number of bit times
    # after the previous one.
    gaps = {b - a for a, b in zip(rx.edges, rx.edges[1:])}
    assert all(g % (8 * DIV) == 0 for g in gaps), f"edges off the bit grid: {sorted(gaps)}"
    assert board.contention == []


@cocotb.test()
async def test_datasheet_example(dut):
    """The raw SPI bytes of the first test in docs/info.md, sent exactly as printed there."""
    h, board = await setup(dut)
    frames = """
        C0  A0 9F  27 F7  01 60  42 06
        84  41 36 00  00 03
        89  00 00 00
        8C  25
        98  01 01 00
        80 0E
        83 00
        80 01
    """
    for line in frames.strip().splitlines():
        await h.t.transfer(bytes.fromhex(line))
    rx = board.attach(UartReceiver(0, bit_cycles=434))
    await h.t.transfer(bytes.fromhex("90  4A 00 00 00"))
    await wait_until(dut, lambda: len(rx.bytes) == 1, what="the UART byte")
    assert rx.bytes == [0x4A] and rx.framing_errors == 0
    # From the start bit's falling edge to the stop bit's rising edge is nine bit times.
    assert abs((rx.edges[-1] - rx.edges[0]) - 9 * CLK_HZ / 115200) <= 2

    # The trigger example: delay 10, width 4, fired by hand.
    board.uo_trace = {}
    await h.t.transfer(bytes.fromhex("B8 0A 00 04 02"))
    await ClockCycles(dut.clk, 30)
    high = [c for c, value in sorted(board.uo_trace.items()) if value & 0x20]
    assert len(high) == 5 and high == list(range(high[0], high[0] + 5))


@cocotb.test()
async def test_uart_tx_fractional_baud(dut):
    """115200 baud from a 50 MHz clock needs the fractional divider (54.25)."""
    h, board = await setup(dut)
    TX = 0
    div = H.clkdiv_for(CLK_HZ, 8 * 115200)
    assert div == (54, 65)
    rx = board.attach(UartReceiver(TX, bit_cycles=round(CLK_HZ / 115200)))
    await load_firmware(h, "uart_tx", sm=1, clkdiv=div, out_base=TX, side_base=TX)
    await h.set_pins(out=1 << TX, dirs=1 << TX)
    await h.start(1)
    message = [0x4A, 0x53, 0xF0, 0x0F]
    for byte in message:
        await h.tx_wait(1, byte)
    await wait_until(dut, lambda: len(rx.bytes) == len(message), what="all UART bytes")
    assert rx.bytes == message and rx.framing_errors == 0
    # Over the whole message the line stays within a few clocks of the ideal bit grid.
    bit = CLK_HZ / 115200
    span = rx.edges[-1] - rx.edges[0]
    assert abs(span - round(span / bit) * bit) <= 8, f"{span} clocks is off the 115200 baud grid"


@cocotb.test()
async def test_uart_rx(dut):
    h, board = await setup(dut)
    RX, DIV = 5, 5
    tx = board.attach(UartTransmitter(RX, bit_cycles=8 * DIV))
    await load_firmware(h, "uart_rx", sm=0, clkdiv=(DIV, 0), in_base=RX, jmp_pin=RX)
    await h.start(0)

    message = [0x00, 0xFF, 0x5A, 0xC3, 0x01, 0x80]
    received = []
    for byte in message:
        tx.send(byte)
        await wait_until(dut, lambda: not tx.busy, what="UART byte sent")
        received.append(await h.rx_wait(0) >> 24)
    assert received == message

    # A byte with a low stop bit is dropped, and the next good byte still arrives.
    tx.send(0x77, stop_bit=0, idle_bits=3)
    tx.send(0x42)
    await wait_until(dut, lambda: not tx.busy, what="UART bytes sent")
    assert await h.rx_wait(0) >> 24 == 0x42
    assert await h.status(0) & H.ST_RX_EMPTY
    assert board.contention == []


# ---------------------------------------------------------------- SPI


@cocotb.test()
async def test_spi_master(dut):
    h, board = await setup(dut)
    SCK, MOSI, MISO, CS, DIV = 0, 1, 2, 3, 4
    reply = [0x9F, 0x00, 0xFF, 0x3C, 0xA5, 0x81]
    target = board.attach(SpiTarget(SCK, MOSI, MISO, cs=CS, reply=reply))
    await load_firmware(h, "spi_master", sm=0, clkdiv=(DIV, 0),
                        out_base=MOSI, in_base=MISO, side_base=SCK)
    await h.set_pins(out=1 << CS, dirs=(1 << SCK) | (1 << MOSI) | (1 << CS))
    await h.start(0)

    sent = [0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0xFF]
    await h.write(H.PIN_OUT, 0)                  # chip select low
    received = []
    for byte in sent:
        await h.tx(0, byte << 24)
        received.append(await h.rx_wait(0) & 0xFF)
    await h.write(H.PIN_OUT, 1 << CS)            # chip select high

    assert target.received == sent
    assert received == reply
    assert target.sck_idle_violations == 0
    assert board.contention == []


# ---------------------------------------------------------------- I2C


def i2c_commands(prog):
    """Command words for firmware/i2c_master.pio."""
    start, stop, byte = (prog.labels[n] << 27 for n in ("start", "stop", "byte"))

    def send(value, ack_bit=1):
        return byte | (value << 19) | (ack_bit << 18)

    return start, stop, send


async def i2c_setup(dut, stretch=0):
    h, board = await setup(dut)
    SDA, SCL, DIV = 6, 7, 6
    mem = board.attach(I2cMemory(SDA, SCL, address=0x50, stretch=stretch))
    prog = await load_firmware(h, "i2c_master", sm=0, clkdiv=(DIV, 0), set_base=SDA, set_count=1,
                               out_base=SDA, in_base=SDA, side_base=SCL)
    both = (1 << SDA) | (1 << SCL)
    await h.set_pins(out=both, dirs=both, od=both)
    await h.start(0, pc=prog.labels["dispatch"])
    return h, board, mem, i2c_commands(prog)


async def i2c_run(h, commands):
    """Send command words; return the (byte, ack) pair for each `byte` command."""
    results = []
    for is_byte, word in commands:
        await h.tx_wait(0, word)
        if is_byte:
            value = await h.rx_wait(0)
            results.append((value >> 1 & 0xFF, value & 1))
    return results


async def i2c_write_then_read(dut, stretch):
    h, board, mem, (start, stop, send) = await i2c_setup(dut, stretch)
    data = [0xA5, 0x00, 0xFF, 0x3C]

    # Write four bytes at pointer 0x10.
    cmds = [(0, start), (1, send(0x50 << 1)), (1, send(0x10))]
    cmds += [(1, send(b)) for b in data] + [(0, stop)]
    acks = await i2c_run(h, cmds)
    assert [a for _, a in acks] == [0] * 6, "the target acknowledges every byte"
    assert [b for b, _ in acks] == [0xA0, 0x10] + data, "what we send is what is on the bus"
    await wait_until(dut, lambda: mem.stops == 1, what="STOP")
    assert [mem.mem.get(0x10 + i) for i in range(4)] == data

    # Set the pointer, repeated START, read the four bytes back, NACK the last.
    cmds = [(0, start), (1, send(0x50 << 1)), (1, send(0x10)), (0, start),
            (1, send((0x50 << 1) | 1))]
    cmds += [(1, send(0xFF, ack_bit=0))] * 3 + [(1, send(0xFF, ack_bit=1)), (0, stop)]
    results = await i2c_run(h, cmds)
    assert [b for b, _ in results[3:]] == data
    assert [a for _, a in results] == [0, 0, 0, 0, 0, 0, 1]
    await wait_until(dut, lambda: mem.stops == 2, what="STOP")
    assert mem.starts == 3
    assert board.contention == []
    return mem


@cocotb.test()
async def test_i2c_master(dut):
    await i2c_write_then_read(dut, stretch=0)


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """The target holds SCL low for 100 clocks before every ACK; `wait` rides it out."""
    mem = await i2c_write_then_read(dut, stretch=100)
    assert mem.stretched >= 12


@cocotb.test()
async def test_i2c_absent_target_is_not_acknowledged(dut):
    h, board, mem, (start, stop, send) = await i2c_setup(dut)
    results = await i2c_run(h, [(0, start), (1, send(0x23 << 1)), (0, stop)])
    assert results == [(0x46, 1)]
    await wait_until(dut, lambda: mem.stops == 1, what="STOP")
    assert mem.mem == {}


# ---------------------------------------------------------------- JTAG


def jtag_words(cycles):
    """Pack (tms, tdi) pairs into TX words for firmware/jtag.pio, padding with idle cycles."""
    cycles = list(cycles) + [(0, 0)] * (-len(cycles) % 16)
    words = []
    for i in range(0, len(cycles), 16):
        words.append(sum((tms | (tdi << 1)) << (2 * n) for n, (tms, tdi) in enumerate(cycles[i:i + 16])))
    return words


async def jtag_run(h, cycles):
    """Clock the TAP through `cycles`; return TDO as sampled in each cycle."""
    tdo = []
    for word in jtag_words(cycles):
        await h.tx_wait(0, word)
        value = await h.rx_wait(0)
        tdo += [(value >> (16 + n)) & 1 for n in range(16)]
    return tdo


@cocotb.test()
async def test_jtag_idcode(dut):
    h, board = await setup(dut)
    TMS, TDI, TDO, TCK, DIV = 0, 1, 2, 3, 4
    IDCODE = 0x4BA00477
    tap = board.attach(JtagTap(TCK, TMS, TDI, TDO, idcode=IDCODE))
    await load_firmware(h, "jtag", sm=0, clkdiv=(DIV, 0), out_base=TMS, in_base=TDO, side_base=TCK)
    await h.set_pins(dirs=(1 << TMS) | (1 << TDI) | (1 << TCK))
    await h.start(0)

    # Reset the TAP, go to Shift-DR, shift 32 bits, leave through Update-DR.
    go_shift_dr = [(1, 0)] * 5 + [(0, 0), (1, 0), (0, 0), (0, 0)]
    shift = [(0, 0)] * 31 + [(1, 0)]
    cycles = go_shift_dr + shift + [(1, 0), (0, 0)]
    tdo = await jtag_run(h, cycles)
    first = len(go_shift_dr)
    idcode = sum(bit << i for i, bit in enumerate(tdo[first:first + 32]))
    assert idcode == IDCODE, f"read {idcode:#010x}"
    assert tap.visited[:7] == ["test_logic_reset", "run_test_idle", "select_dr", "capture_dr",
                               "shift_dr", "exit1_dr", "update_dr"]

    # Load BYPASS through the instruction register; data then comes back one cycle late.
    go_shift_ir = [(1, 0), (1, 0), (0, 0), (0, 0)]
    ir = [(0, 1), (0, 1), (0, 1), (1, 1)]                      # 0b1111, last bit with TMS high
    to_shift_dr = [(1, 0), (1, 0), (0, 0), (0, 0)]             # update, select-DR, capture, shift
    pattern = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0]
    data = [(0, bit) for bit in pattern]
    cycles = go_shift_ir + ir + to_shift_dr + data
    tdo = await jtag_run(h, cycles)
    first = len(go_shift_ir) + len(ir) + len(to_shift_dr)
    assert tap.ir == 0b1111
    assert tdo[first + 1:first + len(pattern)] == pattern[:-1]
    assert board.contention == []


# ---------------------------------------------------------------- SWD


class SwdHost:
    """Host side of firmware/swd.pio."""

    def __init__(self, h, prog, sm=0):
        self.h, self.sm = h, sm
        self.write_cmd, self.read_cmd = prog.labels["write"], prog.labels["read"]

    async def write_bits(self, value, count):
        while count:
            n = min(count, 32)
            await self.h.tx_wait(self.sm, self.write_cmd | ((n - 1) << 5))
            await self.h.tx_wait(self.sm, value & ((1 << n) - 1))
            value >>= n
            count -= n

    async def read_bits(self, count):
        await self.h.tx_wait(self.sm, self.read_cmd | ((count - 1) << 5))
        return await self.h.rx_wait(self.sm) >> (32 - count)

    async def line_reset(self):
        await self.write_bits((1 << 56) - 1, 56)

    async def read_dp(self, address):
        """Read a debug-port register; return (ack, data, parity_ok)."""
        a2, a3 = (address >> 2) & 1, (address >> 3) & 1
        request = 0x81 | (1 << 2) | (a2 << 3) | (a3 << 4) | ((1 ^ a2 ^ a3) << 5)
        await self.write_bits(request, 8)
        ack = await self.read_bits(4) >> 1          # the first bit is the turnaround
        data = await self.read_bits(32)
        tail = await self.read_bits(2)              # parity, then the turnaround
        await self.write_bits(0, 8)                 # idle clocks
        return ack, data, (tail & 1) == parity(data)


@cocotb.test()
async def test_swd_idcode(dut):
    h, board = await setup(dut)
    SWDIO, SWCLK, DIV = 4, 5, 4
    DPIDR = 0x2BA01477
    target = board.attach(SwdTarget(SWCLK, SWDIO, dpidr=DPIDR))
    prog = await load_firmware(h, "swd", sm=0, clkdiv=(DIV, 0), set_base=SWDIO, set_count=1,
                               out_base=SWDIO, in_base=SWDIO, side_base=SWCLK)
    await h.set_pins(dirs=(1 << SWCLK))
    await h.start(0, pc=prog.labels["dispatch"])
    swd = SwdHost(h, prog)

    # Line reset, JTAG-to-SWD switch, line reset, idle: the standard connect sequence.
    await swd.line_reset()
    await swd.write_bits(0xE79E, 16)
    await swd.line_reset()
    await swd.write_bits(0, 4)

    ack, data, parity_ok = await swd.read_dp(0x0)
    assert ack == 0b001, f"ACK {ack:03b}"
    assert data == DPIDR, f"read {data:#010x}"
    assert parity_ok
    assert target.requests[-1] == 0xA5
    assert target.line_resets == 2 and target.reads_answered == 1

    # Reading it again works without a new reset.
    assert await swd.read_dp(0x0) == (0b001, DPIDR, True)
    assert board.contention == [], "SWDIO was never driven from both ends"


# ---------------------------------------------------------------- beyond the built-in list


@cocotb.test()
async def test_new_protocol_without_rtl_change(dut):
    """WS2812 LEDs are nowhere in the RTL: the protocol exists only as firmware/ws2812.pio."""
    h, board = await setup(dut)
    PIN, DIV = 2, 3
    await load_firmware(h, "ws2812", sm=1, clkdiv=(DIV, 0), side_base=PIN)
    await h.set_pins(dirs=1 << PIN)
    # The pin floats high until the chip drives it, so the LEDs are connected after that.
    leds = board.attach(Ws2812Receiver(PIN, bit_cycles=10 * DIV))

    # Queue the pixels first so the stream on the pin has no gaps.
    pixels = [0xFF0000, 0x00FF00, 0x123456, 0xA5C33C]
    await h.prepare(1)
    for pixel in pixels:
        await h.tx(1, pixel << 8)
    await h.enable(1)
    await wait_until(dut, lambda: len(leds.bits) == 24 * len(pixels), what="all pixels")
    await ClockCycles(dut.clk, 200)

    assert leds.pixels == pixels
    # Cycle-exact pulse widths: 3 ticks high for a 0, 6 for a 1, 10 ticks per bit.
    assert set(leds.high_times) == {3 * DIV, 6 * DIV}
    assert set(leds.periods) == {10 * DIV}
    assert board.contention == []


# ---------------------------------------------------------------- trigger and timer


@cocotb.test()
async def test_trigger_on_the_nth_edge(dut):
    """firmware/trigger_edge.pio: the pulse on uo[5] is placed to the clock cycle."""
    h, board = await setup(dut)
    WATCH, PERIOD = 4, 14
    await load_firmware(h, "trigger_edge", sm=0, in_base=WATCH)
    await h.start(0)
    board.uo_trace = {}

    for n, delay, width in [(1, 0, 0), (3, 0, 4), (2, 7, 1), (5, 300, 20)]:
        await h.set_trigger(delay=delay, width=width)
        clock = board.attach(PulseTrain(WATCH, PERIOD, start_cycle=board.cycle + 600))
        await h.tx(0, n - 1)                              # arm before the clock starts
        assert await h.rx_wait(0) == 0
        await ClockCycles(dut.clk, delay + width + 40)
        board.detach(clock)

        high = [c for c in range(clock.rising[0] - 5, board.cycle - 1) if board.uo_trace[c] & 0x20]
        first = clock.rising[n - 1] + 5 + delay
        assert high == list(range(first, first + width + 1)), (
            f"edge {n}, delay {delay}, width {width}: pulse on cycles {high[:3]}..{high[-1:]}, "
            f"want {first}..{first + width}")
        await ClockCycles(dut.clk, 2 * PERIOD)

    # Inverted polarity for an active-low glitcher input, and firing by hand.
    await h.set_trigger(delay=2, width=3, invert=1)
    await ClockCycles(dut.clk, 4)
    assert int(dut.uo_out.value) & 0x20
    await h.write(H.TRIG_CTRL, H.TRIG_INVERT | H.TRIG_FIRE)
    start = board.cycle
    await ClockCycles(dut.clk, 20)
    low = [c for c in range(start - 80, board.cycle - 1) if not board.uo_trace[c] & 0x20]
    assert len(low) == 4 and low == list(range(low[0], low[0] + 4))


@cocotb.test()
async def test_latency_measurement(dut):
    """firmware/latency.pio: a target's response time, measured to the clock cycle."""
    h, board = await setup(dut)
    STIM, RESP = 1, 6
    await load_firmware(h, "latency", sm=1, side_base=STIM, in_base=RESP)
    await h.set_pins(dirs=1 << STIM)
    await h.start(1)

    measured = {}
    for delay in (1, 2, 3, 10, 57, 1000, 4321):
        target = board.attach(Responder(STIM, RESP, delay))
        await h.tx(1, 0)
        measured[delay] = await h.rx_wait(1)
        board.detach(target)
        await ClockCycles(dut.clk, 8)
    # Exactly the target's delay plus the fixed offset documented in the program.
    assert measured == {delay: delay + 4 for delay in measured}, measured
    assert board.contention == []


@cocotb.test()
async def test_bus_sniffer(dut):
    """firmware/sniffer.pio: a passive logic analyser. The chip drives nothing;
    it records a timestamp and pin snapshot for every change on the bus."""
    h, board = await setup(dut)
    # The watched clock is slow enough that the SPI host drains each event before
    # the next arrives; a faster bus would overrun the 4-word FIFO, as the program
    # header warns.
    PIN, PERIOD = 0, 4000
    await load_firmware(h, "sniffer", sm=0, in_base=0)
    await h.start(0)

    clock = board.attach(PulseTrain(PIN, PERIOD, start_cycle=board.cycle + 50))

    events = []
    for _ in range(7):
        ts = await h.rx_wait(0)
        snap = await h.rx_wait(0)
        events.append((ts, snap & 1))  # the other pins float; only pin 0 is driven
    events = events[1:]  # drop the start-up transition when the clock begins driving

    # The chip never drove a pin: the pad level came only from the clock.
    assert int(dut.uio_oe.value) == 0
    assert board.contention == []

    # The snapshot toggles pin 0 on every event (the first kept edge may be
    # rising or falling, depending on where capture started relative to the wave).
    snaps = [s for _, s in events]
    assert snaps in ([i % 2 for i in range(len(snaps))], [(i + 1) % 2 for i in range(len(snaps))]), \
        f"snapshots should alternate: {snaps}"
    # Consecutive events are exactly a half-period apart, so the capture reproduces
    # the clock's timing to the cycle.
    deltas = [b - a for (a, _), (b, _) in zip(events, events[1:])]
    assert all(d == PERIOD // 2 for d in deltas), f"half-period should be {PERIOD // 2}: {deltas}"


@cocotb.test()
async def test_glitch_on_matching_byte(dut):
    """firmware/glitch_on_match.pio: fire the trigger exactly when a chosen byte
    is clocked onto a bus the chip only watches."""
    h, board = await setup(dut)
    SCK, DATA, TARGET = 0, 1, 0xA5
    await load_firmware(h, "glitch_on_match", sm=0, in_base=DATA)
    await h.set_trigger(delay=0, width=0)      # a one-cycle pulse per match
    await h.start(0)
    await h.tx(0, TARGET)                       # arm

    stream = [0x00, 0xA5, 0xFF, 0xA5, 0x5A, 0xA5, 0x3C]
    driver = board.attach(SpiBusDriver(SCK, DATA, stream, half=4, start_cycle=board.cycle + 20))
    board.uo_trace = {}
    await wait_until(dut, lambda: board.cycle >= driver.done_cycle + 40, what="the stream")

    # The chip stayed passive on the bus.
    assert int(dut.uio_oe.value) == 0
    assert board.contention == []

    # One trigger pulse per occurrence of the target byte.
    expected_matches = stream.count(TARGET)
    rising = [c for c in sorted(board.uo_trace)
              if board.uo_trace[c] & 0x20 and not board.uo_trace.get(c - 1, 0) & 0x20]
    assert len(rising) == expected_matches, f"{len(rising)} pulses, want {expected_matches}"

    # Each pulse lands just after the last bit of a matching byte, not a mismatching one.
    last_bit_cycle = {}
    for byte_index, bit_index, cycle in driver.bit_rising:
        if bit_index == 7:
            last_bit_cycle[byte_index] = cycle
    match_bytes = [i for i, b in enumerate(stream) if b == TARGET]
    for pulse_cycle, byte_index in zip(rising, match_bytes):
        gap = pulse_cycle - last_bit_cycle[byte_index]
        assert 0 < gap < 12, f"pulse {gap} cycles after byte {byte_index}'s last bit"

    # The host also got one notification per match.
    count = 0
    while not await h.status(0) & H.ST_RX_EMPTY:
        await h.rx(0)
        count += 1
    assert count == expected_matches


@cocotb.test()
async def test_armed_glitch_across_state_machines(dut):
    """firmware/armed_glitch_watch.pio + armed_glitch_fire.pio: one state machine
    watches the bus and raises a shared flag, the other waits on it and fires the
    trigger. The two programs share the instruction memory and one flag."""
    h, board = await setup(dut)
    SCK, DATA, TARGET = 0, 1, 0x3C
    watch = await load_firmware(h, "armed_glitch_watch", sm=0, origin=0, in_base=DATA)
    await load_firmware(h, "armed_glitch_fire", sm=1, origin=len(watch.words))
    await h.set_trigger(delay=0, width=0)
    await h.start(0, pc=0)
    await h.start(1, pc=len(watch.words))
    await h.tx(0, TARGET)                       # arm the watcher

    stream = [0x3C, 0x00, 0x3C, 0xFF, 0x3C]
    driver = board.attach(SpiBusDriver(SCK, DATA, stream, half=4, start_cycle=board.cycle + 20))
    board.uo_trace = {}
    await wait_until(dut, lambda: board.cycle >= driver.done_cycle + 60, what="the stream")

    assert int(dut.uio_oe.value) == 0, "both machines stayed passive on the bus"
    assert board.contention == []
    expected = stream.count(TARGET)
    rising = [c for c in sorted(board.uo_trace)
              if board.uo_trace[c] & 0x20 and not board.uo_trace.get(c - 1, 0) & 0x20]
    assert len(rising) == expected, f"{len(rising)} trigger pulses, want {expected}"
    # The firer notified the host once per match.
    count = 0
    while not await h.status(1) & H.ST_RX_EMPTY:
        await h.rx(1)
        count += 1
    assert count == expected


@cocotb.test()
async def test_two_protocols_at_once(dut):
    """UART on state machine 0 and SPI on state machine 1, sharing the instruction memory."""
    h, board = await setup(dut)
    TX, SCK, MOSI, MISO = 7, 0, 1, 2
    reply = [0x11, 0x22, 0x33, 0x44]

    uart = await load_firmware(h, "uart_tx", sm=0, origin=0, clkdiv=(5, 0), out_base=TX, side_base=TX)
    await load_firmware(h, "spi_master", sm=1, origin=len(uart.words), clkdiv=(7, 0),
                        out_base=MOSI, in_base=MISO, side_base=SCK)
    await h.set_pins(out=1 << TX, dirs=(1 << TX) | (1 << SCK) | (1 << MOSI))
    # This target has no chip select, so it is connected once SCK is driven low.
    rx = board.attach(UartReceiver(TX, bit_cycles=8 * 5))
    target = board.attach(SpiTarget(SCK, MOSI, MISO, reply=reply))
    await h.start(0, pc=0)
    await h.start(1, pc=len(uart.words))

    uart_bytes = [0x48, 0x69, 0x21, 0x0A]
    spi_bytes = [0xC0, 0xFF, 0xEE, 0x99]
    # Fill both TX FIFOs first so the two protocols really overlap on the pins.
    for u, s in zip(uart_bytes, spi_bytes):
        await h.tx(0, u)
        await h.tx(1, s << 24)
    received = [await h.rx_wait(1) & 0xFF for _ in spi_bytes]
    await wait_until(dut, lambda: len(rx.bytes) == len(uart_bytes), what="all UART bytes")

    assert rx.bytes == uart_bytes and rx.framing_errors == 0
    assert target.received == spi_bytes and received == reply
    assert board.contention == []


@cocotb.test()
async def test_status_pins(dut):
    """uo_out[4:1] mirror the FIFO flags so a host can wait on a pin instead of polling."""
    h, board = await setup(dut)
    # After the shift: bit 0 = SM0 RX has data, 1 = SM0 TX has room, 2 and 3 the same for SM1.
    assert int(dut.uo_out.value) >> 1 == 0b1010          # both TX FIFOs have room, no RX data
    assert int(dut.uo_out.value) >> 5 == 0               # trigger idle, top two outputs tied low
    for i in range(4):
        await h.tx(1, i)
    assert int(dut.uo_out.value) >> 1 == 0b0010          # state machine 1's TX FIFO is full
    await h.load_program(asm.assemble("pull block" + chr(10) + "mov isr, osr" + chr(10) + "push block").words)
    await h.configure(1, wrap_top=2)
    await h.enable(1)
    await ClockCycles(dut.clk, 40)
    assert int(dut.uo_out.value) >> 1 == 0b1110          # RX data waiting, TX has room again
    assert [await h.rx(1) for _ in range(4)] == [0, 1, 2, 3]
    assert int(dut.uo_out.value) >> 1 == 0b1010
