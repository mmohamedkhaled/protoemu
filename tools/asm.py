#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Assembler and disassembler for the protoemu instruction set (docs/isa.md).

Source format, one statement per line, `;` starts a comment:

    .program uart_tx            ; optional name
    .side_set 1 opt             ; side-set bits, then `opt` and/or `pindirs`
    .config out_dir 1           ; a configuration field the program relies on
    .define BITS 8
    .wrap_target
    again:
        pull block     side 1 [7]
        set x, BITS - 1 side 0 [7]
    bitloop:
        out pins, 1
        jmp x-- bitloop      [6]
    .wrap

Command line:

    asm.py program.pio                 one hex word per line
    asm.py program.pio --format json   words plus the configuration the host must write
    asm.py program.pio --list          listing with addresses and disassembly
"""

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field

MEM_WORDS = 32

OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_PUSHPULL, OP_MOV, OP_TRIG, OP_SET = 0, 1, 2, 3, 4, 5, 6, 7

JMP_CONDS = {"": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "!osre": 7}
WAIT_SRCS = {"gpio": 0, "pin": 1, "irq": 2}
IN_SRCS = {"pins": 0, "x": 1, "y": 2, "null": 3}
OUT_DSTS = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5}
MOV_DSTS = {"x": 1, "y": 2, "pc": 5, "isr": 6, "osr": 7}
MOV_SRCS = {"pins": 0, "x": 1, "y": 2, "null": 3, "timer": 5, "isr": 6, "osr": 7}
TRIG_OPS = {"trig": 0, "tstart": 1}
IRQ_OPS = {"irq": 0b010, "irqclr": 0b011}  # `irq N` sets flag N, `irqclr N` clears it
MOV_OPS = {"": 0, "!": 1, "~": 1, "::": 2}
SET_DSTS = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}

# Configuration fields a program may fix with `.config`; pin numbers and the
# clock divider are left to whoever wires the program to a board.
CONFIG_FIELDS = {
    "in_dir": 1, "out_dir": 1, "autopush": 1, "autopull": 1, "push_thresh": 31,
    "pull_thresh": 31, "set_count": 5,
}


class AsmError(Exception):
    def __init__(self, message, line=None):
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass
class Program:
    name: str = ""
    origin: int = 0
    words: list = field(default_factory=list)
    wrap_target: int = 0      # absolute addresses
    wrap: int = 0
    side_cnt: int = 0
    side_opt: int = 0
    side_pindir: int = 0
    config: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)

    def sm_config(self):
        """Configuration fields this program fixes, by register field name."""
        return dict(self.config, wrap_bottom=self.wrap_target, wrap_top=self.wrap,
                    side_cnt=self.side_cnt, side_opt=self.side_opt, side_pindir=self.side_pindir)


# ---------------------------------------------------------------- expressions

_BIN_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.FloorDiv: lambda a, b: a // b, ast.Div: lambda a, b: a // b,
    ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
    ast.BitOr: lambda a, b: a | b, ast.BitAnd: lambda a, b: a & b, ast.BitXor: lambda a, b: a ^ b,
}


def evaluate(text, symbols, line=None):
    """Evaluate an integer expression of literals, symbols and + - * / << >> | & ^."""

    def walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in symbols:
                raise AsmError(f"unknown symbol '{node.id}'", line)
            return symbols[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return _BIN_OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise AsmError(f"cannot evaluate '{text}'", line)

    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        raise AsmError(f"cannot evaluate '{text}'", line) from None
    return walk(tree.body)


# ---------------------------------------------------------------- encoding


def pack_field(delay, side, side_cnt, side_opt, line=None):
    """Build bits 12:8 from a delay and an optional side-set value."""
    delay_bits = 5 - side_cnt - side_opt
    if not 0 <= delay <= (1 << delay_bits) - 1:
        raise AsmError(f"delay {delay} does not fit in {delay_bits} bits", line)
    if side is None:
        if side_cnt and not side_opt:
            raise AsmError("this program's side-set is not optional; add 'side <value>'", line)
        return delay
    if not side_cnt:
        raise AsmError("'side' used without a .side_set directive", line)
    if not 0 <= side < (1 << side_cnt):
        raise AsmError(f"side-set value {side} does not fit in {side_cnt} bits", line)
    return (side_opt << 4) | (side << delay_bits) | delay


def _lookup(table, key, what, line):
    if key not in table:
        raise AsmError(f"'{key}' is not a valid {what} (choose from {', '.join(k for k in table if k)})",
                       line)
    return table[key]


def _bit_count(text, symbols, line):
    n = evaluate(text, symbols, line)
    if not 1 <= n <= 32:
        raise AsmError(f"bit count {n} is not in 1..32", line)
    return n & 31


def encode_operands(mnemonic, args, symbols, line=None):
    """Return bits 15:13 and 7:0 of an instruction, given its mnemonic and operand text."""
    args = args.strip()

    def two():
        parts = [p.strip() for p in args.split(",")]
        if len(parts) != 2 or not all(parts):
            raise AsmError(f"'{mnemonic}' takes two operands separated by a comma", line)
        return parts

    if mnemonic == "nop":
        if args:
            raise AsmError("'nop' takes no operands", line)
        return (OP_MOV << 13) | (MOV_DSTS["y"] << 5) | MOV_SRCS["y"]

    if mnemonic in TRIG_OPS:
        if args:
            raise AsmError(f"'{mnemonic}' takes no operands", line)
        return (OP_TRIG << 13) | (TRIG_OPS[mnemonic] << 5)

    if mnemonic in IRQ_OPS:
        flag = evaluate(args, symbols, line)
        if not 0 <= flag <= 3:
            raise AsmError(f"flag index {flag} is not in 0..3", line)
        return (OP_TRIG << 13) | (IRQ_OPS[mnemonic] << 5) | flag

    if mnemonic == "jmp":
        parts = args.split()
        if len(parts) == 1:
            cond, target = "", parts[0]
        else:
            cond, target = "".join(parts[:-1]), parts[-1]
        addr = evaluate(target, symbols, line)
        if not 0 <= addr < MEM_WORDS:
            raise AsmError(f"jump target {addr} is outside the instruction memory", line)
        return (OP_JMP << 13) | (_lookup(JMP_CONDS, cond.lower(), "jmp condition", line) << 5) | addr

    if mnemonic == "wait":
        parts = args.split(None, 2)
        if len(parts) != 3:
            raise AsmError("use 'wait <0|1> gpio|pin|irq <index>'", line)
        pol = evaluate(parts[0], symbols, line)
        idx = evaluate(parts[2], symbols, line)
        if pol not in (0, 1):
            raise AsmError("wait polarity must be 0 or 1", line)
        src = _lookup(WAIT_SRCS, parts[1].lower(), "wait source", line)
        limit = 3 if parts[1].lower() == "irq" else 7
        if not 0 <= idx <= limit:
            raise AsmError(f"wait index {idx} is not in 0..{limit}", line)
        return (OP_WAIT << 13) | (pol << 7) | (src << 5) | idx

    if mnemonic == "in":
        src, n = two()
        return (OP_IN << 13) | (_lookup(IN_SRCS, src.lower(), "in source", line) << 5) | \
            _bit_count(n, symbols, line)

    if mnemonic == "out":
        dst, n = two()
        return (OP_OUT << 13) | (_lookup(OUT_DSTS, dst.lower(), "out destination", line) << 5) | \
            _bit_count(n, symbols, line)

    if mnemonic in ("push", "pull"):
        cond_word = "iffull" if mnemonic == "push" else "ifempty"
        cond, block = 0, 1
        for word in args.lower().split():
            if word == cond_word:
                cond = 1
            elif word == "block":
                block = 1
            elif word == "noblock":
                block = 0
            else:
                raise AsmError(f"'{word}' is not valid after '{mnemonic}'", line)
        return (OP_PUSHPULL << 13) | ((mnemonic == "pull") << 7) | (cond << 6) | (block << 5)

    if mnemonic == "mov":
        dst, src = two()
        match = re.fullmatch(r"(!|~|::)?\s*(\w+)", src)
        if not match:
            raise AsmError(f"cannot parse mov source '{src}'", line)
        op = MOV_OPS[match.group(1) or ""]
        return (OP_MOV << 13) | (_lookup(MOV_DSTS, dst.lower(), "mov destination", line) << 5) | \
            (op << 3) | _lookup(MOV_SRCS, match.group(2).lower(), "mov source", line)

    if mnemonic == "set":
        dst, value = two()
        v = evaluate(value, symbols, line)
        if not 0 <= v <= 31:
            raise AsmError(f"set value {v} is not in 0..31", line)
        return (OP_SET << 13) | (_lookup(SET_DSTS, dst.lower(), "set destination", line) << 5) | v

    raise AsmError(f"unknown instruction '{mnemonic}'", line)


# ---------------------------------------------------------------- assembler

_TAIL = re.compile(r"^(?P<body>.*?)(?:\bside\s+(?P<side>[^\[\]]+?))?\s*(?:\[(?P<delay>[^\]]+)\])?\s*$",
                   re.IGNORECASE)
_LABEL = re.compile(r"^([A-Za-z_]\w*)\s*:\s*(.*)$")


def assemble(source, origin=0):
    """Assemble source text into a Program placed at address `origin`."""
    prog = Program(origin=origin)
    symbols = {}
    statements = []  # (line number, text) of instructions and .word
    wrap_target = wrap = None

    for number, raw in enumerate(source.splitlines(), 1):
        text = raw.split(";", 1)[0].strip()
        while True:
            match = _LABEL.match(text)
            if not match:
                break
            name = match.group(1)
            if name in symbols:
                raise AsmError(f"'{name}' is defined twice", number)
            symbols[name] = prog.labels[name] = origin + len(statements)
            text = match.group(2).strip()
        if not text:
            continue

        if not text.startswith(".") or text.lower().startswith(".word"):
            statements.append((number, text))
            continue

        parts = text.split()
        directive, args = parts[0].lower(), parts[1:]
        if directive == ".program":
            prog.name = args[0] if args else ""
        elif directive == ".side_set":
            if statements:
                raise AsmError(".side_set must come before the first instruction", number)
            if not args:
                raise AsmError(".side_set needs a bit count", number)
            prog.side_cnt = evaluate(args[0], symbols, number)
            flags = [a.lower() for a in args[1:]]
            if set(flags) - {"opt", "pindirs"}:
                raise AsmError("only 'opt' and 'pindirs' may follow the .side_set count", number)
            prog.side_opt = int("opt" in flags)
            prog.side_pindir = int("pindirs" in flags)
            if not 0 <= prog.side_cnt <= 3:
                raise AsmError("side-set count must be 0..3", number)
        elif directive == ".define":
            if len(args) < 2:
                raise AsmError("use '.define NAME value'", number)
            if args[0] in symbols:
                raise AsmError(f"'{args[0]}' is defined twice", number)
            symbols[args[0]] = evaluate(" ".join(args[1:]), symbols, number)
        elif directive == ".config":
            if len(args) < 2 or args[0].lower() not in CONFIG_FIELDS:
                raise AsmError(f"use '.config <field> <value>' with one of {', '.join(CONFIG_FIELDS)}",
                               number)
            name = args[0].lower()
            value = evaluate(" ".join(args[1:]), symbols, number)
            # A threshold of 32 is stored as 0.
            if name.endswith("_thresh") and value == 32:
                value = 0
            if not 0 <= value <= CONFIG_FIELDS[name]:
                raise AsmError(f"{name} = {value} is out of range", number)
            prog.config[name] = value
        elif directive == ".wrap_target":
            wrap_target = origin + len(statements)
        elif directive == ".wrap":
            if not statements:
                raise AsmError(".wrap must follow an instruction", number)
            wrap = origin + len(statements) - 1
        else:
            raise AsmError(f"unknown directive '{directive}'", number)

    if not statements:
        raise AsmError("the program has no instructions")
    if origin + len(statements) > MEM_WORDS:
        raise AsmError(f"program ends at address {origin + len(statements) - 1}, "
                       f"past the {MEM_WORDS}-word instruction memory")

    for number, text in statements:
        if text.lower().startswith(".word"):
            value = evaluate(text[5:], symbols, number)
            if not 0 <= value <= 0xFFFF:
                raise AsmError(".word value does not fit in 16 bits", number)
            prog.words.append(value)
            continue
        match = _TAIL.match(text)
        body = match.group("body").strip()
        mnemonic, _, args = body.partition(" ")
        side = evaluate(match.group("side"), symbols, number) if match.group("side") else None
        delay = evaluate(match.group("delay"), symbols, number) if match.group("delay") else 0
        word = encode_operands(mnemonic.lower(), args, symbols, number)
        word |= pack_field(delay, side, prog.side_cnt, prog.side_opt, number) << 8
        prog.words.append(word)

    prog.wrap_target = origin if wrap_target is None else wrap_target
    prog.wrap = origin + len(statements) - 1 if wrap is None else wrap
    return prog


def assemble_file(path, origin=0):
    with open(path, encoding="utf-8") as handle:
        return assemble(handle.read(), origin)


# ---------------------------------------------------------------- disassembler


def _name(table, code):
    for key, value in table.items():
        if value == code:
            return key
    return None


def disassemble(word, side_cnt=0, side_opt=0):
    """Return assembly text for one word; illegal words come back as `.word`."""
    opcode, fld, hi, lo = word >> 13, (word >> 8) & 31, (word >> 5) & 7, word & 31
    text = None
    if opcode == OP_JMP:
        cond = _name(JMP_CONDS, hi)
        text = f"jmp {cond} {lo}" if cond else f"jmp {lo}"
    elif opcode == OP_WAIT:
        src = _name(WAIT_SRCS, (word >> 5) & 3)
        if src and lo <= (3 if src == "irq" else 7):
            text = f"wait {word >> 7 & 1} {src} {lo}"
    elif opcode == OP_IN:
        src = _name(IN_SRCS, hi)
        if src:
            text = f"in {src}, {lo or 32}"
    elif opcode == OP_OUT:
        dst = _name(OUT_DSTS, hi)
        if dst:
            text = f"out {dst}, {lo or 32}"
    elif opcode == OP_PUSHPULL and lo == 0:
        name, cond = ("pull", "ifempty") if word & 0x80 else ("push", "iffull")
        text = " ".join([name] + [cond] * bool(word & 0x40) + ["block" if word & 0x20 else "noblock"])
    elif opcode == OP_MOV:
        dst, src, op = _name(MOV_DSTS, hi), _name(MOV_SRCS, lo & 7), lo >> 3
        if dst and src and op < 3:
            text = f"mov {dst}, {('', '!', '::')[op]}{src}"
    elif opcode == OP_TRIG and hi < 2 and lo == 0:
        text = _name(TRIG_OPS, hi)
    elif opcode == OP_TRIG and hi in (0b010, 0b011) and lo <= 3:
        text = f"{_name(IRQ_OPS, hi)} {lo}"
    elif opcode == OP_SET:
        dst = _name(SET_DSTS, hi)
        if dst:
            text = f"set {dst}, {lo}"
    if text is None:
        return f".word 0x{word:04x}"

    delay_bits = 5 - side_cnt - side_opt
    delay = fld & ((1 << delay_bits) - 1)
    if side_cnt and (not side_opt or fld & 0x10):
        text += f" side {(fld >> delay_bits) & ((1 << side_cnt) - 1)}"
    elif side_opt and fld >> delay_bits:
        # Side-set data bits set while the enable bit is clear: keep the word exact.
        return f".word 0x{word:04x}"
    if delay:
        text += f" [{delay}]"
    return text


def listing(prog):
    lines = []
    for i, word in enumerate(prog.words):
        addr = prog.origin + i
        marks = "".join(f"{name}: " for name, a in prog.labels.items() if a == addr)
        notes = (" ; wrap target" if addr == prog.wrap_target else "") + \
                (" ; wrap" if addr == prog.wrap else "")
        lines.append(f"{addr:2d}  {word:04x}  {marks}{disassemble(word, prog.side_cnt, prog.side_opt)}{notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------- command line


def main(argv=None):
    parser = argparse.ArgumentParser(description="protoemu assembler")
    parser.add_argument("source", help="assembly source file")
    parser.add_argument("-o", "--output", help="output file (default: standard output)")
    parser.add_argument("--origin", type=int, default=0, help="address of the first instruction")
    parser.add_argument("--format", choices=("hex", "json"), default="hex")
    parser.add_argument("--list", action="store_true", help="print a listing instead")
    args = parser.parse_args(argv)

    try:
        prog = assemble_file(args.source, args.origin)
    except AsmError as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 1

    if args.list:
        text = listing(prog)
    elif args.format == "json":
        text = json.dumps({"name": prog.name, "origin": prog.origin,
                           "words": [f"{w:04x}" for w in prog.words], "config": prog.sm_config()},
                          indent=2)
    else:
        text = "\n".join(f"{w:04x}" for w in prog.words)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
