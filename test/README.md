# Tests

The tests use [cocotb](https://docs.cocotb.org/en/stable/) with Icarus Verilog. The project README explains what each layer checks.

## How to run

```sh
make -B                 # everything; results are merged into results.xml
make -B sm              # one RTL block: clkdiv shift fifo decoder sm trigger imem core cfg_spi
make -B asm             # the assembler's tests (pytest)
make -B top             # the whole-chip tests in test.py
```

`cocotb.mk` holds the rules for one cocotb run; `Makefile` calls it once per testbench. When you add a source file, add it to `PROJECT_SOURCES` in `cocotb.mk` and to `info.yaml`.

To run the whole-chip tests on a gate-level netlist, first harden the project and copy `../runs/wokwi/results/final/verilog/gl/tt_um_mmohamedkhaled_protoemu.v` to `gate_level_netlist.v`. Then run:

```sh
make -B GATES=yes
```

## What is where

| File | Contents |
|---|---|
| `test_<block>.py` | unit test of `src/pe_<block>.v` |
| `test.py` | whole-chip tests: every program in `firmware/`, loaded over SPI |
| `host.py` | the register map and the host-side driver, over the register bus or over SPI |
| `board.py` | resolves the eight bidirectional pads between the chip and the device models |
| `models/isa_model.py` | executable reference model of the instruction set |
| `models/*.py` | the other end of each protocol: UART, SPI, I2C, JTAG TAP, SWD target, WS2812, and devices with known timing |

## How to view the waveform file

The whole-chip run writes `tb.fst`.

```sh
gtkwave tb.fst tb.gtkw
```

or

```sh
surfer tb.fst
```
