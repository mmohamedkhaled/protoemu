# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_sm.

Every test runs the RTL in lock step with the reference model in
models/isa_model.py and compares all architectural state and all outputs after
every clock cycle. The directed tests also assert hand-computed expectations,
which check the model against docs/isa.md; the random tests check the RTL
against the model over random programs, configurations and stimulus.
"""

import dataclasses
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from models.isa_model import SmConfig, SmModel, TickResult, decode_op

# Opcodes and operand codes, for hand-encoding the directed programs.
JMP, WAIT, IN, OUT, PP, MOV, SET = 0b000, 0b001, 0b010, 0b011, 0b100, 0b101, 0b111
PINS, X, Y, NULL, PINDIRS, PC, ISR, OSR = 0, 1, 2, 3, 4, 5, 6, 7
TIMER = 5            # as a mov source
TRIG, TSTART = 0xC000, 0xC020
NOP = 0xA042  # mov y, y


def ins(opcode, hi, lo, fld=0):
    return (opcode << 13) | (fld << 8) | (hi << 5) | lo


PUSH_BLOCK, PUSH_NOBLOCK, PUSH_IFFULL = 0x8020, 0x8000, 0x8060
PULL_BLOCK, PULL_NOBLOCK = 0x80A0, 0x8080


def bits_of(writes):
    """Turn a {pin: bit} dictionary into (mask, value)."""
    mask = sum(1 << p for p in writes)
    value = sum(b << p for p, b in writes.items())
    return mask, value


class Bench:
    def __init__(self, dut, cfg=None, program=()):
        self.dut = dut
        self.model = SmModel(cfg or SmConfig())
        self.mem = (list(program) + [NOP] * 32)[:32]
        self.cycles = 0

    async def reset(self):
        dut = self.dut
        cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
        for f in dataclasses.fields(SmConfig):
            getattr(dut, f.name).value = getattr(self.model.cfg, f.name)
        for name in ("tick", "restart", "pc_wr", "pc_wdata", "instr", "pins_in",
                     "tx_data", "tx_empty", "rx_full", "timer", "flags_in"):
            getattr(dut, name).value = 0
        dut.rst_n.value = 0
        for _ in range(3):
            await RisingEdge(dut.clk)
        dut.rst_n.value = 1
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        self.check_state("reset")

    def check_state(self, what):
        dut, m = self.dut, self.model
        got = dict(pc=int(dut.pc.value), x=int(dut.x.value), y=int(dut.y.value),
                   isr=int(dut.isr.value), isr_count=int(dut.isr_count.value),
                   osr=int(dut.osr.value), osr_count=int(dut.osr_count.value),
                   delay=int(dut.delay_cnt.value),
                   illegal=int(dut.flag_illegal.value), rxover=int(dut.flag_rxover.value))
        want = dict(pc=m.pc, x=m.x, y=m.y, isr=m.isr, isr_count=m.isr_count, osr=m.osr,
                    osr_count=m.osr_count, delay=m.delay, illegal=int(m.illegal),
                    rxover=int(m.rxover))
        diff = {k: (hex(got[k]), hex(want[k])) for k in got if got[k] != want[k]}
        assert not diff, f"cycle {self.cycles}, {what}: (rtl, model) differ in {diff}"

    async def cycle(self, tick=1, pins=0, tx_empty=1, tx_data=0, rx_full=0, restart=0, pc_wr=None,
                    timer=0, flags=0):
        """One clock cycle. Returns what the model says the tick did."""
        dut, m = self.dut, self.model
        instr = self.mem[m.pc]
        dut.instr.value = instr
        dut.tick.value = tick
        dut.pins_in.value = pins
        dut.tx_empty.value = tx_empty
        dut.tx_data.value = tx_data
        dut.rx_full.value = rx_full
        dut.timer.value = timer
        dut.flags_in.value = flags
        dut.restart.value = restart
        dut.pc_wr.value = pc_wr is not None
        dut.pc_wdata.value = pc_wr or 0
        await Timer(1, unit="ns")

        res = m.step(instr, pins, tx_empty, tx_data, rx_full, timer, flags) if tick else TickResult()
        what = f"instr {instr:#06x} ({decode_op(instr)}) at pc {int(dut.pc.value)}"
        for name, writes in (("out", res.out_writes), ("dir", res.dir_writes)):
            mask, value = bits_of(writes)
            got_mask = int(getattr(dut, name + "_mask").value)
            got_val = int(getattr(dut, name + "_val").value) & got_mask
            assert (got_mask, got_val) == (mask, value), (
                f"cycle {self.cycles}, {what}: {name} write (mask, value) is "
                f"({got_mask:#04x}, {got_val:#04x}), model ({mask:#04x}, {value:#04x})")
        assert int(dut.tx_pop.value) == res.tx_pop, f"cycle {self.cycles}, {what}: tx_pop"
        assert int(dut.rx_push.value) == res.rx_push, f"cycle {self.cycles}, {what}: rx_push"
        if res.rx_push:
            assert int(dut.rx_data.value) == res.rx_data, f"cycle {self.cycles}, {what}: rx_data"
        assert int(dut.stalled.value) == res.stalled, f"cycle {self.cycles}, {what}: stalled"
        assert int(dut.trig_fire.value) == res.trig, f"cycle {self.cycles}, {what}: trig_fire"
        assert int(dut.timer_reset.value) == res.tstart, f"cycle {self.cycles}, {what}: timer_reset"
        assert int(dut.flag_set.value) == res.flag_set, f"cycle {self.cycles}, {what}: flag_set"
        assert int(dut.flag_clr.value) == res.flag_clr, f"cycle {self.cycles}, {what}: flag_clr"

        if pc_wr is not None:
            m.pc = pc_wr
        if restart:
            m.restart()
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        self.cycles += 1
        self.check_state(what)
        return res

    async def ticks(self, n, **kwargs):
        return [await self.cycle(**kwargs) for _ in range(n)]


# ---------------------------------------------------------------- directed


@cocotb.test()
async def test_set_and_delay(dut):
    """The next instruction runs exactly delay + 1 ticks after this one."""
    cfg = SmConfig(set_base=2, set_count=3, wrap_top=1)
    b = Bench(dut, cfg, [ins(SET, PINS, 0b101, fld=3), ins(SET, PINS, 0b010)])
    await b.reset()
    results = await b.ticks(12)
    writes = [(i, r.out_writes) for i, r in enumerate(results) if r.out_writes]
    # set [3] at tick 0, the second set at tick 4, wrap to the first at tick 5.
    assert [i for i, _ in writes] == [0, 4, 5, 9, 10]
    assert writes[0][1] == {2: 1, 3: 0, 4: 1}
    assert writes[1][1] == {2: 0, 3: 1, 4: 0}
    # Cycles without a tick do nothing and do not count.
    for _ in range(5):
        r = await b.cycle(tick=0)
        assert not r.out_writes
    res = await b.ticks(3)  # ticks 12 and 13 finish the delay, tick 14 executes
    assert [bool(r.out_writes) for r in res] == [False, False, True]
    assert res[2].out_writes == {2: 0, 3: 1, 4: 0}


@cocotb.test()
async def test_jmp_x_dec_loop(dut):
    prog = [ins(SET, X, 3), ins(JMP, 0b010, 1), ins(SET, Y, 9), ins(JMP, 0b000, 3)]
    b = Bench(dut, SmConfig(), prog)
    await b.reset()
    await b.ticks(1)
    assert b.model.x == 3
    await b.ticks(4)  # taken three times, then falls through with X wrapped
    assert b.model.pc == 2 and b.model.x == 0xFFFFFFFF
    await b.ticks(1)
    assert b.model.y == 9


@cocotb.test()
async def test_jmp_conditions(dut):
    # Each jmp skips a `set y` when taken; Y records which ones fell through.
    for cond, x, y, pins, taken in [
        (0b001, 0, 5, 0, True), (0b001, 1, 5, 0, False),
        (0b011, 5, 0, 0, True), (0b011, 5, 1, 0, False),
        (0b100, 5, 2, 0, True), (0b100, 5, 0, 0, False),
        (0b101, 4, 5, 0, True), (0b101, 5, 5, 0, False),
        (0b110, 0, 0, 0x20, True), (0b110, 0, 0, 0xDF, False),
        (0b111, 0, 0, 0, False),  # OSR is empty after reset
    ]:
        prog = [ins(SET, X, x), ins(SET, Y, y), ins(JMP, cond, 4), ins(SET, X, 31), NOP]
        b = Bench(dut, SmConfig(jmp_pin=5), prog)
        await b.reset()
        await b.ticks(4, pins=pins)
        assert (b.model.x != 31) == taken, f"cond {cond:03b} x={x} y={y} pins={pins:#x}"


@cocotb.test()
async def test_wait_and_side_set_while_stalled(dut):
    """Side-set applies from the first stalled tick; the wait itself has no effect."""
    cfg = SmConfig(side_base=6, side_cnt=1, in_base=3)
    prog = [ins(WAIT, 0b100, 1, fld=0b10000),   # wait 1 gpio 1, side 1
            ins(WAIT, 0b001, 2, fld=0b00000),   # wait 0 pin 2 (gpio 5), side 0
            NOP]
    b = Bench(dut, cfg, prog)
    await b.reset()
    for _ in range(3):
        r = await b.cycle(pins=0b0000_0000)
        assert r.stalled and r.out_writes == {6: 1}
    r = await b.cycle(pins=0b0000_0010)
    assert r.executed and b.model.pc == 1
    for _ in range(2):
        r = await b.cycle(pins=0b0010_0000)
        assert r.stalled and r.out_writes == {6: 0}
    r = await b.cycle(pins=0b1101_1111)
    assert r.executed and b.model.pc == 2


@cocotb.test()
async def test_out_directions_and_destinations(dut):
    word = 0xC0DE_F00D
    for out_dir in (0, 1):
        cfg = SmConfig(out_dir=out_dir, out_base=6)
        prog = [PULL_BLOCK, ins(OUT, X, 4), ins(OUT, Y, 12), ins(OUT, PINS, 4),
                ins(OUT, PINDIRS, 2), ins(OUT, NULL, 5), ins(OUT, PC, 5)]
        b = Bench(dut, cfg, prog)
        await b.reset()
        r = await b.cycle(tx_empty=0, tx_data=word)
        assert r.tx_pop
        bits = f"{word:032b}"
        stream = bits[::-1] if out_dir else bits  # in the order the bits leave

        def field(start, n):
            chunk = stream[start:start + n]
            return int(chunk[::-1] if out_dir else chunk, 2)

        res = await b.ticks(6)
        assert b.model.x == field(0, 4)
        assert b.model.y == field(4, 12)
        v = field(16, 4)
        assert res[2].out_writes == {6: v & 1, 7: (v >> 1) & 1, 0: (v >> 2) & 1, 1: (v >> 3) & 1}
        v = field(20, 2)
        assert res[3].dir_writes == {6: v & 1, 7: v >> 1}
        assert b.model.pc == field(27, 5)
        assert b.model.osr == 0 and b.model.osr_count == 32


@cocotb.test()
async def test_in_directions_and_push(dut):
    for in_dir in (0, 1):
        cfg = SmConfig(in_dir=in_dir, in_base=4)
        prog = [ins(SET, X, 0b10110), ins(IN, X, 5), ins(IN, PINS, 3), ins(IN, NULL, 2), PUSH_BLOCK]
        b = Bench(dut, cfg, prog)
        await b.reset()
        await b.ticks(4, pins=0b0101_0000)  # pins 4..6 read 0b101
        if in_dir:
            want = (0b00 << 30) | (0b101 << 27) | (0b10110 << 22)
        else:
            want = (0b10110 << 5) | (0b101 << 2)
        assert b.model.isr == want and b.model.isr_count == 10
        # A blocking push stalls while the FIFO is full, then pushes and clears.
        r = await b.cycle(rx_full=1)
        assert r.stalled and not r.rx_push
        r = await b.cycle(rx_full=0)
        assert r.rx_push and r.rx_data == want
        assert b.model.isr == 0 and b.model.isr_count == 0


@cocotb.test()
async def test_push_iffull_and_overrun(dut):
    cfg = SmConfig(push_thresh=8)
    prog = [ins(IN, NULL, 4), PUSH_IFFULL, ins(IN, NULL, 4), PUSH_IFFULL, ins(IN, NULL, 1),
            PUSH_NOBLOCK]
    b = Bench(dut, cfg, prog)
    await b.reset()
    res = await b.ticks(4)
    assert [r.rx_push for r in res] == [False, False, False, True]
    assert not b.model.rxover
    await b.cycle()
    r = await b.cycle(rx_full=1)  # non-blocking push into a full FIFO drops the data
    assert r.executed and not r.rx_push
    assert b.model.rxover and b.model.isr_count == 0
    await b.cycle(restart=1, tick=0)
    assert not b.model.rxover


@cocotb.test()
async def test_pull_variants(dut):
    prog = [ins(SET, X, 21), PULL_NOBLOCK, PULL_BLOCK, ins(OUT, NULL, 32), 0x80E0, 0x80E0]
    b = Bench(dut, SmConfig(), prog)
    await b.reset()
    await b.ticks(2)
    assert b.model.osr == 21 and b.model.osr_count == 0  # empty FIFO: OSR <- X
    assert (await b.cycle()).stalled
    r = await b.cycle(tx_empty=0, tx_data=0x1234_5678)
    assert r.tx_pop and b.model.osr == 0x1234_5678
    await b.cycle()
    # `pull ifempty` (0x80E0) acts only when the OSR is empty.
    r = await b.cycle(tx_empty=0, tx_data=7)
    assert r.tx_pop and b.model.osr == 7
    r = await b.cycle(tx_empty=0, tx_data=8)
    assert not r.tx_pop and b.model.osr == 7


@cocotb.test()
async def test_autopull_stream_has_no_gap(dut):
    """`out pins, 1` alone, with autopull, emits one bit per tick across word boundaries."""
    cfg = SmConfig(autopull=1, pull_thresh=8, out_dir=1, wrap_top=0)
    b = Bench(dut, cfg, [ins(OUT, PINS, 1)])
    await b.reset()
    data = [0xA5, 0x3C, 0xFF, 0x00, 0x81]
    r = await b.cycle(tx_empty=0, tx_data=data[0])
    assert r.stalled and r.tx_pop, "the first tick loads the OSR"
    sent = []
    for i in range(len(data)):
        last = i == len(data) - 1
        for bit in range(8):
            r = await b.cycle(tx_empty=last, tx_data=0 if last else data[i + 1])
            assert r.executed
            sent.append(r.out_writes[0])
            assert r.tx_pop == (bit == 7 and not last), "refill on the tick that uses the last bit"
    want = [(byte >> i) & 1 for byte in data for i in range(8)]
    assert sent == want
    assert (await b.cycle(tx_empty=1)).stalled, "FIFO empty: the next OUT stalls"


@cocotb.test()
async def test_autopull_empty_fifo_stalls(dut):
    cfg = SmConfig(autopull=1, pull_thresh=4, wrap_top=0)
    b = Bench(dut, cfg, [ins(OUT, X, 4)])
    await b.reset()
    for _ in range(3):
        assert (await b.cycle(tx_empty=1)).stalled
    r = await b.cycle(tx_empty=0, tx_data=0x9000_0000)
    assert r.stalled and r.tx_pop
    r = await b.cycle(tx_empty=1)
    assert r.executed and b.model.x == 9
    assert (await b.cycle(tx_empty=1)).stalled


@cocotb.test()
async def test_autopush(dut):
    cfg = SmConfig(autopush=1, push_thresh=8, in_dir=1, wrap_top=0)
    b = Bench(dut, cfg, [ins(IN, PINS, 1)])
    await b.reset()
    byte = 0b1100_1010
    for i in range(7):
        r = await b.cycle(pins=(byte >> i) & 1)
        assert not r.rx_push
    # FIFO full on the eighth bit: the IN does not execute at all, and the pin
    # is sampled on the tick it finally does.
    r = await b.cycle(pins=0, rx_full=1)
    assert r.stalled and b.model.isr_count == 7
    r = await b.cycle(pins=1, rx_full=0)
    assert r.rx_push and r.rx_data == byte << 24
    assert b.model.isr == 0 and b.model.isr_count == 0


@cocotb.test()
async def test_mov(dut):
    prog = [ins(SET, X, 0b10011),
            ins(MOV, Y, (0b01 << 3) | X),      # y = ~x
            ins(MOV, ISR, (0b10 << 3) | X),    # isr = reverse(x)
            ins(MOV, OSR, ISR),                # osr = isr
            ins(MOV, X, PINS),                 # x = pins
            ins(MOV, PC, Y)]                   # pc = y
    b = Bench(dut, SmConfig(in_base=1), prog)
    await b.reset()
    await b.ticks(6, pins=0b1000_0110)
    m = b.model
    assert m.y == 0xFFFF_FFEC
    assert m.isr == 0xC800_0000 and m.isr_count == 0
    assert m.osr == 0xC800_0000 and m.osr_count == 0
    assert m.x == 0b0100_0011
    assert m.pc == 0xC


@cocotb.test()
async def test_side_set_modes(dut):
    # Optional side-set on directions, two bits, overriding SET on a shared pin.
    cfg = SmConfig(side_base=1, side_cnt=2, side_opt=1, side_pindir=1, set_base=0, set_count=3)
    prog = [ins(SET, PINDIRS, 0b111, fld=0b11001),  # side 2, delay 1
            ins(SET, PINDIRS, 0b000, fld=0b00011),  # no side-set, delay 3
            ins(SET, PINS, 0b111, fld=0b10100)]     # side 1
    b = Bench(dut, cfg, prog)
    await b.reset()
    res = await b.ticks(8)
    assert res[0].dir_writes == {0: 1, 1: 0, 2: 1}
    assert not res[1].dir_writes
    assert res[2].dir_writes == {0: 0, 1: 0, 2: 0}
    assert all(not r.dir_writes and not r.out_writes for r in res[3:6])
    assert res[6].out_writes == {0: 1, 1: 1, 2: 1} and res[6].dir_writes == {1: 1, 2: 0}


@cocotb.test()
async def test_illegal_instruction_is_a_nop(dut):
    cfg = SmConfig(side_base=0, side_cnt=1)
    b = Bench(dut, cfg, [ins(SET, X, 5), 0xC080 | (0b10010 << 8), ins(SET, Y, 6)])
    await b.reset()
    res = await b.ticks(5)
    assert b.model.illegal and b.model.x == 5 and b.model.y == 6
    assert res[1].out_writes == {0: 1} and not res[2].executed  # side-set and delay honoured


@cocotb.test()
async def test_trig_tstart_and_timer(dut):
    """`trig` and `tstart` strobe for exactly the tick they execute on; `mov` reads the timer."""
    cfg = SmConfig(side_base=0, side_cnt=1, side_opt=1)
    prog = [TRIG | (0b00011 << 8),              # trig [3]
            TSTART | (0b11000 << 8),            # tstart side 1
            ins(MOV, X, TIMER),
            ins(MOV, ISR, (0b01 << 3) | TIMER), # isr = ~timer
            ins(WAIT, 0b100, 0),                # wait 1 gpio 0
            TRIG]
    b = Bench(dut, cfg, prog)
    await b.reset()
    res = await b.ticks(5)                      # trig, then three delay ticks, then tstart
    assert [r.trig for r in res] == [True, False, False, False, False]
    assert [r.tstart for r in res] == [False, False, False, False, True]
    assert res[4].out_writes == {0: 1}, "side-set works on the new instructions"
    await b.cycle(timer=0x1234_5678)
    assert b.model.x == 0x1234_5678
    await b.cycle(timer=0x0000_00FF)
    assert b.model.isr == 0xFFFF_FF00 and b.model.isr_count == 0
    for _ in range(3):                          # a stalled wait never fires the trigger
        r = await b.cycle(pins=0)
        assert r.stalled and not r.trig
    await b.cycle(pins=1)
    assert (await b.cycle()).trig
    assert not b.model.illegal


@cocotb.test()
async def test_irq_flags(dut):
    """`irq`/`irqclr` strobe flag_set/flag_clr for one tick; `wait irq` stalls on a flag."""
    IRQSET, IRQCLR = 0xC040, 0xC060
    cfg = SmConfig()
    prog = [IRQSET | 2,                         # irq 2         -> flag_set bit 2
            IRQCLR | 2,                         # irqclr 2      -> flag_clr bit 2
            IRQSET | 0 | (0b00011 << 8),        # irq 0 [3]     (delay honoured)
            0x2000 | (1 << 7) | (0b10 << 5) | 1,  # wait 1 irq 1
            ins(SET, X, 5)]
    b = Bench(dut, cfg, prog)
    await b.reset()
    r = await b.cycle()
    assert r.flag_set == 0b0100 and r.flag_clr == 0
    r = await b.cycle()
    assert r.flag_set == 0 and r.flag_clr == 0b0100
    res = await b.ticks(4)                       # irq 0, then three delay ticks
    assert [r.flag_set for r in res] == [0b0001, 0, 0, 0]
    # Now at `wait 1 irq 1`: stalls until flag 1 is high in flags_in.
    for _ in range(3):
        r = await b.cycle(flags=0b0000)
        assert r.stalled and r.flag_set == 0
    r = await b.cycle(flags=0b0010)
    assert r.executed
    await b.cycle(flags=0b0010)
    assert b.model.x == 5 and not b.model.illegal


@cocotb.test()
async def test_host_pc_write_and_wrap(dut):
    cfg = SmConfig(wrap_bottom=10, wrap_top=12)
    b = Bench(dut, cfg, [])
    await b.reset()
    await b.cycle(tick=0, pc_wr=11)
    assert b.model.pc == 11
    await b.ticks(2)
    assert b.model.pc == 10
    await b.cycle(pc_wr=31)  # a host write wins over the instruction's own update
    assert b.model.pc == 31
    await b.cycle()
    assert b.model.pc == 0


# ---------------------------------------------------------------- random


def random_instruction(rng):
    """Mostly legal instructions, with every field random."""
    roll = rng.random()
    fld = rng.getrandbits(5) if rng.random() < 0.5 else 0
    if roll < 0.05:
        return rng.getrandbits(16)
    op = rng.choice(["jmp", "wait", "in", "out", "push", "pull", "mov", "set", "trig", "irq"])
    if op == "trig":
        return rng.choice([TRIG, TSTART]) | (fld << 8)
    if op == "irq":
        return 0xC000 | (rng.choice([0b010, 0b011]) << 5) | rng.randint(0, 3) | (fld << 8)
    if op == "jmp":
        return ins(JMP, rng.getrandbits(3), rng.getrandbits(5), fld)
    if op == "wait":
        # gpio (00), pin (01) or flag (10); flag index only 0..3.
        src = rng.choice([0b00, 0b01, 0b10])
        idx = rng.randint(0, 3) if src == 0b10 else rng.getrandbits(3)
        return ins(WAIT, src, idx, fld)
    if op == "in":
        return ins(IN, rng.randint(0, 3), rng.choice([1, 1, 2, 4, 8, rng.getrandbits(5)]), fld)
    if op == "out":
        return ins(OUT, rng.randint(0, 5), rng.choice([1, 1, 2, 4, 8, rng.getrandbits(5)]), fld)
    if op == "push":
        return ins(PP, rng.getrandbits(2), 0, fld)
    if op == "pull":
        return ins(PP, 4 | rng.getrandbits(2), 0, fld)
    if op == "mov":
        return ins(MOV, rng.choice([1, 2, 5, 6, 7]),
                   (rng.randint(0, 2) << 3) | rng.choice([0, 1, 2, 3, 5, 6, 7]), fld)
    return ins(SET, rng.choice([0, 1, 2, 4]), rng.getrandbits(5), fld)


def random_config(rng):
    return SmConfig(
        wrap_bottom=rng.getrandbits(5), wrap_top=rng.getrandbits(5),
        in_base=rng.getrandbits(3), out_base=rng.getrandbits(3), set_base=rng.getrandbits(3),
        set_count=rng.getrandbits(3), side_base=rng.getrandbits(3), side_cnt=rng.getrandbits(2),
        side_opt=rng.getrandbits(1), side_pindir=rng.getrandbits(1), jmp_pin=rng.getrandbits(3),
        in_dir=rng.getrandbits(1), out_dir=rng.getrandbits(1), autopush=rng.getrandbits(1),
        autopull=rng.getrandbits(1), push_thresh=rng.choice([0, 1, 8, 16, rng.getrandbits(5)]),
        pull_thresh=rng.choice([0, 1, 8, 16, rng.getrandbits(5)]))


async def random_run(dut, seed, cycles):
    rng = random.Random(seed)
    b = Bench(dut, random_config(rng), [random_instruction(rng) for _ in range(32)])
    await b.reset()
    pins = 0
    for _ in range(cycles):
        if rng.random() < 0.3:
            pins = rng.getrandbits(8)
        await b.cycle(
            tick=rng.random() < 0.8,
            pins=pins,
            tx_empty=rng.random() < 0.3,
            tx_data=rng.getrandbits(32),
            rx_full=rng.random() < 0.3,
            restart=rng.random() < 0.003,
            timer=rng.getrandbits(32),
            flags=rng.getrandbits(4),
            pc_wr=rng.getrandbits(5) if rng.random() < 0.01 else None)


@cocotb.test()
async def test_random_programs(dut):
    """Constrained-random lock step: 150 programs of 400 cycles each."""
    for seed in range(150):
        try:
            await random_run(dut, seed, 400)
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}: {exc}") from None
