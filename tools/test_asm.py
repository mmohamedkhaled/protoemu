# SPDX-License-Identifier: Apache-2.0
"""Tests for the assembler. Run with `pytest tools` (or `make -C test asm`)."""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "test"))

import asm  # noqa: E402
from models.isa_model import decode_op, split_field  # noqa: E402

SIDE_CONFIGS = [(cnt, opt) for opt in (0, 1) for cnt in range(4)]


def one(text, **kwargs):
    return asm.assemble(text, **kwargs).words[0]


def test_reference_encodings():
    """The worked examples in docs/isa.md section 7."""
    examples = {
        "jmp x-- 3": 0x0043, "wait 1 gpio 3": 0x2083, "wait 1 pin 0": 0x20A0,
        "in pins, 1": 0x4001, "out pins, 1": 0x6001, "push block": 0x8020,
        "pull block": 0x80A0, "mov y, y": 0xA042, "set pins, 1": 0xE001,
    }
    for text, word in examples.items():
        assert one(text) == word, text
    assert one("nop") == 0xA042
    assert one("push") == 0x8020 and one("pull") == 0x80A0  # blocking by default
    assert one("in x, 32") == 0x4020 and one("out null, 32") == 0x6060
    assert one("trig") == 0xC000 and one("tstart") == 0xC020
    assert one("mov x, timer") == 0xA025
    # irq set/clear (opcode 110, hi 010/011) and wait on a flag (WAIT src 10).
    assert one("irq 0") == 0xC040 and one("irq 3") == 0xC043
    assert one("irqclr 0") == 0xC060 and one("irqclr 2") == 0xC062
    assert one("wait 1 irq 0") == 0x20C0 and one("wait 0 irq 3") == 0x2043


def test_every_operand_spelling_is_legal_for_the_reference_decoder():
    """Whatever the assembler emits, the independent decoder accepts as the same operation."""
    lines = []
    lines += [f"jmp {c} 5" for c in asm.JMP_CONDS]
    lines += [f"wait {p} {s} {i}" for p in (0, 1) for s in asm.WAIT_SRCS
              for i in range(4 if s == "irq" else 8)]
    lines += [f"in {s}, {n}" for s in asm.IN_SRCS for n in (1, 7, 32)]
    lines += [f"out {d}, {n}" for d in asm.OUT_DSTS for n in (1, 7, 32)]
    lines += [f"{m} {c} {b}" for m, c in (("push", "iffull"), ("push", ""), ("pull", "ifempty"),
                                          ("pull", "")) for b in ("block", "noblock", "")]
    lines += [f"mov {d}, {o}{s}" for d in asm.MOV_DSTS for o in asm.MOV_OPS for s in asm.MOV_SRCS]
    lines += [f"set {d}, {v}" for d in asm.SET_DSTS for v in (0, 21, 31)]
    lines += ["trig", "tstart"]
    lines += [f"irq {n}" for n in range(4)] + [f"irqclr {n}" for n in range(4)]
    for line in lines:
        word = one(line)
        expected = {"irq": "irqset", "irqclr": "irqclr"}.get(line.split()[0], line.split()[0])
        assert decode_op(word) == expected, line


def test_round_trip_every_word():
    """disassemble() then assemble() gives back the same word, for all 65536 words."""
    for side_cnt, side_opt in SIDE_CONFIGS:
        flags = " opt" if side_opt else ""
        header = f".side_set {side_cnt}{flags}\n" if side_cnt or side_opt else ""
        for word in range(0, 1 << 16, 1 if (side_cnt, side_opt) == (0, 0) else 7):
            text = asm.disassemble(word, side_cnt, side_opt)
            assert one(header + text) == word, f"{word:#06x} -> '{text}'"
            # Only illegal words fall back to .word (or side bits without the enable).
            if decode_op(word) is None:
                assert text.startswith(".word")


def test_delay_and_side_set_packing():
    for side_cnt, side_opt in SIDE_CONFIGS:
        if side_cnt == 0:
            continue
        flags = " opt" if side_opt else ""
        delay_bits = 5 - side_cnt - side_opt
        for side in range(1 << side_cnt):
            for delay in range(1 << delay_bits):
                word = one(f".side_set {side_cnt}{flags}\nnop side {side} [{delay}]")
                assert split_field((word >> 8) & 31, side_cnt, side_opt) == (delay, 1, side)
        if side_opt:
            word = one(f".side_set {side_cnt} opt\nnop [1]")
            assert split_field((word >> 8) & 31, side_cnt, 1)[:2] == (1, 0)


def test_program_structure():
    prog = asm.assemble("""
        .program demo
        .side_set 1 opt pindirs
        .config autopull 1
        .config pull_thresh 32      ; stored as 0
        .config out_dir 1
        .define BITS 8
        .define LAST (BITS - 1)
            set pindirs, 1
        .wrap_target
        start: pull block  side 1 [3]
            set x, LAST
        loop:
            out pins, 1
            jmp x-- loop
        .wrap
            jmp start               ; never reached
    """, origin=4)
    assert prog.name == "demo" and prog.origin == 4 and len(prog.words) == 6
    assert prog.labels == {"start": 5, "loop": 7}
    assert (prog.wrap_target, prog.wrap) == (5, 8)
    assert prog.words[2] == 0xE027                       # set x, 7
    assert prog.words[4] == 0x0047                       # jmp x-- 7
    assert prog.words[5] == 0x0005                       # jmp 5
    assert prog.sm_config() == dict(autopull=1, pull_thresh=0, out_dir=1, wrap_bottom=5,
                                    wrap_top=8, side_cnt=1, side_opt=1, side_pindir=1)


def test_defaults_wrap_whole_program():
    prog = asm.assemble("nop\nnop\nnop", origin=10)
    assert (prog.wrap_target, prog.wrap) == (10, 12)


def test_forward_labels_and_word():
    prog = asm.assemble("jmp end\n.word 0xBEEF\nend: nop")
    assert prog.words == [0x0002, 0xBEEF, 0xA042]


@pytest.mark.parametrize("source, message", [
    ("bogus x, 1", "unknown instruction"),
    ("set pins, 32", "not in 0..31"),
    ("in pins, 0", "not in 1..32"),
    ("out pins, 33", "not in 1..32"),
    ("out isr, 1", "not a valid out destination"),
    ("mov pins, x", "not a valid mov destination"),
    ("wait 1 gpio 8", "not in 0..7"),
    ("wait 2 gpio 1", "polarity"),
    ("jmp nowhere", "unknown symbol"),
    ("jmp 32", "outside the instruction memory"),
    ("nop [32]", "does not fit"),
    ("nop side 1", "without a .side_set"),
    (".side_set 1\nnop", "not optional"),
    (".side_set 2\nnop side 4", "does not fit in 2 bits"),
    (".side_set 2 opt\nnop side 1 [4]", "does not fit in 2 bits"),
    (".side_set 4\nnop", "must be 0..3"),
    ("nop\n.side_set 1", "before the first instruction"),
    ("a: nop\na: nop", "defined twice"),
    (".config speed 1\nnop", "use '.config"),
    (".config autopull 2\nnop", "out of range"),
    (".bogus\nnop", "unknown directive"),
    ("\n".join(["nop"] * 33), "past the 32-word"),
    ("; nothing", "no instructions"),
    ("push now", "not valid after"),
    ("trig 1", "takes no operands"),
    ("mov timer, x", "not a valid mov destination"),
    ("irq 4", "flag index 4 is not in 0..3"),
    ("wait 1 irq 4", "not in 0..3"),
    ("wait 1 flag 0", "not a valid wait source"),
])
def test_errors(source, message):
    with pytest.raises(asm.AsmError, match=message.replace("(", r"\(").replace(".", r"\.")):
        asm.assemble(source)


def test_error_reports_the_line():
    with pytest.raises(asm.AsmError) as info:
        asm.assemble("nop\nnop\nset pins, 99\n")
    assert info.value.line == 3


def test_command_line(tmp_path, capsys):
    source = tmp_path / "blink.pio"
    source.write_text(".program blink\nset pins, 1 [31]\nset pins, 0 [31]\n")
    assert asm.main([str(source)]) == 0
    assert capsys.readouterr().out.split() == ["ff01", "ff00"]
    assert asm.main([str(source), "--format", "json"]) == 0
    assert '"wrap_top": 1' in capsys.readouterr().out
    assert asm.main([str(source), "--list"]) == 0
    assert "set pins, 1 [31]" in capsys.readouterr().out
    source.write_text("set pins, 99\n")
    assert asm.main([str(source)]) == 1
    assert "line 1" in capsys.readouterr().err
