#!/usr/bin/env python3
"""Make the IHP sg13cmos5l cell models simulate under a zero-delay functional run.

The sequential cell models declare timing-check "delayed" nets and drive them
from `$setuphold` / `$recrem` inside a `specify` block, then feed those nets into
the flip-flop and latch primitives:

    wire delayed_D, delayed_RESET_B, delayed_CLK;
    ...
    ihp_dff_r (int_fwire_IQ, notifier, delayed_CLK, delayed_D, int_fwire_r, ...);
    specify
        $setuphold (posedge CLK, posedge D, 0.0, 0.0, notifier,,, delayed_CLK, delayed_D);
    endspecify

Icarus Verilog does not drive `specify` delayed nets, so every sequential cell
reads X and the whole netlist is X. A functional (zero-delay) gate-level
simulation does not use timing checks at all, so we tie each delayed net
straight to its source signal:

    wire delayed_D = D; wire delayed_RESET_B = RESET_B; wire delayed_CLK = CLK;

Only the timing-check wiring is touched; no cell logic is changed. This is the
exact patch used for local gate-level simulation and in the `gl_test` CI job.
Run it on the installed PDK copy of the model; it is never committed back.

    python3 test/patch_gl_models.py <...>/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v
"""
import re
import sys

# A `wire` declaration whose nets are all `delayed_*`. The nets carry no
# initialiser, so an already-patched line (`wire delayed_D = D;`) does not match
# and the script is safe to run twice.
DECL = re.compile(r"wire\s+(delayed_[A-Za-z0-9_]+(?:\s*,\s*delayed_[A-Za-z0-9_]+)*)\s*;")


def _tie(match):
    nets = [n.strip() for n in match.group(1).split(",")]
    return " ".join(f"wire {n} = {n[len('delayed_'):]};" for n in nets)


def patch(text):
    return DECL.sub(_tie, text)


def main(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = patch(src)
    tied = len(re.findall(r"wire delayed_[A-Za-z0-9_]+ = ", out))
    if tied == 0:
        sys.exit(f"patch_gl_models: no timing-check nets found in {path} "
                 f"(already patched, or wrong file?)")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"patch_gl_models: tied {tied} timing-check nets to their sources in {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: patch_gl_models.py <sg13cmos5l_stdcell.v>")
    main(sys.argv[1])
