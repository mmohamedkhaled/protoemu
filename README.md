![](../../workflows/gds/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/formal/badge.svg) ![](../../workflows/docs/badge.svg)

# protoemu: a protocol emulator ASIC that is also a debug-port probe

An entry for the [Jane Street Protocol Emulator ASIC Competition](https://blog.janestreet.com/protocol-emulator-asic-competition/), built for [Tiny Tapeout](https://tinytapeout.com) on IHP SG13CMOS5L (130 nm), 6x4 tiles.

The chip has no protocol built in. A host loads a short program over SPI and two small state machines then bit-bang the protocol on eight pins with cycle-exact timing. UART, SPI and I2C are programs.

What sets it apart is that it is built for hardware security work:

- **JTAG and SWD** are programs too, which turns the chip into a debug-port probe: it can read a target's IDCODE to check whether its debug port has been left open.
- **A glitch trigger.** A program can recognise an event on a bus, such as the Nth clock of a transfer, and fire a pulse a set number of clock cycles later; the pulse can also be scheduled to fire when the cycle timer reaches a chosen value, for a glitch a fixed number of cycles after an event. Its position and width are exact to one clock cycle, and that is proved formally, not just tested.
- **A cycle timer.** A program can measure a target's response time to the clock cycle, the raw measurement of a timing side channel.
- **A pattern-triggered glitch.** [`firmware/glitch_on_match.pio`](firmware/glitch_on_match.pio) watches a bus and fires the trigger only when a chosen byte appears, so the glitch lands on the instruction that matters, not a guess.
- **A passive bus sniffer.** [`firmware/sniffer.pio`](firmware/sniffer.pio) timestamps every edge on the pins to the clock cycle: a logic analyser built from firmware, with the chip driving nothing.
- **Split detection and action.** One state machine can watch a bus and raise a shared flag while the other waits on it and reacts, so the two run independently ([`firmware/armed_glitch_watch.pio`](firmware/armed_glitch_watch.pio) + [`firmware/armed_glitch_fire.pio`](firmware/armed_glitch_fire.pio)).

- [Datasheet](docs/info.md): pinout, register map, how to program it
- [Instruction set](docs/isa.md)
- [Timing, area and power](docs/efficiency.md)

## What is in the chip

| | |
|---|---|
| State machines | 2, running at the same time, sharing 32 words of program memory |
| Instructions | `jmp`, `wait`, `in`, `out`, `push`, `pull`, `mov`, `set`, `trig`, `tstart`, `irq`, `irqclr`, each with a side-set (clock pins) and a delay (0 to 31 ticks) |
| Security features | trigger pulse generator (16-bit delay, 8-bit width, in clock cycles) on a dedicated pin, fired by a program, by hand, or when the cycle timer reaches a scheduled value; 32-bit cycle timer readable by programs; 4 shared flags for one state machine to signal the other |
| Per state machine | 32-bit `X`, `Y`, input and output shift registers; 4-word TX and RX FIFOs; 16.8 fractional clock divider |
| Pins | 8 bidirectional protocol pins, push-pull or open drain |
| Host port | SPI slave, byte-wide register map |

The instruction set follows the RP2040 PIO closely enough to feel familiar, with these differences: 3-bit pin numbers, an open-drain pin mode, no `exec`, command dispatch through `out pc`, the trigger and timer instructions, and four shared inter-state-machine flags (`irq`/`irqclr`/`wait irq`) in place of PIO's IRQ system.

## Programs

| Program | Words | Tested against |
|---|---|---|
| [`uart_tx.pio`](firmware/uart_tx.pio) | 4 | a UART receiver model, with edge timing checked to the clock cycle |
| [`uart_rx.pio`](firmware/uart_rx.pio) | 8 | a UART transmitter model, including a framing error |
| [`spi_master.pio`](firmware/spi_master.pio) | 2 | an SPI mode 0 target model |
| [`i2c_master.pio`](firmware/i2c_master.pio) | 16 | an I2C memory model: write, repeated START, read, NACK, clock stretching |
| [`jtag.pio`](firmware/jtag.pio) | 2 | an IEEE 1149.1 TAP model: IDCODE read, then BYPASS through the instruction register |
| [`swd.pio`](firmware/swd.pio) | 12 | an ADIv5 SW-DP model: line reset, JTAG-to-SWD switch, DPIDR read with parity |
| [`ws2812.pio`](firmware/ws2812.pio) | 4 | a pulse-width decoder; a protocol added with no RTL change |
| [`trigger_edge.pio`](firmware/trigger_edge.pio) | 7 | a pulse train with known edges: the trigger lands on the predicted clock cycle for several edge counts, delays and widths |
| [`latency.pio`](firmware/latency.pio) | 6 | a target with a known response time, from 1 to 4,321 cycles: the measurement is exact every time |
| [`sniffer.pio`](firmware/sniffer.pio) | 10 | a passive logic analyser: a watched clock is captured, timestamp and pin state per edge, to the cycle |
| [`glitch_on_match.pio`](firmware/glitch_on_match.pio) | 13 | watches a byte stream and fires the trigger only on a chosen byte, at the right cycle, never on others |
| [`armed_glitch_watch.pio`](firmware/armed_glitch_watch.pio) + [`armed_glitch_fire.pio`](firmware/armed_glitch_fire.pio) | 12 + 5 | one state machine matches a byte and raises a shared flag, the other waits on it and fires the trigger |

Assemble one with `python tools/asm.py firmware/uart_tx.pio --list`.

## Verification

Verification is half of this project. There are five layers, and each one checks the layer below it against something written independently.

1. **A reference model of the instruction set.** [`test/models/isa_model.py`](test/models/isa_model.py) is written from [docs/isa.md](docs/isa.md), not from the RTL. The state-machine test runs the RTL and the model in lock step over 150 constrained-random programs, with random configuration, pins and FIFO back-pressure, and compares all state and all outputs after every clock cycle.
2. **Unit tests for every RTL block** (clock divider, shift registers, FIFO, decoder, state machine, trigger, memory, core, SPI port). The decoder test covers all 65,536 instruction words.
3. **Whole-chip protocol tests.** Each program is loaded through the SPI port, as a real host would, and what appears on the pins is checked by a model of the protocol's other end. These tests touch only the chip's pins, so they also run on the gate-level netlist.
4. **Formal proofs** with SymbiYosys ([`formal/`](formal)), for every instruction stream and configuration:
   - the decoder accepts exactly the legal encodings, and an instruction's timing bits and its operation never affect each other;
   - the delay guarantee: after an instruction with delay `d`, the next one is presented exactly `d + 1` ticks later, and nothing reaches the pins in between;
   - instructions are atomic: a stalled instruction changes nothing but its side-set pins;
   - the FIFOs are never overrun or underrun;
   - the trigger pulse is high exactly from `delay` to `delay + width` clock cycles after it is fired, for every delay and width, and the timer reads exactly the number of cycles since it was started.
5. **Formal proofs of the firmware itself.** [`formal/fw_gen.py`](formal/fw_gen.py) assembles each program in `firmware/` with the same assembler the host uses and wires it into the real state machine, so the proofs are about the shipped programs on the shipped RTL:

   | Program | Proved | For |
   |---|---|---|
   | `spi_master` | MOSI changes only while SCK is low | all time, every clock divider, every byte, any FIFO timing |
   | `jtag` | TMS and TDI change only while TCK is low | the same |
   | `swd` | SWDIO's level and direction change only while SWCLK is low, so the line is handed over cleanly | the same, for any sequence of `write` and `read` commands |
   | `i2c_master` | SDA changes while SCL is high only inside `start` and `stop`, so no byte can look like a START or STOP; the lines are only ever released or pulled low | all time, any command sequence, and any target behaviour, clock stretching included |
   | `uart_tx` | the TX pin carries a correct 8N1 frame with each bit exactly 8 ticks, and is high between frames | every byte and any FIFO timing, over the first frame and the start of the next (a bounded proof) |

   Breaking any of these programs by one instruction makes its proof fail; that was checked for the SPI, I2C and UART programs.

   On top of these, a **well-formedness proof** ([`formal/fw_valid.sby`](formal/fw_valid.sby)) shows unconditionally that eleven of the thirteen shipped programs never execute an illegal instruction and never run outside their own code, for any input, FIFO state or clock divider. The two command-dispatch programs (`swd`, `i2c_master`) use `out pc`, so that holds for them only when the host sends valid commands, which is the assumption their own proofs already make. The proof found that distinction, it was not assumed.

The assembler has its own tests, including a round trip through the disassembler for every instruction word.

## AI-assisted firmware, gated by proof

The competition asks for AI-assisted verification. Here it is the other way round too: verification gates the AI. [`tools/ai_firmware.py`](tools/ai_firmware.py) has a language model write a `.pio` program from a plain-English spec, and keeps it only if it clears three gates:

1. it assembles with [`tools/asm.py`](tools/asm.py);
2. it passes the **well-formedness proof on the real state-machine RTL** (never executes an illegal instruction, never leaves its code), generated on the fly by [`formal/fw_gen.py`](formal/fw_gen.py);
3. optionally, a named correctness proof such as `fw_uart_tx` (the full 8N1 frame proof).

Any failure — a bad mnemonic, an out-of-range jump, a wrong waveform — is fed back to the model, which tries again, up to a limit. The model does not have to be right; the proofs decide what ships. In testing, a UART transmitter written from a spec passed the assembler, the well-formedness proof and the frame-correctness proof in one iteration; a square-wave generator was accepted on the second iteration after the first was caught misusing side-set.

```sh
export AI_API_KEY=...       # your API key, kept in the environment, never in the repo
export AI_BASE_URL=...      # any OpenAI-compatible chat endpoint
export AI_MODEL=...         # the model to use
python tools/ai_firmware.py --name square --spec-file tools/specs/square_wave.txt
python tools/ai_firmware.py --name uart_tx --spec-file tools/specs/uart_tx.txt --proof fw_uart_tx
```

The endpoint, model and key are taken from those environment variables; any OpenAI-compatible chat API works. The gate logic has offline tests in [`tools/test_ai_firmware.py`](tools/test_ai_firmware.py).

### Running it

Requirements: Icarus Verilog, Python 3.11 to 3.13 with `pip install -r test/requirements.txt`, and for the proofs SymbiYosys, Yosys and yices (all in [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)).

```sh
make -C test            # everything: assembler, unit tests, whole chip
make -C test sm         # one block (clkdiv shift fifo decoder sm trigger imem core cfg_spi), or `asm`, or `top`
make -C formal          # every proof, about 3 minutes
make -C formal firmware # only the firmware proofs
tools/area.sh           # synthesis estimate of cell count and area
```

### Status

| Check | Result |
|---|---|
| RTL tests | 114 pass |
| Formal | 21 proofs pass: decoder, state machine, trigger, 5 firmware correctness proofs, and well-formedness of 11 more programs; cover points reached |
| Gate-level simulation | the 18 whole-chip tests pass on a Yosys netlist mapped to the IHP cells |
| Area estimate (Yosys, typical corner) | 12,273 cells (about half the 24K-cell budget), 1,762 flip-flops, 200,000 µm² of cells: 28% of the 0.72 mm² the competition post gives for 24 tiles |
| Lint | Verilator `-Wall` clean |
| Timing (OpenSTA, typical, pre-layout) | critical path ~20–23 ns, Fmax ~43–49 MHz; rated at 25 MHz for slow-corner margin (see [docs/efficiency.md](docs/efficiency.md)) |
| GDS flow and sign-off timing | not run yet; it runs in the `gds` GitHub workflow |

## Repository layout

| Path | Contents |
|---|---|
| `src/` | RTL. `project.v` is the top level; `pe_*.v` are the blocks |
| `firmware/` | protocol programs |
| `tools/` | assembler, its tests, the area script |
| `test/` | cocotb tests, the host driver (`host.py`), the board and protocol models |
| `formal/` | SymbiYosys proofs |
| `docs/` | datasheet and instruction set |

## License

Apache 2.0. See [LICENSE](LICENSE).
