# Timing, area and power

How fast the chip runs, how much room it uses, and where its power goes. The
numbers are reproducible with `tools/area.sh` (synthesis with Yosys for the IHP
SG13CMOS5L cells, plus OpenSTA for the critical path). They are pre-layout
estimates at the typical corner; the authoritative figures come from the `gds`
GitHub workflow, which places and routes the design and signs off at the slow
corner.

## Speed

`tools/area.sh` reports the longest register-to-register path with OpenSTA:

| | |
|---|---|
| Critical path (typical, pre-layout) | about 20–23 ns |
| Estimated Fmax (typical, pre-layout) | about 43–49 MHz |
| Rated clock (`info.yaml`, `CLOCK_PERIOD`) | 25 MHz (40 ns) |

The estimate moves by several MHz between synthesis runs, because the path is
dominated by a couple of high-fanout nets whose delay depends on how the mapper
happens to size them — more evidence that the real number must come from the
flow, not from here.

The rated clock is set below the estimate on purpose. Two things make the real
sign-off slower than this estimate: the GDS flow signs off at the **slow** corner
(1.08 V, 125 °C), which is roughly 0.6–0.7× of the typical corner used here, and
layout adds wire delay. Two things make it faster: the resizer buffers the
high-fanout nets that dominate the pre-layout path (about 7 of the 23 ns are two
such nets, which this estimate does not buffer), and gate sizing. 25 MHz leaves
margin for the flow to close; if the `gds` run shows headroom, raise
`CLOCK_PERIOD`.

A protocol's speed scales with the clock through the per-state-machine divider.
At 25 MHz, with a divider of 4, SPI and JTAG run at about 6 MHz; UART reaches
common baud rates exactly with the fractional divider (for example 25 MHz /
(8 × 115200) = 27.13, which the divider realises as 27 + 34/256). The functional
tests use a 50 MHz simulation clock to exercise the divider arithmetic; that is
a simulation convenience and does not change the silicon's rated maximum — on
hardware, pick any host clock up to Fmax and scale the dividers with
`host.clkdiv_for()`.

## Area

Whole design, from `tools/area.sh`:

| | |
|---|---|
| Mapped cells | about 12,400 of a ~24,000 budget (52%) |
| Flip-flops | about 1,770 |
| Cell area | about 201,000 µm², 46% of the 24-tile (6×4) budget |

Per block (one instance each, typical corner):

| Block | Cell area µm² | Share | Note |
|---|---|---|---|
| `pe_imem` | 48,400 | 24% | 32 × 16-bit instruction memory in flip-flops |
| `pe_shift` ×2 | 21,500 each | 21% | 32-bit input/output shift registers and the barrel shifter |
| `pe_sm` ×2 | 20,100 each | 20% | decode, X/Y, the execute logic |
| `pe_fifo` ×4 | 10,600 each | 21% | the two TX and two RX FIFOs |
| `pe_trigger` | 5,500 | 3% | pulse generator and cycle timer |
| rest | — | — | clock dividers, config port, pins |

The single largest block is the instruction memory, and it is flip-flop based.
The competition post notes that SRAM is usually more area-efficient for
instruction memory, and this is where that would pay off: `pe_imem` is the only
block that would change, its interface is a byte write port and two read ports,
and replacing it with the Tiny Tapeout SRAM macro or a latch array would reclaim
a large part of that 48,000 µm². It is left as flip-flops here so the design has
no macro dependency and hardens with the stock flow; the swap is the first move
if more area is needed, for example to take the 8×4 tile option.

## Power

No power numbers are measured yet (that needs the placed netlist and switching
activity from the `gds` flow), but the design is built to keep dynamic power
low, and the structure says where it goes.

- **Dynamic power follows activity, and activity follows the clock divider.** A
  state machine only changes state on a tick, and the divider makes ticks as
  rare as the protocol allows — at divider 100, the datapath is idle 99 cycles
  out of 100. Running a protocol slowly costs proportionally little.
- **A disabled state machine does not switch.** Its clock divider holds when
  `en` is low, so it produces no ticks and its datapath, shift registers and
  counters keep their values. The whole chip freezes when `ena` is low. Both are
  checked in simulation (`test_core.test_ena_low_freezes_everything`, and the
  divider's `test_enable_holds_and_restart`).
- **The instruction memory does not switch in use.** Its flip-flops change only
  when the host loads a program; during operation they are read-only, so the
  largest block by area contributes almost nothing to dynamic power. It does
  cost static (leakage) power, which is the other reason the SRAM swap above
  would help.
- **The cycle timer is the one always-on counter**, 32 flip-flops that advance
  every clock while `ena` is high. It is small next to the whole design, and it
  has to run continuously for the response-time measurement to be exact.

In short: the levers are the clock divider (turn protocols down when idle),
leaving state machines disabled when unused, and, for a bigger area and leakage
cut, moving the instruction memory to SRAM in the hardening phase.
