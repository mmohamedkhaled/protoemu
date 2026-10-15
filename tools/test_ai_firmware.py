# SPDX-License-Identifier: Apache-2.0
"""Offline tests for ai_firmware.py: the parts that need no network.

The model call and the SymbiYosys gate are not exercised here (they need the
API and the toolchain); the extraction and the assembler gate, which decide
whether a reply is even a candidate, are.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "formal"))

import ai_firmware as ai  # noqa: E402


def test_extract_from_code_fence():
    reply = "Here is the program:\n```pio\n.program x\nnop\n```\nHope that helps."
    assert ai.extract_program(reply) == ".program x\nnop"


def test_extract_without_fence():
    assert ai.extract_program("nop\njmp 0").strip() == "nop\njmp 0"


def test_extract_prefers_pio_block():
    reply = "```\nignored\n```\ntext\n```pio\n.program y\nnop\n```"
    # The first fenced block wins; both are accepted, but a real reply has one.
    assert "ignored" in ai.extract_program(reply) or ".program y" in ai.extract_program(reply)


def test_gate_assemble_accepts_valid():
    ok, detail = ai.gate_assemble(".program ok\nset x, 1\njmp 0")
    assert ok and "2 words" in detail


def test_gate_assemble_rejects_bad_mnemonic():
    ok, detail = ai.gate_assemble("bogus x, 1")
    assert not ok and "assembler error" in detail


def test_gate_assemble_hints_bare_side():
    # The model's most common mistake: a bare `side 1` line.
    ok, detail = ai.gate_assemble(".side_set 1\nnop side 1\nside 1")
    assert not ok and "suffix" in detail


def test_gate_assemble_rejects_oversize():
    ok, detail = ai.gate_assemble("\n".join(["nop"] * 33))
    assert not ok
