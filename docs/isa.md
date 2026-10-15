# protoemu ISA

Status: **v1.0, implemented** in `src/`. The Python model in `test/models/isa_model.py` is an executable form of this document, and the RTL is checked against it instruction by instruction.

This is the instruction set of the protocol-emulator core in `tt_um_mmohamedkhaled_protoemu`. It is modelled on the RP2040 PIO, cut down to fit 24 Tiny Tapeout tiles and extended where JTAG, SWD and I2C need it. It is PIO-like, not binary compatible with PIO.

Section 9 lists the design's additions beyond a minimal UART/SPI/I2C PIO-style core. The register map, the pinout and the host protocol are in `docs/info.md`.

## 1. Machine model

Two identical state machines, SM0 and SM1, share one instruction memory of 32 words of 16 bits. Each state machine has:

| State | Width | Reset | Purpose |
|---|---|---|---|
| `PC` | 5 | 0 | Address of the current instruction |
| `X`, `Y` | 32 | 0 | Scratch registers and loop counters |
| `ISR` | 32 | 0 | Input shift register |
| `OSR` | 32 | 0 | Output shift register |
| `isr_count` | 6 | 0 | Bits shifted into `ISR` since it was last cleared (0 to 32) |
| `osr_count` | 6 | 32 | Bits shifted out of `OSR` since it was last loaded (0 to 32); 32 means empty |
| TX FIFO | 4 × 32 | empty | Host to state machine |
| RX FIFO | 4 × 32 | empty | State machine to host |

Shared between both state machines:

| State | Width | Reset | Purpose |
|---|---|---|---|
| `PIN_OUT` | 8 | 0 | Output level of each `uio` pin |
| `PIN_DIR` | 8 | 0 | Direction of each `uio` pin, 1 = drive |
| `PIN_OD` | 8 | 0 | Open-drain mode of each `uio` pin (host-written only) |
| `TIMER` | 32 | 0 | Counts clock cycles (section 4.8) |
| Trigger | | idle | Pulse generator driving `uo_out[5]` (section 4.8) |
| `FLAGS` | 4 | 0 | Shared inter-state-machine flags (section 4.9) |

Pins are numbered 0 to 7 and are the eight `uio` pins. All pin indices wrap modulo 8.

### 1.1 Ticks

Each state machine has its own clock divider, which produces a one-cycle enable pulse called a **tick**. A state machine changes state only on a rising `clk` edge where its tick is high, it is enabled, and `ena` is high. Every timing statement in this document is in ticks of the state machine concerned.

The divider is `CLKDIV_INT` (16 bits) plus `CLKDIV_FRAC` (8 bits):

- Tick period is `CLKDIV_INT + CLKDIV_FRAC/256` clock cycles on average. `CLKDIV_INT = 0` means 65536.
- `CLKDIV_INT = 1`, `CLKDIV_FRAC = 0` ticks on every clock cycle.
- With a non-zero fraction, each period is either `CLKDIV_INT` or `CLKDIV_INT + 1` cycles, chosen by an 8-bit accumulator that adds `CLKDIV_FRAC` at every tick and stretches the next period by one cycle on carry. Jitter is therefore at most one clock cycle.
- `CLKDIV_INT = 1` with a non-zero fraction is not allowed.

### 1.2 Execution

On each tick a state machine does exactly one of these:

1. **Execute** the instruction at `PC`, then set the delay counter to the instruction's delay value and advance `PC`.
2. **Stall** on the instruction at `PC`, because its stall condition holds. Only the side-set is applied; no other state changes.
3. **Delay**, if the delay counter is non-zero: decrement it and do nothing else.

An instruction is atomic: on any tick it either takes full effect or has no effect apart from its side-set. This is proved formally (`formal/sm.sby`).

The autopull refill of `OSR` (section 4.4) is not part of any instruction. It can happen on any tick, including stall and delay ticks.

There is no fetch or branch penalty. A taken `JMP` costs one tick, the same as any other instruction.

`PC` advance: if the instruction wrote `PC` (taken `JMP`, `OUT PC`, `MOV PC`), that value is used. Otherwise, if `PC == WRAP_TOP` then `PC ← WRAP_BOTTOM`, else `PC ← PC + 1` modulo 32.

### 1.3 Pin timing

- An instruction that executes on clock edge E changes `uio_out` / `uio_oe` at edge E.
- Inputs pass through a two-flop synchroniser. An instruction that executes on edge E sees the pad value that was sampled at edge E − 2.
- So a state machine reading back its own output sees it three clock cycles after the write. This is a fixed number of clock cycles, not ticks.

Pad drive, per pin: `uio_out = PIN_OUT`. `uio_oe = PIN_DIR` normally, and `uio_oe = PIN_DIR & ~PIN_OUT` when `PIN_OD` is set, so writing 1 releases the pin and writing 0 drives it low.

Write priority when more than one source writes the same pin bit on the same clock edge, highest first: SM1 side-set, SM1 `OUT`/`SET`, SM0 side-set, SM0 `OUT`/`SET`, host.

## 2. Instruction format

```
 15  14  13 | 12  11  10   9   8 |  7   6   5   4   3   2   1   0
   opcode   |  delay / side-set  |            operands
```

| Opcode | Mnemonic | Operand bits 7:0 |
|---|---|---|
| `000` | `JMP` | `cond[7:5]` `addr[4:0]` |
| `001` | `WAIT` | `pol[7]` `src[6:5]` `idx[4:0]` |
| `010` | `IN` | `src[7:5]` `count[4:0]` |
| `011` | `OUT` | `dst[7:5]` `count[4:0]` |
| `100` | `PUSH` / `PULL` | `dir[7]` `if[6]` `block[5]` `0[4:0]` |
| `101` | `MOV` | `dst[7:5]` `op[4:3]` `src[2:0]` |
| `110` | `TRIG`/`TSTART`/`IRQ` | `op[7:5]` operand |
| `111` | `SET` | `dst[7:5]` `data[4:0]` |

Any encoding marked reserved, and any non-zero value in a field shown as `0`, is an **illegal instruction**. It executes as a no-op: side-set and delay are honoured, `PC` advances, no other state changes, and the sticky `ILLEGAL` status flag of that state machine is set.

## 3. Delay and side-set field

Bits 12:8 are split between side-set and delay by three per-SM configuration fields:

| Field | Meaning |
|---|---|
| `SIDE_CNT` (0 to 3) | Number of side-set data bits |
| `SIDE_OPT` | 1 = bit 12 is a per-instruction side-set enable |
| `SIDE_PINDIR` | 1 = side-set writes `PIN_DIR` instead of `PIN_OUT` |

Layout, from bit 12 down: the enable bit if `SIDE_OPT` is set, then `SIDE_CNT` side-set data bits, then the delay in all remaining bits.

| `SIDE_OPT` | `SIDE_CNT` | Delay bits | Maximum delay |
|---|---|---|---|
| 0 | 0 | 5 | 31 |
| 0 | 1 | 4 | 15 |
| 0 | 2 | 3 | 7 |
| 0 | 3 | 2 | 3 |
| 1 | 1 | 3 | 7 |
| 1 | 2 | 2 | 3 |
| 1 | 3 | 1 | 1 |

**Side-set.** The data bits are written to pins `SIDE_BASE`, `SIDE_BASE + 1`, … with the least significant data bit on `SIDE_BASE`. The write happens on every tick the instruction is at `PC` and not in its delay, whether it executes or stalls on that tick, and the pins then hold the value. If `SIDE_OPT` is set and the enable bit is 0, the pins are not touched. If side-set and the same instruction's `OUT` or `SET` target the same pin, side-set wins.

**Delay guarantee.** If an instruction with delay value `d` executes on tick `t`, the next instruction is at `PC` on tick `t + d + 1`. On ticks `t + 1` to `t + d` no pin is written and no architectural state changes other than the delay counter and an autopull refill of `OSR`. Stall ticks come before the execute tick and are not counted in `d`. This is proved formally for every instruction stream (`formal/sm.sby`).

## 4. Instructions

### 4.1 JMP — `000 ddddd ccc aaaaa`

Set `PC` to `addr` if the condition holds.

| `cond` | Assembly | Condition |
|---|---|---|
| `000` | `jmp addr` | Always |
| `001` | `jmp !x addr` | `X == 0` |
| `010` | `jmp x-- addr` | `X != 0` before the decrement; `X` is then decremented whether or not the jump is taken |
| `011` | `jmp !y addr` | `Y == 0` |
| `100` | `jmp y-- addr` | As `x--`, for `Y` |
| `101` | `jmp x!=y addr` | `X != Y` |
| `110` | `jmp pin addr` | Pin `JMP_PIN` is high |
| `111` | `jmp !osre addr` | `osr_count < PULL_THRESH`, that is, `OSR` still holds unsent bits |

Never stalls.

### 4.2 WAIT — `001 ddddd p ss iiiii`

Stall until a pin equals `pol`.

| `src` | Assembly | Pin tested |
|---|---|---|
| `00` | `wait <pol> gpio <idx>` | Pin `idx` |
| `01` | `wait <pol> pin <idx>` | Pin `IN_BASE + idx` |
| `10` | `wait <pol> irq <idx>` | Shared flag `idx`, `idx` 0 to 3 (section 4.9) |
| `11` | reserved | |

`idx` must be 0 to 7 for a pin, 0 to 3 for a flag. The instruction executes, with no effect, on the first tick the condition holds. Its side-set is applied from the first stalled tick, which is what makes I2C clock stretching one instruction: release SCL by side-set and wait for SCL high.

### 4.3 IN — `010 ddddd sss nnnnn`

Shift `n` bits from the source into `ISR`. `count = 0` means `n = 32`.

| `src` | Assembly | Source value |
|---|---|---|
| `000` | `in pins, n` | Pins, with pin `IN_BASE` in bit 0, zero-extended above bit 7 |
| `001` | `in x, n` | `X` |
| `010` | `in y, n` | `Y` |
| `011` | `in null, n` | Zero |
| others | reserved | |

The low `n` bits of the source are used. With `IN_DIR = 1` (right) `ISR` shifts right by `n` and the new bits enter at the top; with `IN_DIR = 0` (left) `ISR` shifts left by `n` and the new bits enter at the bottom. `isr_count` increases by `n`, saturating at 32.

**Autopush.** If `AUTOPUSH` is set and the new `isr_count` would reach `PUSH_THRESH`: when the RX FIFO has room, the shifted value is pushed and `ISR` and `isr_count` are cleared, all on the same tick; when the RX FIFO is full, the instruction stalls and the pins are sampled on the tick it eventually executes.

### 4.4 OUT — `011 ddddd ddd nnnnn`

Shift `n` bits out of `OSR` to the destination. `count = 0` means `n = 32`.

| `dst` | Assembly | Effect |
|---|---|---|
| `000` | `out pins, n` | Write `PIN_OUT` of pins `OUT_BASE` … `OUT_BASE + n − 1` (at most 8 pins) |
| `001` | `out x, n` | `X ←` the bits, zero-extended |
| `010` | `out y, n` | `Y ←` the bits, zero-extended |
| `011` | `out null, n` | Discard |
| `100` | `out pindirs, n` | As `pins`, writing `PIN_DIR` |
| `101` | `out pc, n` | `PC ←` low 5 bits |
| `110`, `111` | reserved | |

With `OUT_DIR = 1` (right) the bits come from the bottom of `OSR`; with `OUT_DIR = 0` (left) they come from the top. Either way the `n` bits keep their order and are right-justified in the destination. Zeros shift in. `osr_count` increases by `n`, saturating at 32.

**Autopull.** If `AUTOPULL` is set:

- `OUT` stalls while `osr_count >= PULL_THRESH` at the start of the tick.
- On every tick, after that tick's instruction has taken effect, if `osr_count >= PULL_THRESH` and the TX FIFO is not empty, `OSR` is loaded from the FIFO and `osr_count` is cleared. So the `OUT` that uses the last bit also triggers the refill, and a stream of `OUT`s has no gap as long as the FIFO has data.
- Because `osr_count` resets to 32, the first word is loaded on the first tick after the state machine is enabled, and an `OUT` at that first tick stalls for one tick.

Without `AUTOPULL`, `OUT` never stalls and shifts out zeros once `OSR` is empty.

With `AUTOPULL` set, an explicit `pull` discards whatever the refill has already loaded. Use `pull ifempty`, or do not mix the two.

### 4.5 PUSH and PULL — `100 ddddd 0 f b 00000` and `100 ddddd 1 f b 00000`

**`push [iffull] [block|noblock]`** moves `ISR` to the RX FIFO and clears `ISR` and `isr_count`.

- `if = 1` (`iffull`): do nothing unless `isr_count >= PUSH_THRESH`.
- RX FIFO full, `block = 1`: stall.
- RX FIFO full, `block = 0`: clear `ISR` and `isr_count`, drop the data, set the sticky `RXOVER` flag.

**`pull [ifempty] [block|noblock]`** loads `OSR` from the TX FIFO and clears `osr_count`.

- `if = 1` (`ifempty`): do nothing unless `osr_count >= PULL_THRESH`.
- TX FIFO empty, `block = 1`: stall.
- TX FIFO empty, `block = 0`: `OSR ← X`, `osr_count ← 0`.

The assembler default is `block`.

### 4.6 MOV — `101 ddddd ddd oo sss`

`dst ← op(src)`.

| Code | `dst` | `src` |
|---|---|---|
| `000` | reserved | `pins` (as for `IN`) |
| `001` | `x` | `x` |
| `010` | `y` | `y` |
| `011` | reserved | `null` (zero) |
| `100` | reserved | reserved |
| `101` | `pc` (low 5 bits) | `timer` (section 4.8) |
| `110` | `isr` (also clears `isr_count`) | `isr` |
| `111` | `osr` (also clears `osr_count`) | `osr` |

| `op` | Assembly | Operation |
|---|---|---|
| `00` | `mov dst, src` | Copy |
| `01` | `mov dst, !src` | Bitwise invert |
| `10` | `mov dst, ::src` | Reverse the order of all 32 bits |
| `11` | reserved | |

Never stalls. `mov y, y` is the canonical `nop`.

### 4.7 SET — `111 ddddd ddd vvvvv`

| `dst` | Assembly | Effect |
|---|---|---|
| `000` | `set pins, v` | Write `PIN_OUT` of pins `SET_BASE` … `SET_BASE + SET_COUNT − 1` |
| `001` | `set x, v` | `X ← v`, upper bits cleared |
| `010` | `set y, v` | `Y ← v`, upper bits cleared |
| `100` | `set pindirs, v` | As `pins`, writing `PIN_DIR` |
| others | reserved | |

`SET_COUNT` is 0 to 5. Never stalls.

### 4.8 TRIG, TSTART and IRQ — opcode `110`

Opcode `110` holds four instructions, chosen by `op` in bits 7:5: `000` TRIG, `001` TSTART, `010` IRQ set, `011` IRQ clear (`1xx` reserved). TRIG and TSTART take no operand (bits 4:0 are zero); IRQ set and clear take a flag index in bits 1:0 (bits 4:2 zero). IRQ is specified in section 4.9; TRIG and TSTART follow.

These two instructions, with the `timer` source of `MOV`, are for hardware security work: placing a fault-injection pulse relative to a protocol event, and measuring a response time. The trigger and the timer are shared by both state machines and count **clock cycles, not ticks**, so their resolution is one clock cycle whatever the dividers are.

**`trig`** fires the trigger pulse generator. If it executes on clock edge E, `uo_out[5]` goes active at edge `E + TRIG_DELAY` and inactive again `TRIG_WIDTH + 1` clock cycles later. With `TRIG_DELAY = 0` the pulse starts on the same edge as a pin write by that instruction would. Firing again while a pulse is pending or active starts over. `TRIG_DELAY` (16 bits), `TRIG_WIDTH` (8 bits) and the polarity are host registers; the host can also fire the trigger directly.

**`tstart`** restarts `TIMER`. If it executes on clock edge E, a `mov <dst>, timer` that executes on edge `E + k` reads `k`.

Neither instruction stalls, and a stalled or delayed instruction never fires the trigger. The pulse position and length are proved formally for every delay and width (`formal/trigger.sby`).

```
    tstart          side 1    ; stimulus pin up and timer started on the same clock
    wait 1 pin 0              ; the target responds
    mov isr, timer            ; clock cycles it took, plus a fixed 4
    push block
```

### 4.9 IRQ set and clear — `110 ddddd 010 000nn` and `110 ddddd 011 000nn`

`irq <n>` sets shared flag `n`, `irqclr <n>` clears it, for `n` 0 to 3. The four flags are shared by both state machines and the host (section 6). Setting and clearing take effect on the tick the instruction executes, like a pin write, and never stall.

A flag set by one state machine, waited on with `wait <pol> irq <n>` by the other, lets the two coordinate: one can watch a bus and raise a flag while the other waits on it and acts. When a set and a clear of the same flag land on one clock, the set wins, and a state machine wins over the host, so a flag a program raises is seen even if the host clears it in the same cycle.

## 5. Timing summary

Every instruction takes `1 + delay` ticks plus any stall ticks before it.

| Instruction | Stalls while | Takes effect |
|---|---|---|
| `JMP` | never | `PC` (and `X` or `Y` for `x--`, `y--`) on the execute tick |
| `WAIT` | pin ≠ `pol` | nothing; execute tick is the first tick the pin matches |
| `IN` | autopush due and RX FIFO full | `ISR` on the execute tick; pins seen as sampled two clocks earlier |
| `OUT` | autopull on and `OSR` empty | destination on the execute tick; pins change at that clock edge |
| `PUSH` | blocking and RX FIFO full | FIFO and `ISR` on the execute tick |
| `PULL` | blocking and TX FIFO empty | `OSR` on the execute tick |
| `MOV` | never | destination on the execute tick |
| `SET` | never | destination on the execute tick; pins change at that clock edge |
| `TRIG` | never | trigger fires on the execute tick; the pulse starts `TRIG_DELAY` clocks later |
| `TSTART` | never | `TIMER` restarts on the execute tick |
| `IRQ` set/clear | never | the flag changes on the execute tick |
| side-set | — | every tick the instruction is at `PC` and not in its delay, stalled or not |

## 6. Per-state-machine configuration

Written by the host through the SPI configuration port. Register addresses and the SPI framing are in `docs/info.md`.

| Field | Bits | Meaning |
|---|---|---|
| `CLKDIV_INT`, `CLKDIV_FRAC` | 16, 8 | Tick divider (section 1.1) |
| `WRAP_BOTTOM`, `WRAP_TOP` | 5, 5 | Program loop bounds (section 1.2). Reset: 0 and 31 |
| `IN_BASE` | 3 | First pin for `IN pins`, `MOV … pins`, `WAIT pin` |
| `OUT_BASE` | 3 | First pin for `OUT pins` / `OUT pindirs` |
| `SET_BASE`, `SET_COUNT` | 3, 3 | Pins for `SET pins` / `SET pindirs` |
| `SIDE_BASE`, `SIDE_CNT`, `SIDE_OPT`, `SIDE_PINDIR` | 3, 2, 1, 1 | Side-set (section 3) |
| `JMP_PIN` | 3 | Pin tested by `jmp pin` |
| `IN_DIR`, `OUT_DIR` | 1, 1 | Shift direction, 1 = right |
| `AUTOPUSH`, `PUSH_THRESH` | 1, 5 | Autopush; threshold 0 means 32 |
| `AUTOPULL`, `PULL_THRESH` | 1, 5 | Autopull; threshold 0 means 32 |

The host can also, per state machine: enable or disable it, restart it (clears `ISR`, `OSR`, the counts, the delay counter and the sticky flags; `X`, `Y` and `PC` keep their values), clear its FIFOs, restart its clock divider so two state machines tick in phase, write `PC` (a host write wins over the instruction executing on the same clock), write the TX FIFO, read the RX FIFO, and read the FIFO levels and the `ILLEGAL`, `RXOVER` and stalled flags. The host writes `PIN_OUT`, `PIN_DIR` and `PIN_OD` directly to set the initial pin state.

## 7. Assembly syntax

One instruction per line, PIO style, with optional `side <value>` and `[<delay>]` after the operands, `label:` definitions, `;` comments, and the directives `.program`, `.side_set <n> [opt] [pindirs]`, `.define`, `.config`, `.wrap_target`, `.wrap` and `.word`. `nop` assembles to `mov y, y`; `irq <n>` sets a flag and `irqclr <n>` clears it. The assembler and disassembler are in `tools/asm.py`.

Reference encodings, with no side-set and no delay:

| Assembly | Word |
|---|---|
| `jmp x-- 3` | `0x0043` |
| `wait 1 gpio 3` | `0x2083` |
| `wait 1 pin 0` | `0x20A0` |
| `in pins, 1` | `0x4001` |
| `out pins, 1` | `0x6001` |
| `push block` | `0x8020` |
| `pull block` | `0x80A0` |
| `mov y, y` | `0xA042` |
| `mov x, timer` | `0xA025` |
| `trig` | `0xC000` |
| `tstart` | `0xC020` |
| `set pins, 1` | `0xE001` |

## 8. How the protocols use it

The tested programs are in `firmware/`. Two of them are short enough to show here.

UART transmit, 8N1, eight ticks per bit, `.side_set 1 opt` on TX:

```
    pull block     side 1 [7]   ; stop bit, or idle while the FIFO is empty
    set x, 7       side 0 [7]   ; start bit
bitloop:
    out pins, 1                 ; data bit
    jmp x-- bitloop       [6]
```

SPI mode 0 master, and equally a JTAG shifter, with autopull and autopush, `.side_set 1` on the clock, four ticks per bit:

```
    out pins, 1    side 0 [1]   ; SPI: MOSI.  JTAG: `out pins, 2` drives TMS and TDI
    in pins, 1     side 1 [1]   ; SPI: MISO.  JTAG: TDO
```

| Need | Feature that covers it |
|---|---|
| I2C open-drain SDA and SCL | `PIN_OD`, so `out pins` and side-set use normal polarity |
| I2C clock stretching | `wait 1 gpio <scl>` with SCL released by side-set |
| I2C START, STOP and byte routines in one program | `out pc, 5`: the command word from the host selects the routine |
| SWD turnaround | `set pindirs, 0` / `set pindirs, 1` between phases, clock on side-set |
| JTAG TMS and TDI together | `out pins, 2`, host supplies both streams |
| UART receive start bit and stop-bit check | `wait 0 pin 0`, a delay to mid-bit, then `jmp pin` |
| Two protocols in 32 words | `.wrap` loops and autopush/autopull save the `jmp`, `push` and `pull` words |

## 9. Design decisions and rationale

The choices that go beyond a minimal UART/SPI/I2C PIO-style core, with the reason for each.

| # | Decision | Minimal baseline | Reason |
|---|---|---|---|
| 1 | `PUSH` and `PULL` share opcode `100` | ~8 instructions | Frees opcode `110` for row 10 |
| 2 | `JMP` adds `!y`, `y--`, `x!=y`, `!osre` | always, `X==0`, `X--`, pin high | Nested loops (bit and byte counters). `x!=y` costs a 32-bit comparator and is the first to cut if area gets tight |
| 3 | `OUT` adds `x`, `y`, `null`, `pindirs`, `pc`; `IN` adds `x`, `y`, `null`; `SET` adds `x`, `y` | pins only | Loading counters from the host, direction control from data, computed jumps for command dispatch |
| 4 | Autopush, autopull and program wrap | none | Each saves one or two of the 32 words per program |
| 5 | Per-pin open-drain mode `PIN_OD` | `SET pindirs` only | I2C programs get shorter and use normal polarity; costs 8 flip-flops |
| 6 | No `EXEC`, no `MOV` to pins, no `STATUS` | PIO has these | Left out to save area; the shared flags (row 11) stand in for PIO's IRQ system |
| 7 | 32-bit `X`, `Y`, `ISR`, `OSR`; FIFOs 4 deep | unspecified | One JTAG or SWD word per FIFO entry |
| 8 | Instruction memory is flip-flops, not SRAM | SRAM suggested | No macro to integrate, and the whole design still fits; `pe_imem` is the only block to swap if an SRAM is used later |
| 9 | `uo_out[4:1]` carry FIFO status flags | unused outputs tied to 0 | A host can wait on a pin instead of polling over SPI. `ui_in[7:3]` and `uo_out[7:6]` stay unused |
| 10 | `TRIG`, `TSTART`, the `timer` source and the trigger output on `uo_out[5]`, plus a host `TRIG_MATCH` register that fires the trigger when the timer reaches a set value | none | What a PIO does not have: a glitch trigger placed to the clock cycle after a protocol event, scheduled from the cycle timer, and cycle-exact response timing. About 4% more cell area. The `TRIG_MATCH` path is in `pe_core` and needs no ISA change |
| 11 | Four shared flags with `irq`/`irqclr`/`wait irq` | none | Lets one state machine signal the other, so detection and reaction can be split across them. A lighter stand-in for PIO's IRQ system; about 1% more cell area |
