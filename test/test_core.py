# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_core (two state machines, shared memory and pins), driven
through its register bus."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

import asm
import host as H
from host import BusTransport, Host


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.ena.value = 1
    dut.pad_in.value = 0
    h = Host(BusTransport(dut))
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    return h


async def settle(dut, cycles=1):
    """Let `cycles` clock edges pass, then return once their effects are visible."""
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    await Timer(1, unit="ns")


async def load(h, source, sm, origin=0, clkdiv=(1, 0), **pins):
    prog = asm.assemble(source, origin=origin)
    await h.load_program(prog.words, origin)
    await h.configure(sm, clkdiv=clkdiv, **dict(prog.sm_config(), **pins))
    return prog


TOGGLE = """
    set pins, 1
    set pins, 0
"""


@cocotb.test()
async def test_reset_values_and_id(dut):
    h = await start(dut)
    assert await h.read(H.ID) == H.ID_VALUE
    for base in H.SM_BASE:
        assert await h.read(base + H.CTRL) == 0
        assert await h.read(base + H.STATUS) == H.ST_TX_EMPTY | H.ST_RX_EMPTY
        assert await h.read(base + H.FLEVEL) == 0
        assert list(await h.read(base + H.CLKDIV_FRAC, 3)) == [0, 1, 0]
        assert list(await h.read(base + H.WRAP_BOTTOM, 2)) == [0, 31]
    assert list(await h.read(H.PIN_OUT, 3)) == [0, 0, 0]
    assert int(dut.pad_oe.value) == 0
    assert int(dut.rx_ready.value) == 0 and int(dut.tx_ready.value) == 3


@cocotb.test()
async def test_registers_read_back(dut):
    h = await start(dut)
    rng = random.Random(9)
    # (register, writable bits)
    regs = [(H.CLKDIV_FRAC, 0xFF), (H.CLKDIV_INTL, 0xFF), (H.CLKDIV_INTH, 0xFF),
            (H.WRAP_BOTTOM, 0x1F), (H.WRAP_TOP, 0x1F), (H.PINCTRL0, 0x3F), (H.PINCTRL1, 0x3F),
            (H.PINCTRL2, 0x3F), (H.EXECCTRL, 0xFF), (H.PUSH_THRESH, 0x1F), (H.PULL_THRESH, 0x1F),
            (H.PC, 0x1F)]
    written = {}
    for base in H.SM_BASE:
        for reg, mask in regs:
            value = rng.getrandbits(8)
            await h.write(base + reg, value)
            written[base + reg] = value & mask
    for addr in (H.PIN_OUT, H.PIN_DIR, H.PIN_OD, H.TRIG_DELAY, H.TRIG_DELAY + 1, H.TRIG_WIDTH):
        written[addr] = rng.getrandbits(8)
        await h.write(addr, written[addr])
    # The two state machines' registers are separate, and nothing aliases.
    for addr, value in written.items():
        assert await h.read(addr) == value, f"register {addr:#04x}"
    # Unused addresses read as zero.
    for addr in (0x0F, 0x1D, 0x1F, 0x2F, 0x3F):
        assert await h.read(addr) == 0, f"address {addr:#04x}"


@cocotb.test()
async def test_program_memory_read_back(dut):
    h = await start(dut)
    rng = random.Random(10)
    words = [rng.getrandbits(16) for _ in range(32)]
    await h.load_program(words)
    assert await h.read_program(32) == words
    await h.load_program([0x1234, 0x5678], origin=30)
    assert await h.read_program(4, origin=28) == words[28:30] + [0x1234, 0x5678]


@cocotb.test()
async def test_scheduled_glitch_trigger(dut):
    """The trigger fires on its own when the cycle timer reaches the match value:
    a glitch scheduled a fixed number of cycles after `tstart`."""
    h = await start(dut)
    MATCH = 50
    # A program that starts the timer once, then spins.
    await load(h, "tstart\njmp 1", sm=0)
    await h.set_trigger(delay=0, width=0, match=MATCH)
    assert await h.read(H.TRIG_CTRL) & H.TRIG_MATCH_EN
    assert list(await h.read(H.TRIG_MATCH, 2)) == [MATCH, 0]
    await h.start(0)

    # tstart runs on the first tick; the timer then counts 1,2,... and the
    # trigger fires for one cycle when it reaches MATCH.
    highs = []
    for cycle in range(MATCH + 30):
        await settle(dut)
        if int(dut.trig_out.value):
            highs.append(cycle)
    assert len(highs) == 1, f"exactly one trigger pulse, got {len(highs)}"

    # With the match disabled, nothing fires.
    await h.stop(0)
    await h.set_trigger(delay=0, width=0)      # match left off
    await h.start(0)
    for _ in range(MATCH + 30):
        await settle(dut)
        assert int(dut.trig_out.value) == 0, "no pulse when the match is disabled"


@cocotb.test()
async def test_shared_flags(dut):
    """The host and both state machines share four flags: one machine raises a
    flag, the other waits on it, and the host can read and write them."""
    h = await start(dut)
    # Host read/write of the flags register.
    assert await h.read(H.FLAGS) == 0
    await h.write(H.FLAGS, 0b1010)
    await settle(dut)
    assert await h.read(H.FLAGS) == 0b1010
    await h.write(H.FLAGS, 0b0000)
    await settle(dut)
    assert await h.read(H.FLAGS) == 0

    # SM0 waits for pin 0 high, then raises flag 1. SM1 waits on flag 1, then
    # drives pin 7. Neither could do this alone.
    await load(h, "wait 1 gpio 0\nirq 1\njmp 2", sm=0)
    await load(h, "wait 1 irq 1\nset pins, 1\njmp 2", sm=1, origin=3, set_base=7, set_count=1)
    await h.set_pins(dirs=1 << 7)
    await h.start(0)
    await h.start(1, pc=3)

    await settle(dut, 5)
    assert int(dut.pad_out.value) & 0x80 == 0, "SM1 still waiting on the flag"
    assert await h.read(H.FLAGS) == 0
    dut.pad_in.value = 1                      # pin 0 goes high
    await settle(dut, 6)                      # SM0 sees it (2-flop sync), raises flag 1
    assert await h.read(H.FLAGS) & 0b0010
    assert int(dut.pad_out.value) & 0x80, "SM1 reacted to the flag"


@cocotb.test()
async def test_flag_set_beats_host_clear(dut):
    """A state machine setting a flag wins over the host clearing it on the same cycle."""
    h = await start(dut)
    await load(h, "irq 0\njmp 0", sm=0)       # SM0 sets flag 0 every tick
    await h.start(0)
    await settle(dut, 4)
    assert await h.read(H.FLAGS) & 1, "flag 0 stays set while SM0 raises it"
    # Host writes 0: the set still wins, so the flag reads 1.
    await h.write(H.FLAGS, 0)
    await settle(dut, 2)
    assert await h.read(H.FLAGS) & 1
    # Stop SM0, then the host can clear it.
    await h.stop(0)
    await h.write(H.FLAGS, 0)
    await settle(dut, 2)
    assert await h.read(H.FLAGS) == 0


@cocotb.test()
async def test_two_state_machines_run_at_once(dut):
    h = await start(dut)
    await load(h, TOGGLE, sm=0, set_base=0, set_count=1)
    # Same program, reused by the second machine on another pin at a third of the rate.
    await h.configure(1, clkdiv=(3, 0), wrap_top=1, set_base=5, set_count=1)
    await h.set_pins(dirs=0b0010_0001)
    await h.start(0)
    await h.start(1)
    await settle(dut, 4)
    trace = []
    for _ in range(24):
        trace.append(int(dut.pad_out.value))
        await settle(dut)
    pin0 = [v & 1 for v in trace]
    pin5 = [(v >> 5) & 1 for v in trace]
    assert all(a != b for a, b in zip(pin0, pin0[1:])), "pin 0 toggles every clock"
    runs = "".join(map(str, pin5)).strip("0").strip("1")
    assert set(runs.replace("111", "H").replace("000", "L")) <= {"H", "L"}, (
        f"pin 5 toggles every third clock, got {pin5}")
    assert int(dut.pad_oe.value) == 0b0010_0001


@cocotb.test()
async def test_pin_write_priority(dut):
    """On the same clock edge: state machine 1 beats state machine 0, which beats the host."""
    h = await start(dut)
    await load(h, "set pins, 0b01", sm=0, set_base=2, set_count=2)             # pins 3:2 <- 01
    await load(h, "set pins, 1", sm=1, origin=1, set_base=3, set_count=1)      # pin 3 <- 1
    await h.start(0)
    await h.start(1)
    await settle(dut, 3)
    assert int(dut.pad_out.value) & 0b1100 == 0b1100
    # The host writes zeros while both machines keep writing: only free pins change.
    await h.write(H.PIN_OUT, 0xF0)
    await settle(dut)
    assert int(dut.pad_out.value) == 0xFC
    # With state machine 1 stopped, state machine 0 owns pin 3 again.
    await h.stop(1)
    await settle(dut, 2)
    assert int(dut.pad_out.value) == 0xF4
    await h.stop(0)
    await h.write(H.PIN_OUT, 0x00)
    await settle(dut)
    assert int(dut.pad_out.value) == 0x00


@cocotb.test()
async def test_fifo_path_and_flags(dut):
    h = await start(dut)
    await load(h, """
        pull block
        mov x, ::osr
        mov isr, x
        push block
    """, sm=1)
    words = [0x0000_0001, 0x8000_0000, 0x1234_5678, 0xF0F0_A5A5]
    # Disabled: the words queue up in the TX FIFO.
    for i, w in enumerate(words):
        await h.tx(1, w)
        assert await h.read(H.SM_BASE[1] + H.FLEVEL) == i + 1
    assert await h.status(1) & (H.ST_TX_FULL | H.ST_RX_EMPTY) == H.ST_TX_FULL | H.ST_RX_EMPTY
    assert int(dut.tx_ready.value) == 0b01 and int(dut.rx_ready.value) == 0
    # Only the top byte pushes: three low bytes into a full FIFO change nothing.
    await h.write(H.SM_BASE[1] + H.TXF, 1, 2, 3)
    assert await h.read(H.SM_BASE[1] + H.FLEVEL) == 4

    await h.write(H.SM_BASE[1] + H.CTRL, H.CTRL_EN)
    await settle(dut, 30)
    assert await h.read(H.SM_BASE[1] + H.FLEVEL) == 0x40
    status = await h.status(1)
    assert status & H.ST_RX_FULL and status & H.ST_TX_EMPTY and status & H.ST_STALLED
    assert int(dut.rx_ready.value) == 0b10 and int(dut.tx_ready.value) == 0b11
    # Reading the three low bytes does not pop.
    assert list(await h.read(H.SM_BASE[1] + H.RXF, 3)) == [0, 0, 0]
    assert await h.read(H.SM_BASE[1] + H.FLEVEL) == 0x40
    for w in words:
        assert await h.rx(1) == int(f"{w:032b}"[::-1], 2)
    assert await h.status(1) & H.ST_RX_EMPTY
    # State machine 0's FIFOs were never involved.
    assert await h.read(H.SM_BASE[0] + H.FLEVEL) == 0


@cocotb.test()
async def test_fifo_clear_and_restart(dut):
    h = await start(dut)
    await load(h, ".word 0xC080\nin null, 8\npush noblock", sm=0)   # illegal, then overrun
    await h.tx(0, 1)
    await h.tx(0, 2)
    await h.write(H.SM_BASE[0] + H.CTRL, H.CTRL_EN)
    await settle(dut, 40)
    status = await h.status(0)
    assert status & H.ST_ILLEGAL and status & H.ST_RXOVER and status & H.ST_RX_FULL
    await h.write(H.SM_BASE[0] + H.CTRL, H.CTRL_RESTART | H.CTRL_FIFO_CLEAR)  # also disables
    assert await h.status(0) == H.ST_TX_EMPTY | H.ST_RX_EMPTY
    assert await h.read(H.SM_BASE[0] + H.CTRL) == 0


@cocotb.test()
async def test_open_drain_pins(dut):
    h = await start(dut)
    await h.set_pins(out=0b1010, dirs=0b1111, od=0b0011)
    await settle(dut)
    # Pins 1:0 are open drain: pin 1 holds a 1, so it is released; pin 0 drives low.
    assert int(dut.pad_oe.value) == 0b1101
    assert int(dut.pad_out.value) == 0b1010
    await h.write(H.PIN_OUT, 0b0101)
    await settle(dut)
    assert int(dut.pad_oe.value) == 0b1110


@cocotb.test()
async def test_input_latency_is_two_clocks(dut):
    """docs/isa.md 1.3: an instruction executing on edge E sees the pad as sampled at E - 2."""
    h = await start(dut)
    await load(h, "wait 1 gpio 3\nset pins, 1\nwait 0 gpio 3", sm=0, set_base=0, set_count=1)
    await h.start(0)
    await settle(dut, 3)
    assert await h.status(0) & H.ST_STALLED
    await RisingEdge(dut.clk)        # edge E0
    dut.pad_in.value = 0b1000        # sampled at E0+1; the wait runs at E0+3, the set at E0+4
    await settle(dut, 3)
    assert int(dut.pad_out.value) & 1 == 0
    await settle(dut, 1)
    assert int(dut.pad_out.value) & 1 == 1
    assert await h.read(H.PIN_IN) == 0b1000


@cocotb.test()
async def test_ena_low_freezes_everything(dut):
    h = await start(dut)
    await load(h, TOGGLE, sm=0, set_base=0, set_count=1)
    await h.set_pins(dirs=1)
    await h.start(0)
    await settle(dut, 4)
    dut.ena.value = 0
    await settle(dut, 2)
    frozen = int(dut.pad_out.value)
    for _ in range(10):
        await settle(dut)
        assert int(dut.pad_out.value) == frozen
        assert int(dut.pad_oe.value) == 0
    dut.ena.value = 1
    await settle(dut, 1)
    assert int(dut.pad_oe.value) == 1
    seen = set()
    for _ in range(4):
        await settle(dut)
        seen.add(int(dut.pad_out.value) & 1)
    assert seen == {0, 1}, "the state machine runs again"


@cocotb.test()
async def test_host_can_move_the_program_counter(dut):
    h = await start(dut)
    await load(h, "jmp 0\njmp 1\nset pins, 1\njmp 3", sm=0, set_base=7, set_count=1)
    await h.start(0, pc=1)
    await settle(dut, 3)
    assert await h.read(H.SM_BASE[0] + H.PC) == 1
    assert int(dut.pad_out.value) == 0
    await h.write(H.SM_BASE[0] + H.PC, 2)   # while running
    await settle(dut, 3)
    assert await h.read(H.SM_BASE[0] + H.PC) == 3
    assert int(dut.pad_out.value) == 0x80
