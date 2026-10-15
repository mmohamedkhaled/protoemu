# SPDX-License-Identifier: Apache-2.0
"""Executable reference model of the protoemu ISA.

Written from docs/isa.md, not from the RTL: it uses plain shifts, dictionaries
of pin writes and one `if` per rule in the document. The unit tests run the RTL
and this model in lock step and compare them after every clock cycle.
"""

from dataclasses import dataclass, field

M32 = 0xFFFFFFFF

# Legal operand codes, copied from the tables in docs/isa.md section 4.
IN_SRC = {0b000: "pins", 0b001: "x", 0b010: "y", 0b011: "null"}
OUT_DST = {0b000: "pins", 0b001: "x", 0b010: "y", 0b011: "null", 0b100: "pindirs", 0b101: "pc"}
MOV_DST = {0b001: "x", 0b010: "y", 0b101: "pc", 0b110: "isr", 0b111: "osr"}
MOV_SRC = {0b000: "pins", 0b001: "x", 0b010: "y", 0b011: "null", 0b101: "timer", 0b110: "isr",
           0b111: "osr"}
MOV_OP = {0b00: "copy", 0b01: "invert", 0b10: "reverse"}
SET_DST = {0b000: "pins", 0b001: "x", 0b010: "y", 0b100: "pindirs"}


def decode_op(word):
    """Return the operation name, or None for an illegal instruction."""
    opcode, hi, lo = word >> 13, (word >> 5) & 7, word & 31
    if opcode == 0b000:
        return "jmp"
    if opcode == 0b001:
        src = (word >> 5) & 3
        if lo >> 3:
            return None
        if src in (0b00, 0b01):
            return "wait"
        if src == 0b10 and lo <= 3:  # wait on a shared flag
            return "wait"
        return None
    if opcode == 0b010:
        return "in" if hi in IN_SRC else None
    if opcode == 0b011:
        return "out" if hi in OUT_DST else None
    if opcode == 0b100:
        if lo != 0:
            return None
        return "pull" if word & 0x80 else "push"
    if opcode == 0b101:
        legal = hi in MOV_DST and (lo & 7) in MOV_SRC and (lo >> 3) in MOV_OP
        return "mov" if legal else None
    if opcode == 0b110:
        if hi >> 2:
            return None
        if hi >> 1 == 0b00:                       # trig / tstart
            return None if lo != 0 else ("tstart" if hi & 1 else "trig")
        # hi 010 / 011: irq set / clear, flag index in lo[1:0]
        return None if lo >> 2 else ("irqclr" if hi & 1 else "irqset")
    if opcode == 0b111:
        return "set" if hi in SET_DST else None
    return None


def split_field(fld, side_cnt, side_opt):
    """Split bits 12:8 into (delay, side_en, side_val) per docs/isa.md section 3."""
    bits = [(fld >> i) & 1 for i in (4, 3, 2, 1, 0)]  # from bit 12 down
    enable = bits.pop(0) if side_opt else 1
    side = 0
    for _ in range(side_cnt):
        side = (side << 1) | bits.pop(0)
    delay = 0
    for b in bits:
        delay = (delay << 1) | b
    return delay, int(bool(side_cnt) and enable), side


def reverse32(v):
    return int(f"{v:032b}"[::-1], 2)


@dataclass
class SmConfig:
    wrap_bottom: int = 0
    wrap_top: int = 31
    in_base: int = 0
    out_base: int = 0
    set_base: int = 0
    set_count: int = 0
    side_base: int = 0
    side_cnt: int = 0
    side_opt: int = 0
    side_pindir: int = 0
    jmp_pin: int = 0
    in_dir: int = 0   # 1 = shift right
    out_dir: int = 0  # 1 = shift right
    autopush: int = 0
    autopull: int = 0
    push_thresh: int = 0  # 0 means 32
    pull_thresh: int = 0  # 0 means 32


@dataclass
class TickResult:
    """What one tick did outside the state machine."""
    out_writes: dict = field(default_factory=dict)  # pin -> PIN_OUT bit
    dir_writes: dict = field(default_factory=dict)  # pin -> PIN_DIR bit
    tx_pop: bool = False
    rx_push: bool = False
    rx_data: int = 0
    trig: bool = False      # fired the trigger pulse generator
    tstart: bool = False    # restarted the cycle timer
    flag_set: int = 0       # 4-bit one-hot: flag raised this tick
    flag_clr: int = 0       # 4-bit one-hot: flag cleared this tick
    executed: bool = False
    stalled: bool = False


class SmModel:
    def __init__(self, cfg=None):
        self.cfg = cfg or SmConfig()
        self.pc = 0
        self.x = 0
        self.y = 0
        self.restart()

    def restart(self):
        self.isr = 0
        self.isr_count = 0
        self.osr = 0
        self.osr_count = 32
        self.delay = 0
        self.illegal = False
        self.rxover = False

    # -- helpers ---------------------------------------------------------

    def _pins_rel(self, pins):
        b = self.cfg.in_base
        return ((pins >> b) | (pins << (8 - b))) & 0xFF

    def _shift_in(self, n, data):
        bits = data & ((1 << n) - 1)
        if self.cfg.in_dir:
            value = ((self.isr >> n) | (bits << (32 - n))) & M32
        else:
            value = ((self.isr << n) | bits) & M32
        return value, min(32, self.isr_count + n)

    def _shift_out(self, n):
        if self.cfg.out_dir:
            bits = self.osr & ((1 << n) - 1)
            self.osr >>= n
        else:
            bits = self.osr >> (32 - n)
            self.osr = (self.osr << n) & M32
        self.osr_count = min(32, self.osr_count + n)
        return bits

    @staticmethod
    def _write_pins(writes, base, nbits, value):
        for i in range(min(nbits, 8)):
            writes[(base + i) % 8] = (value >> i) & 1

    # -- one tick --------------------------------------------------------

    def step(self, instr, pins, tx_empty, tx_data, rx_full, timer=0, flags=0):
        """Advance by one tick. `instr` must be the word at the current pc."""
        cfg, res = self.cfg, TickResult()
        push_thr = cfg.push_thresh or 32
        pull_thr = cfg.pull_thresh or 32

        if self.delay:
            self.delay -= 1
        else:
            self._instruction(instr, pins, tx_empty, tx_data, rx_full, res, push_thr, pull_thr,
                              timer, flags)

        # Autopull refill, on the state left by the instruction of this tick.
        if cfg.autopull and self.osr_count >= pull_thr and not tx_empty and not res.tx_pop:
            self.osr, self.osr_count = tx_data, 0
            res.tx_pop = True
        return res

    def _instruction(self, instr, pins, tx_empty, tx_data, rx_full, res, push_thr, pull_thr,
                     timer, flags):
        cfg = self.cfg
        op = decode_op(instr)
        hi, lo = (instr >> 5) & 7, instr & 31
        n = lo or 32
        delay, side_en, side_val = split_field((instr >> 8) & 31, cfg.side_cnt, cfg.side_opt)
        pins_rel = self._pins_rel(pins)
        side = {}
        if side_en:
            self._write_pins(side, cfg.side_base, cfg.side_cnt, side_val)
        side_target = res.dir_writes if cfg.side_pindir else res.out_writes

        new_pc = None
        out_writes, dir_writes = {}, {}

        if op == "jmp":
            cond = hi
            taken = [True, self.x == 0, self.x != 0, self.y == 0, self.y != 0, self.x != self.y,
                     bool((pins >> cfg.jmp_pin) & 1), self.osr_count < pull_thr][cond]
            if cond == 0b010:
                self.x = (self.x - 1) & M32
            if cond == 0b100:
                self.y = (self.y - 1) & M32
            if taken:
                new_pc = lo

        elif op == "wait":
            pol, src, idx = (instr >> 7) & 1, (instr >> 5) & 3, lo & 7
            if src == 0b10:
                level = (flags >> (idx & 3)) & 1          # a shared flag
            else:
                level = ((pins_rel if src else pins) >> idx) & 1
            if level != pol:
                res.stalled = True

        elif op == "in":
            source = {"pins": pins_rel, "x": self.x, "y": self.y, "null": 0}[IN_SRC[hi]]
            value, count = self._shift_in(n, source)
            if cfg.autopush and count >= push_thr:
                if rx_full:
                    res.stalled = True
                else:
                    res.rx_push, res.rx_data = True, value
                    self.isr, self.isr_count = 0, 0
            else:
                self.isr, self.isr_count = value, count

        elif op == "out":
            if cfg.autopull and self.osr_count >= pull_thr:
                res.stalled = True
            else:
                bits = self._shift_out(n)
                dst = OUT_DST[hi]
                if dst == "pins":
                    self._write_pins(out_writes, cfg.out_base, n, bits)
                elif dst == "pindirs":
                    self._write_pins(dir_writes, cfg.out_base, n, bits)
                elif dst == "x":
                    self.x = bits
                elif dst == "y":
                    self.y = bits
                elif dst == "pc":
                    new_pc = bits & 31

        elif op == "push":
            iffull, block = (instr >> 6) & 1, (instr >> 5) & 1
            if not iffull or self.isr_count >= push_thr:
                if rx_full and block:
                    res.stalled = True
                else:
                    if rx_full:
                        self.rxover = True
                    else:
                        res.rx_push, res.rx_data = True, self.isr
                    self.isr, self.isr_count = 0, 0

        elif op == "pull":
            ifempty, block = (instr >> 6) & 1, (instr >> 5) & 1
            if not ifempty or self.osr_count >= pull_thr:
                if tx_empty and block:
                    res.stalled = True
                elif tx_empty:
                    self.osr, self.osr_count = self.x, 0
                else:
                    self.osr, self.osr_count = tx_data, 0
                    res.tx_pop = True

        elif op == "mov":
            value = {"pins": pins_rel, "x": self.x, "y": self.y, "null": 0, "timer": timer,
                     "isr": self.isr, "osr": self.osr}[MOV_SRC[lo & 7]]
            operation = MOV_OP[lo >> 3]
            if operation == "invert":
                value ^= M32
            elif operation == "reverse":
                value = reverse32(value)
            dst = MOV_DST[hi]
            if dst == "x":
                self.x = value
            elif dst == "y":
                self.y = value
            elif dst == "pc":
                new_pc = value & 31
            elif dst == "isr":
                self.isr, self.isr_count = value, 0
            elif dst == "osr":
                self.osr, self.osr_count = value, 0

        elif op == "set":
            dst = SET_DST[hi]
            if dst == "pins":
                self._write_pins(out_writes, cfg.set_base, min(cfg.set_count, 5), lo)
            elif dst == "pindirs":
                self._write_pins(dir_writes, cfg.set_base, min(cfg.set_count, 5), lo)
            elif dst == "x":
                self.x = lo
            elif dst == "y":
                self.y = lo

        elif op == "trig":
            res.trig = True

        elif op == "tstart":
            res.tstart = True

        elif op == "irqset":
            res.flag_set = 1 << (lo & 3)

        elif op == "irqclr":
            res.flag_clr = 1 << (lo & 3)

        else:
            self.illegal = True

        # Side-set applies whether or not the instruction stalled, and wins.
        if not res.stalled:
            res.out_writes.update(out_writes)
            res.dir_writes.update(dir_writes)
        side_target.update(side)

        if res.stalled:
            return
        res.executed = True
        self.delay = delay
        if new_pc is not None:
            self.pc = new_pc
        elif self.pc == cfg.wrap_top:
            self.pc = cfg.wrap_bottom
        else:
            self.pc = (self.pc + 1) % 32
