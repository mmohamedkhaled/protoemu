# SPDX-License-Identifier: Apache-2.0
"""Unit test for pe_decoder: every 16-bit word, against the reference decoder in models/isa_model.py."""

import cocotb
from cocotb.triggers import Timer

from models.isa_model import decode_op as reference
from models.isa_model import split_field as reference_field

OPS = ("jmp", "wait", "in", "out", "push", "pull", "mov", "set", "trig", "tstart",
       "irqset", "irqclr")


@cocotb.test()
async def test_every_word(dut):
    dut.side_cnt.value = 0
    dut.side_opt.value = 0
    for word in range(1 << 16):
        dut.instr.value = word
        await Timer(1, unit="ns")
        want = reference(word)
        got = [name for name in OPS if int(getattr(dut, "op_" + name).value)]
        assert got == ([want] if want else []), f"{word:#06x}: decoded {got}, want {want}"
        assert int(dut.illegal.value) == (want is None), f"{word:#06x}: illegal flag"

        hi, lo = (word >> 5) & 7, word & 31
        assert int(dut.sel.value) == hi
        assert int(dut.arg.value) == lo
        assert int(dut.count.value) == (lo or 32)
        assert int(dut.wait_pol.value) == (word >> 7) & 1
        assert int(dut.wait_rel.value) == (word >> 5) & 1
        assert int(dut.wait_idx.value) == lo & 7
        assert int(dut.pp_if.value) == (word >> 6) & 1
        assert int(dut.pp_block.value) == (word >> 5) & 1
        assert int(dut.mov_op.value) == lo >> 3
        assert int(dut.mov_src.value) == lo & 7


@cocotb.test()
async def test_delay_and_side_set_field(dut):
    # The split must not depend on the opcode, legal or not.
    for base in (0x0000, 0x2083, 0x4001, 0x6001, 0x80A0, 0xA042, 0xC000, 0xE001, 0xA0FF, 0xC0FF):
        for side_opt in (0, 1):
            for side_cnt in range(4):
                dut.side_opt.value = side_opt
                dut.side_cnt.value = side_cnt
                for field in range(32):
                    dut.instr.value = (base & 0xE0FF) | (field << 8)
                    await Timer(1, unit="ns")
                    delay, side_en, side_val = reference_field(field, side_cnt, side_opt)
                    got = (int(dut.delay.value), int(dut.side_en.value))
                    assert got == (delay, side_en), (
                        f"field {field:05b} cnt={side_cnt} opt={side_opt}: got {got}, want {(delay, side_en)}")
                    if side_en:
                        assert int(dut.side_val.value) == side_val


@cocotb.test()
async def test_reference_encodings(dut):
    """The worked examples in docs/isa.md section 7."""
    dut.side_cnt.value = 0
    dut.side_opt.value = 0
    examples = {0x0043: "jmp", 0x2083: "wait", 0x20A0: "wait", 0x4001: "in", 0x6001: "out",
                0x8020: "push", 0x80A0: "pull", 0xA042: "mov", 0xE001: "set",
                0xC000: "trig", 0xC020: "tstart", 0xA0C5: "mov"}
    for word, name in examples.items():
        dut.instr.value = word
        await Timer(1, unit="ns")
        assert int(getattr(dut, "op_" + name).value) == 1, f"{word:#06x} should be {name}"
        assert int(dut.illegal.value) == 0
