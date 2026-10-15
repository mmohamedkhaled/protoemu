#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write protoemu firmware with an LLM, and accept it only if it is verified.

This is the "AI-assisted verification" idea the competition post asks for, made
concrete: a language model writes a `.pio` program from a plain-English spec,
and the program is kept only if it (1) assembles with tools/asm.py, (2) passes
the well-formedness proof on the real state-machine RTL (never executes an
illegal instruction, never leaves its code), and optionally (3) passes a named
SymbiYosys correctness proof or a cocotb test. Any failure is fed back to the
model, which tries again, up to a limit. Formal methods, not the model, decide
whether to trust the output.

The API key is read from the AI_API_KEY environment variable (GLM_API_KEY is
also accepted for back-compatibility). It is never written to the repository.

usage:
    ai_firmware.py --name blink --spec "Blink a LED on the side-set pin: on for
        16 ticks, off for 16 ticks, forever."
    ai_firmware.py --name uart_tx --spec-file specs/uart_tx.txt --proof fw_uart_tx
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "formal"))

import asm  # noqa: E402
import fw_gen  # noqa: E402

DEFAULT_BASE = (os.environ.get("AI_BASE_URL") or os.environ.get("GLM_BASE_URL")
                or "https://open.bigmodel.cn/api/paas/v4")
DEFAULT_MODEL = os.environ.get("AI_MODEL") or os.environ.get("GLM_MODEL") or "glm-4.5-flash"

# A compact description of the instruction set, so the model does not have to be
# handed the whole of docs/isa.md. Kept in step with tools/asm.py.
ISA_CHEATSHEET = """\
protoemu is a PIO-style programmable state machine. A program is up to 32 lines,
one instruction each, assembled by a custom assembler. Syntax:

  label:  mnemonic operands   side <v>   [<delay>]   ; comment

`side <v>` drives the side-set pins (needs a `.side_set <n> [opt] [pindirs]`
directive first). `[<delay>]` stalls 0..31 ticks after the instruction. The
program loops: after the last line it wraps to the top unless `.wrap_target` /
`.wrap` narrow the loop, or a jmp redirects.

Registers (per state machine): X, Y (32-bit scratch), ISR, OSR (32-bit shift
regs), TX FIFO (host->program), RX FIFO (program->host). A shared 32-bit TIMER
counts clock cycles. Four shared flags let the two state machines signal.

Instructions:
  jmp <cond> <label>     cond: (none)=always, !x, x--, !y, y--, x!=y, pin, !osre
  wait <0|1> gpio <n>    stall until pin n reaches the level
  wait <0|1> pin <n>     pin n relative to IN_BASE
  wait <0|1> irq <n>     stall until shared flag n (0..3) reaches the level
  in <src>, <count>      src: pins,x,y,null   shift count bits into ISR (1..32)
  out <dst>, <count>     dst: pins,x,y,null,pindirs,pc   shift from OSR
  push [iffull] [block|noblock]    ISR -> RX FIFO
  pull [ifempty] [block|noblock]   TX FIFO -> OSR
  mov <dst>, [!|::]<src> dst: x,y,pc,isr,osr   src: pins,x,y,null,timer,isr,osr
  set <dst>, <value>     dst: pins,x,y,pindirs   value 0..31
  nop
  trig                   fire the trigger pulse (uo[5]); host sets delay/width
  tstart                 restart TIMER
  irq <n>                set shared flag n (0..3)
  irqclr <n>             clear shared flag n

Directives: .program <name>, .side_set <n> [opt] [pindirs], .define NAME value,
.config <field> <value> (in_dir,out_dir,autopush,autopull,push_thresh,
pull_thresh,set_count), .wrap_target, .wrap, .word <hex>.

Notes: autopull/autopush refill OSR/ISR from the FIFOs automatically when set.
`out pins`/`set pins` write pins starting at OUT_BASE/SET_BASE. Pin numbers and
the clock divider are set by the host, not the program; write the program in
terms of OUT_BASE/IN_BASE/SET_BASE/SIDE_BASE. Keep programs within 32 lines.
A value pulled from the TX FIFO lands in OSR, so read it with `mov <dst>, osr`.

CRITICAL SYNTAX: `side <v>` and `[<delay>]` are SUFFIXES on an instruction line,
never lines of their own. To drive a side-set pin you must attach `side 0` or
`side 1` to a real instruction. A bare `side 1` line is an error. Every line is
either a label, a directive (starts with `.`), or ONE instruction optionally
followed by `side <v>` and/or `[<delay>]`.

Worked example (UART transmitter, 8 ticks per bit, side-set drives the TX pin):

```pio
.program uart_tx
.side_set 1 opt
.config out_dir 1
    pull block       side 1 [7]   ; stop/idle bit held high
    set x, 7         side 0 [7]   ; start bit low
bitloop:
    out pins, 1                   ; one data bit to OUT_BASE
    jmp x-- bitloop        [6]
```

Another example (square wave on the side-set pin, high 8 ticks, low 8 ticks):

```pio
.program square
.side_set 1
again:
    nop              side 1 [7]
    jmp again        side 0 [7]
```
"""

SYSTEM_PROMPT = """\
You write firmware for the protoemu protocol-emulator chip. You are given the
instruction set and a specification. Produce ONE program as protoemu assembly,
inside a single ```pio code block and nothing else outside a short explanation.
The program must assemble and must never execute an illegal instruction or jump
outside itself. If you are given feedback about a previous attempt that failed
verification, fix exactly that problem. Do not invent instructions or operands
that are not in the instruction set.
"""


def read_key():
    key = os.environ.get("AI_API_KEY") or os.environ.get("GLM_API_KEY")
    if key:
        return key.strip()
    path = os.path.expanduser(os.environ.get("AI_KEY_FILE", "~/protoemu-tools/secrets/glm_key"))
    if os.path.exists(path):
        with open(path) as handle:
            return handle.read().strip()
    sys.exit("No API key: set AI_API_KEY (or AI_KEY_FILE to a file that holds it)")


def call_model(key, messages, model, base, max_tokens=4096, retries=4):
    """One chat completion. Returns the assistant's content.

    Thinking is disabled: it keeps the reply in the `content` field and makes the
    call fast and deterministic. The verification loop, not the model's internal
    reasoning, is what guarantees correctness.
    """
    body = json.dumps({"model": model, "messages": messages, "temperature": 0.2,
                       "max_tokens": max_tokens, "thinking": {"type": "disabled"}}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            message = data["choices"][0]["message"]
            return (message.get("content") or message.get("reasoning_content") or ""), \
                data.get("usage", {})
        except Exception as exc:  # noqa: BLE001  (network, rate limit, etc.)
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"model call failed after {retries} tries: {last}")


def extract_program(text):
    """Pull the assembly out of the model's reply."""
    m = re.search(r"```(?:pio|asm|text)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


# ---------------------------------------------------------------- the gates


def gate_assemble(source):
    try:
        prog = asm.assemble(source)
    except asm.AsmError as exc:
        hint = ""
        if "unknown instruction ''" in str(exc):
            hint = (" Hint: `side <v>` and `[<delay>]` are suffixes on an instruction line, "
                    "not lines of their own. Attach them to a real instruction such as "
                    "`nop side 1 [7]`.")
        return False, f"assembler error: {exc}.{hint}"
    if len(prog.words) > 32:
        return False, f"program is {len(prog.words)} words, the memory holds 32"
    return True, f"assembles to {len(prog.words)} words"


def gate_wellformed(name, source):
    """Generate the real-RTL wrapper and prove it never goes illegal or out of range."""
    with tempfile.TemporaryDirectory() as work:
        pio = os.path.join(work, f"{name}.pio")
        with open(pio, "w") as handle:
            handle.write(source)
        wrapper = os.path.join(work, f"{name}_sm.v")
        try:
            fw_gen.generate(pio, wrapper)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not build the proof wrapper: {exc}"
        sby = os.path.join(work, "wf.sby")
        src = os.path.join(ROOT, "src")
        # SymbiYosys copies [files] in by basename, so read_verilog uses basenames.
        with open(sby, "w") as handle:
            handle.write(f"""\
[options]
mode prove
depth 8
[engines]
smtbmc yices
[script]
read_verilog -formal -DFORMAL pe_sm.v pe_shift.v pe_decoder.v {name}_sm.v
hierarchy -top {name}_sm
proc -norom
prep -top {name}_sm
[files]
{src}/pe_sm.v
{src}/pe_shift.v
{src}/pe_decoder.v
{wrapper}
""")
        proc = subprocess.run(["sby", "-f", sby], cwd=work, capture_output=True, text=True)
        if "DONE (PASS" in proc.stdout:
            return True, "well-formedness proof passes (never illegal, pc in range)"
        fail = [ln for ln in proc.stdout.splitlines() if "Assert failed" in ln or "ERROR" in ln]
        return False, "well-formedness proof failed: " + (fail[0] if fail else "see log")


def gate_proof(name, source, task):
    """Install the program as firmware/<name>.pio and run a named formal/*.sby proof."""
    target = os.path.join(ROOT, "firmware", f"{name}.pio")
    backup = target + ".bak" if os.path.exists(target) else None
    if backup:
        os.replace(target, backup)
    gen_dir = os.path.join(ROOT, "formal", "gen")
    os.makedirs(gen_dir, exist_ok=True)
    try:
        with open(target, "w") as handle:
            handle.write(source)
        subprocess.run([sys.executable, os.path.join(ROOT, "formal", "fw_gen.py"), target,
                        os.path.join(gen_dir, f"{name}_sm.v")], check=True)
        sby, task_name = task.split(":", 1) if ":" in task else (task, None)
        cmd = ["sby", "-f", f"{sby}.sby"] + ([task_name] if task_name else [])
        proc = subprocess.run(cmd, cwd=os.path.join(ROOT, "formal"), capture_output=True, text=True)
        if "DONE (PASS" in proc.stdout and "DONE (FAIL" not in proc.stdout:
            return True, f"correctness proof {task} passes"
        fail = [ln for ln in proc.stdout.splitlines() if "Assert failed" in ln]
        return False, f"correctness proof {task} failed: " + (fail[0] if fail else "see log")
    finally:
        if backup:
            os.replace(backup, target)
        elif os.path.exists(target):
            os.remove(target)


# ---------------------------------------------------------------- the loop


def run(spec, name, proof, model, base, max_iters, out_dir):
    key = read_key()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + ISA_CHEATSHEET},
        {"role": "user", "content": f"Specification for program `{name}`:\n\n{spec}\n\n"
                                     "Write the program now."},
    ]
    transcript = []
    for iteration in range(1, max_iters + 1):
        print(f"\n=== iteration {iteration} ===")
        content, usage = call_model(key, messages, model, base)
        source = extract_program(content)
        transcript.append({"iteration": iteration, "reply": content, "program": source,
                           "usage": usage})
        print(source)

        gates = [("assemble", lambda: gate_assemble(source)),
                 ("wellformed", lambda: gate_wellformed(name, source))]
        if proof:
            gates.append(("proof", lambda: gate_proof(name, source, proof)))

        failed = None
        for gate_name, gate in gates:
            ok, detail = gate()
            print(f"  [{gate_name}] {'PASS' if ok else 'FAIL'}: {detail}")
            transcript[-1].setdefault("gates", []).append({"gate": gate_name, "ok": ok,
                                                           "detail": detail})
            if not ok:
                failed = (gate_name, detail)
                break

        if failed is None:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"{name}.pio")
            with open(path, "w", newline="\n") as handle:
                handle.write(source + ("\n" if not source.endswith("\n") else ""))
            print(f"\nACCEPTED after {iteration} iteration(s) -> {path}")
            _save_transcript(out_dir, name, transcript, accepted=True)
            return 0

        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         f"That program failed the `{failed[0]}` check:\n{failed[1]}\n"
                         "Fix exactly this and return the corrected program."})

    print(f"\nNOT ACCEPTED after {max_iters} iterations.")
    _save_transcript(out_dir, name, transcript, accepted=False)
    return 1


def _save_transcript(out_dir, name, transcript, accepted):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.transcript.json")
    with open(path, "w") as handle:
        json.dump({"accepted": accepted, "iterations": transcript}, handle, indent=2)
    print(f"transcript -> {path}")


def main(argv=None):
    p = argparse.ArgumentParser(description="write and verify protoemu firmware with an LLM")
    p.add_argument("--name", required=True, help="program name (output file stem)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec", help="the specification text")
    g.add_argument("--spec-file", help="a file holding the specification")
    p.add_argument("--proof", help="a formal/*.sby task to gate on, e.g. fw_uart_tx")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--max-iters", type=int, default=5)
    p.add_argument("--out-dir", default=os.path.join(tempfile.gettempdir(), "protoemu-ai"))
    args = p.parse_args(argv)
    spec = args.spec or open(args.spec_file).read()
    return run(spec, args.name, args.proof, args.model, args.base, args.max_iters, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
