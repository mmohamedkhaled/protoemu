# SPDX-License-Identifier: Apache-2.0
"""UART reference models (8N1), working in clock cycles."""


class UartReceiver:
    """Decodes a UART line the way a real receiver does: it finds the start
    edge, then samples the middle of every bit. It also records every edge on
    the line so a test can check the bit timing exactly."""

    def __init__(self, pin, bit_cycles):
        self.pin = pin
        self.bit_cycles = bit_cycles
        self.drive = {}
        self.bytes = []
        self.framing_errors = 0
        self.edges = []        # cycle numbers at which the line changed
        self._last = 1
        self._start = None     # cycle of the start edge of the frame in progress

    def step(self, pads, cycle):
        level = (pads >> self.pin) & 1
        if level != self._last:
            self.edges.append(cycle)
        if self._start is None:
            if self._last == 1 and level == 0:
                self._start = cycle
                self._bits = []
        else:
            since = cycle - self._start
            index, phase = divmod(since, self.bit_cycles)
            if phase == self.bit_cycles // 2:
                if index == 0 and level != 0:
                    self._start = None  # a glitch, not a start bit
                elif 1 <= index <= 8:
                    self._bits.append(level)
                elif index == 9:
                    if level == 1:
                        self.bytes.append(sum(b << i for i, b in enumerate(self._bits)))
                    else:
                        self.framing_errors += 1
                    self._start = None
        self._last = level


class UartTransmitter:
    """Drives a UART line with queued bytes. `stop_bit=0` sends a framing error."""

    def __init__(self, pin, bit_cycles):
        self.pin = pin
        self.bit_cycles = bit_cycles
        self.drive = {pin: 1}
        self._levels = []  # one entry per clock cycle still to drive

    def send(self, byte, stop_bit=1, idle_bits=1):
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [stop_bit] + [1] * idle_bits
        for bit in bits:
            self._levels += [bit] * self.bit_cycles

    @property
    def busy(self):
        return bool(self._levels)

    def step(self, pads, cycle):
        self.drive[self.pin] = self._levels.pop(0) if self._levels else 1
