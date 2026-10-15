# SPDX-License-Identifier: Apache-2.0
"""What a host does to the chip: the register map and the sequences that load a
program, configure a state machine and move data through the FIFOs.

`Host` is written once against two small transports, so the same code drives
pe_core's register bus directly and the whole chip through its SPI port.
"""

from cocotb.triggers import RisingEdge, Timer

# ---------------------------------------------------------------- register map

SM_BASE = (0x00, 0x20)

CTRL, STATUS, FLEVEL, PC = 0x00, 0x01, 0x02, 0x03
CLKDIV_FRAC, CLKDIV_INTL, CLKDIV_INTH = 0x04, 0x05, 0x06
WRAP_BOTTOM, WRAP_TOP = 0x07, 0x08
PINCTRL0, PINCTRL1, PINCTRL2, EXECCTRL = 0x09, 0x0A, 0x0B, 0x0C
PUSH_THRESH, PULL_THRESH = 0x0D, 0x0E
TXF, RXF = 0x10, 0x14

PIN_OUT, PIN_DIR, PIN_OD, PIN_IN, ID = 0x18, 0x19, 0x1A, 0x1B, 0x1C
TRIG_DELAY, TRIG_WIDTH, TRIG_CTRL = 0x38, 0x3A, 0x3B
FLAGS = 0x3C   # the four shared inter-state-machine flags, in bits 3:0
TRIG_MATCH = 0x3D   # 16-bit: fire the trigger when the cycle timer reaches this
IMEM = 0x40

CTRL_EN, CTRL_RESTART, CTRL_CLKDIV_RESTART, CTRL_FIFO_CLEAR = 1, 2, 4, 8

ST_TX_FULL, ST_TX_EMPTY, ST_RX_FULL, ST_RX_EMPTY = 1, 2, 4, 8
ST_ILLEGAL, ST_RXOVER, ST_STALLED = 16, 32, 64

TRIG_INVERT, TRIG_FIRE, TRIG_MATCH_EN, TRIG_BUSY = 1, 2, 4, 0x80

ID_VALUE = 0x5E

CONFIG_DEFAULTS = dict(
    wrap_bottom=0, wrap_top=31, in_base=0, out_base=0, set_base=0, set_count=0, side_base=0,
    side_cnt=0, side_opt=0, side_pindir=0, jmp_pin=0, in_dir=0, out_dir=0, autopush=0,
    autopull=0, push_thresh=0, pull_thresh=0)


def clkdiv_for(clk_hz, tick_hz):
    """Divider (integer, fraction) for the tick rate closest to tick_hz."""
    div = round(clk_hz * 256 / tick_hz)
    assert 256 <= div < 65536 * 256, "tick rate out of range"
    return div >> 8, div & 0xFF


class Host:
    """Host-side driver. `transport` provides write(addr, data) and read(addr, n)."""

    def __init__(self, transport):
        self.t = transport

    async def write(self, addr, *data):
        await self.t.write(addr, bytes(data))

    async def read(self, addr, n=1):
        data = await self.t.read(addr, n)
        return data[0] if n == 1 else data

    async def load_program(self, words, origin=0):
        data = b"".join(w.to_bytes(2, "little") for w in words)
        await self.t.write(IMEM + 2 * origin, data)

    async def read_program(self, count, origin=0):
        """Read the instruction memory back. Valid only while state machine 0 is disabled."""
        data = await self.t.read(IMEM + 2 * origin, 2 * count)
        return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]

    async def configure(self, sm, clkdiv=(1, 0), **fields):
        """Write every configuration register of one state machine in one burst."""
        unknown = set(fields) - set(CONFIG_DEFAULTS)
        assert not unknown, f"unknown configuration fields: {unknown}"
        c = dict(CONFIG_DEFAULTS, **fields)
        div_int, div_frac = clkdiv
        await self.t.write(SM_BASE[sm] + CLKDIV_FRAC, bytes([
            div_frac, div_int & 0xFF, div_int >> 8,
            c["wrap_bottom"], c["wrap_top"],
            c["in_base"] | (c["out_base"] << 3),
            c["set_base"] | (c["set_count"] << 3),
            c["side_base"] | (c["jmp_pin"] << 3),
            c["side_cnt"] | (c["side_opt"] << 2) | (c["side_pindir"] << 3) | (c["in_dir"] << 4)
            | (c["out_dir"] << 5) | (c["autopush"] << 6) | (c["autopull"] << 7),
            c["push_thresh"], c["pull_thresh"]]))

    async def set_pins(self, out=0, dirs=0, od=0):
        await self.t.write(PIN_OUT, bytes([out, dirs, od]))

    async def set_trigger(self, delay=0, width=0, invert=0, match=None):
        """The trigger pulse starts `delay` clocks after it fires and lasts `width + 1` clocks.

        With `match` set, the trigger also fires on its own when the cycle timer
        reaches that value (a glitch scheduled a fixed number of cycles after
        `tstart`); the control register's match-enable bit is set too.
        """
        ctrl = invert | (TRIG_MATCH_EN if match is not None else 0)
        await self.t.write(TRIG_DELAY, bytes([delay & 0xFF, delay >> 8, width, ctrl]))
        if match is not None:
            await self.t.write(TRIG_MATCH, bytes([match & 0xFF, match >> 8]))

    async def prepare(self, sm, pc=0):
        """Stop the state machine, clear it and its FIFOs, and point it at `pc`."""
        base = SM_BASE[sm]
        await self.write(base + CTRL, CTRL_RESTART | CTRL_CLKDIV_RESTART | CTRL_FIFO_CLEAR)
        await self.write(base + PC, pc)

    async def enable(self, sm):
        await self.write(SM_BASE[sm] + CTRL, CTRL_EN)

    async def start(self, sm, pc=0):
        """Clear the state machine, point it at `pc` and let it run."""
        await self.prepare(sm, pc)
        await self.enable(sm)

    async def stop(self, sm):
        await self.write(SM_BASE[sm] + CTRL, 0)

    async def status(self, sm):
        return await self.read(SM_BASE[sm] + STATUS)

    async def tx(self, sm, word):
        await self.t.write(SM_BASE[sm] + TXF, word.to_bytes(4, "little"))

    async def rx(self, sm):
        return int.from_bytes(await self.t.read(SM_BASE[sm] + RXF, 4), "little")

    async def rx_wait(self, sm, timeout_polls=2000):
        """Poll until the RX FIFO has a word, then read it."""
        for _ in range(timeout_polls):
            if not await self.status(sm) & ST_RX_EMPTY:
                return await self.rx(sm)
        raise TimeoutError(f"state machine {sm}: no RX data")

    async def tx_wait(self, sm, word, timeout_polls=2000):
        """Poll until the TX FIFO has room, then write a word."""
        for _ in range(timeout_polls):
            if not await self.status(sm) & ST_TX_FULL:
                return await self.tx(sm, word)
        raise TimeoutError(f"state machine {sm}: TX FIFO stayed full")


class BusTransport:
    """Drives the register bus of pe_core directly, one byte per clock cycle."""

    def __init__(self, dut):
        self.dut = dut
        dut.bus_we.value = 0
        dut.bus_re.value = 0
        dut.bus_addr.value = 0
        dut.bus_wdata.value = 0

    async def write(self, addr, data):
        dut = self.dut
        for i, byte in enumerate(data):
            dut.bus_addr.value = (addr + i) & 0x7F
            dut.bus_wdata.value = byte
            dut.bus_we.value = 1
            await RisingEdge(dut.clk)
        dut.bus_we.value = 0

    async def read(self, addr, n):
        dut = self.dut
        out = bytearray()
        for i in range(n):
            dut.bus_addr.value = (addr + i) & 0x7F
            dut.bus_re.value = 1
            await Timer(1, unit="ns")
            out.append(int(dut.bus_rdata.value))
            await RisingEdge(dut.clk)
        dut.bus_re.value = 0
        return bytes(out)


class SpiTransport:
    """SPI mode 0 master. `set_pins(sck, csn, mosi)` and `get_miso()` reach the pins."""

    def __init__(self, clk, set_pins, get_miso, half_period=4):
        self.clk = clk
        self.set_pins = set_pins
        self.get_miso = get_miso
        self.half = half_period  # clock cycles per half SCK period
        set_pins(0, 1, 0)

    async def _wait(self, cycles=None):
        for _ in range(cycles or self.half):
            await RisingEdge(self.clk)

    async def transfer(self, mosi_bytes, read_count=0):
        """One frame: send mosi_bytes, then clock in read_count bytes."""
        self.set_pins(0, 0, 0)
        await self._wait()
        received = bytearray()
        for index in range(len(mosi_bytes) + read_count):
            out = mosi_bytes[index] if index < len(mosi_bytes) else 0
            value = 0
            for bit in range(7, -1, -1):
                mosi = (out >> bit) & 1
                self.set_pins(0, 0, mosi)
                await self._wait()
                value = (value << 1) | self.get_miso()  # sampled on the rising edge
                self.set_pins(1, 0, mosi)
                await self._wait()
            if index >= len(mosi_bytes):
                received.append(value)
        self.set_pins(0, 0, 0)
        await self._wait()
        self.set_pins(0, 1, 0)
        await self._wait(2 * self.half)
        return bytes(received)

    async def write(self, addr, data):
        await self.transfer(bytes([0x80 | addr]) + bytes(data))

    async def read(self, addr, n):
        return await self.transfer(bytes([addr]), n)


def chip_spi(dut, half_period=4):
    """SPI transport on the whole chip's pins: ui_in[0]=SCK, [1]=CSn, [2]=MOSI, uo_out[0]=MISO."""

    def set_pins(sck, csn, mosi):
        dut.ui_in.value = sck | (csn << 1) | (mosi << 2)

    return SpiTransport(dut.clk, set_pins, lambda: int(dut.uo_out.value) & 1, half_period)
