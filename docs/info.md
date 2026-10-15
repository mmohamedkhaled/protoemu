<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This is a protocol emulator: a small programmable chip that runs communication protocols as firmware instead of having them built in. A host loads a short program over SPI, and the chip then drives and samples its eight protocol pins with cycle-exact timing. UART, SPI, I2C, JTAG and SWD are all programs in `firmware/`; a new protocol is a new program, with no change to the chip.

It is also built for hardware security work:

- The JTAG and SWD programs make the chip a debug-port probe. It can read a target's IDCODE, which is the first step in checking whether a device's debug port has been left open.
- A **trigger output** produces a pulse a programmable number of clock cycles after any event a program can recognise, such as the Nth clock of a target's bus transfer. That is the input a voltage or clock glitcher needs.
- A **cycle timer** lets a program measure how long a target takes to respond, to the clock cycle, which is the measurement behind a timing side channel.

### Architecture

- **Two state machines** (SM0 and SM1) run at the same time, so the chip can speak two protocols at once. They share one instruction memory of 32 words of 16 bits.
- **Instructions** in the style of the RP2040 PIO: `jmp`, `wait`, `in`, `out`, `push`, `pull`, `mov`, `set` and `nop`, plus `trig`/`tstart` for the trigger and timer and `irq`/`irqclr` for four shared flags that let one state machine signal the other. Every instruction can also drive up to three clock pins in the same cycle (side-set) and wait up to 31 ticks afterwards (delay). The instruction set is specified in [isa.md](isa.md).
- **Each state machine** has two 32-bit shift registers, two 32-bit scratch registers, a 4-word TX FIFO from the host, a 4-word RX FIFO to the host, and a 16.8-bit fractional clock divider that sets its tick rate.
- **The eight `uio` pins** are shared by both state machines. Each pin can be an input, a push-pull output or an open-drain output (for I2C).
- **An SPI slave port** gives the host access to a byte-wide register map: the instruction memory, each state machine's configuration and FIFOs, and the pins.

### Pinout

| Pin | Direction | Function |
|---|---|---|
| `ui[0]` | in | SCK, configuration SPI clock |
| `ui[1]` | in | CSn, configuration SPI chip select, active low |
| `ui[2]` | in | MOSI, configuration SPI data in |
| `ui[7:3]` | in | unused |
| `uo[0]` | out | MISO, configuration SPI data out |
| `uo[1]` | out | SM0 RX FIFO has data |
| `uo[2]` | out | SM0 TX FIFO has room |
| `uo[3]` | out | SM1 RX FIFO has data |
| `uo[4]` | out | SM1 TX FIFO has room |
| `uo[5]` | out | trigger pulse |
| `uo[7:6]` | out | 0 |
| `uio[7:0]` | in/out | protocol pins 0 to 7 |

Which protocol signal is on which `uio` pin is set by the host when it configures a state machine, not by the chip.

### Configuration SPI

SPI mode 0, most significant bit first. SCK must be at most one eighth of the chip clock.

A frame is one command byte followed by any number of data bytes while CSn stays low. The address increases by one after each data byte.

| Command byte | Then |
|---|---|
| `1aaaaaaa` | write: the host sends data bytes on MOSI, starting at address `a` |
| `0aaaaaaa` | read: the chip sends data bytes on MISO, starting at address `a` |

### Register map

State machine 0 is at `0x00`, state machine 1 at `0x20`. Offsets within a state machine:

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x00` | `CTRL` | RW | bit 0 enable. Write 1 to: bit 1 restart the state machine, bit 2 restart the clock divider, bit 3 clear both FIFOs |
| `0x01` | `STATUS` | R | bit 0 TX full, 1 TX empty, 2 RX full, 3 RX empty, 4 illegal instruction seen, 5 RX overrun, 6 stalled now |
| `0x02` | `FLEVEL` | R | bits 2:0 TX FIFO level, bits 6:4 RX FIFO level |
| `0x03` | `PC` | RW | program counter |
| `0x04` | `CLKDIV_FRAC` | RW | clock divider, fraction in 1/256 |
| `0x05`, `0x06` | `CLKDIV_INT` | RW | clock divider, integer, low byte first. Reset value 1 |
| `0x07` | `WRAP_BOTTOM` | RW | address the program wraps to |
| `0x08` | `WRAP_TOP` | RW | address after which it wraps. Reset value 31 |
| `0x09` | `PINCTRL0` | RW | bits 2:0 `IN_BASE`, bits 5:3 `OUT_BASE` |
| `0x0A` | `PINCTRL1` | RW | bits 2:0 `SET_BASE`, bits 5:3 `SET_COUNT` |
| `0x0B` | `PINCTRL2` | RW | bits 2:0 `SIDE_BASE`, bits 5:3 `JMP_PIN` |
| `0x0C` | `EXECCTRL` | RW | bits 1:0 `SIDE_CNT`, 2 `SIDE_OPT`, 3 `SIDE_PINDIR`, 4 `IN_DIR`, 5 `OUT_DIR`, 6 `AUTOPUSH`, 7 `AUTOPULL` |
| `0x0D` | `PUSH_THRESH` | RW | autopush threshold, 0 means 32 |
| `0x0E` | `PULL_THRESH` | RW | autopull threshold, 0 means 32 |
| `0x10`–`0x13` | `TXF` | W | TX FIFO word, low byte first. Writing `0x13` pushes the word |
| `0x14`–`0x17` | `RXF` | R | oldest RX FIFO word, low byte first. Reading `0x17` pops the word |

Shared registers:

| Address | Name | Access | Contents |
|---|---|---|---|
| `0x18` | `PIN_OUT` | RW | output level of each protocol pin |
| `0x19` | `PIN_DIR` | RW | 1 = the pin is an output |
| `0x1A` | `PIN_OD` | RW | 1 = open drain: the pin drives only when its level is 0 |
| `0x1B` | `PIN_IN` | R | the pins as the state machines see them |
| `0x1C` | `ID` | R | `0x5E` |
| `0x38`, `0x39` | `TRIG_DELAY` | RW | clock cycles from `trig` to the pulse, low byte first |
| `0x3A` | `TRIG_WIDTH` | RW | the pulse lasts this many clock cycles plus one |
| `0x3B` | `TRIG_CTRL` | RW | bit 0: invert the output (idle high). Write 1 to bit 1 to fire by hand. Bit 2: enable the scheduled-glitch match. Bit 7 reads 1 while a pulse is pending or active |
| `0x3C` | `FLAGS` | RW | the four shared inter-state-machine flags, in bits 3:0 |
| `0x3D`, `0x3E` | `TRIG_MATCH` | RW | 16-bit: with bit 2 of `TRIG_CTRL` set, the trigger fires on its own when the cycle timer reaches this value (a glitch scheduled a fixed number of cycles after `tstart`) |
| `0x40`–`0x7F` | `IMEM` | RW | instruction memory, word `n` at `0x40 + 2n`, low byte first. Read-back is valid only while SM0 is disabled |

When a state machine and the host write the same pin on the same clock, state machine 1 wins over state machine 0, which wins over the host. The shared flags follow the same idea: a flag a program sets wins over a host clear on the same clock.

### Timing

One tick of a state machine is `CLKDIV_INT + CLKDIV_FRAC/256` clock cycles. Each instruction takes one tick plus its delay. Pin outputs change on the clock edge of the tick. Pin inputs pass through a two-flop synchroniser, so a program sees a pin two clock cycles late; programs that react to a signal they just drove should use a divider of 4 or more.

## How to test

You need a host that can drive SPI, for example the RP2040 on the Tiny Tapeout demo board. The chip is rated at 25 MHz (estimated maximum about 43 MHz at the typical corner; see [efficiency.md](efficiency.md)); use any host clock up to that. The examples below assume a 50 MHz clock to show the divider arithmetic — scale the dividers to your actual clock with the same formula.

1. Assemble a program: `python tools/asm.py firmware/uart_tx.pio --format json`. The output has the instruction words and the configuration fields the program relies on. The header of each `.pio` file says which pins it needs and how the host exchanges data with it.
2. Read `ID` (`0x1C`) and check for `0x5E`.
3. Write the instruction words to `IMEM`.
4. Write the configuration registers of one state machine: the clock divider, the wrap addresses, the pin numbers and `EXECCTRL`.
5. Write `PIN_OUT`, `PIN_DIR` and `PIN_OD` for the initial pin state.
6. Write `CTRL` = `0x0E` to clear the state machine, `PC` to the program's first address, then `CTRL` = `0x01` to start it.
7. Write words to `TXF` and read words from `RXF`. Poll `STATUS`, or watch `uo[4:1]`.

A first test with nothing connected, UART transmit on `uio[0]` at 115200 baud:

| Step | SPI bytes (hex) |
|---|---|
| Load the 4-word program at address 0 | `C0  A0 9F  27 F7  01 60  42 06` |
| Divider 54 + 65/256, wrap 0 to 3 | `84  41 36 00  00 03` |
| Pins: all bases 0 | `89  00 00 00` |
| 1 side-set bit, optional; shift right | `8C  25` |
| `uio[0]` output, level 1 | `98  01 01 00` |
| Clear, `PC` = 0, enable | `80 0E`, `83 00`, `80 01` |
| Send the byte `0x4A` | `90  4A 00 00 00` |

`uio[0]` then carries one start bit, `0x4A` least significant bit first, and a stop bit, 434 clock cycles per bit.

The programs in `firmware/`:

| Program | Words | What it does |
|---|---|---|
| `uart_tx.pio` | 4 | UART transmit, 8N1 |
| `uart_rx.pio` | 8 | UART receive, 8N1, drops bytes with a bad stop bit |
| `spi_master.pio` | 2 | SPI master, mode 0 |
| `i2c_master.pio` | 16 | I2C master with START, STOP, repeated START and clock stretching |
| `jtag.pio` | 2 | JTAG bit pump: TMS and TDI out, TDO in |
| `swd.pio` | 12 | SWD host: write and read bit strings with turnaround |
| `ws2812.pio` | 4 | WS2812 LED driver, as an example of adding a protocol |
| `trigger_edge.pio` | 7 | fires the trigger on the Nth rising edge of a pin |
| `latency.pio` | 6 | measures a response time in clock cycles |
| `sniffer.pio` | 10 | passive logic analyser: timestamps every edge on the bus |
| `glitch_on_match.pio` | 13 | fires the trigger when a chosen byte appears on a watched bus |
| `armed_glitch_watch.pio` + `armed_glitch_fire.pio` | 12 + 5 | split across both state machines through a shared flag: one matches, the other fires |

Every program is tested in simulation against an independent model of its protocol; see the repository README.

A first test of the trigger with nothing connected: write `B8 0A 00 04 02` (delay 10, width 4, fire). `uo[5]` goes high 10 clock cycles later, for 5 clock cycles.

## External hardware

- An SPI host, 3.3 V, to load programs and exchange data.
- Whatever the chosen protocol talks to. For I2C, pull-up resistors on SDA and SCL. For JTAG or SWD, a target board at 3.3 V.
- For fault injection, a glitcher (for example a MOSFET crowbar on the target's supply) driven from `uo[5]`.
