# SPDX-License-Identifier: Apache-2.0
"""Devices with exactly known timing, for the trigger and timer tests."""


class PulseTrain:
    """Drives a square wave and records the cycle of every rising edge it produced."""

    def __init__(self, pin, period, start_cycle):
        self.pin = pin
        self.period = period
        self.start_cycle = start_cycle
        self.drive = {pin: 0}
        self.rising = []

    def step(self, pads, cycle):
        level = 0
        if cycle >= self.start_cycle:
            level = int((cycle - self.start_cycle) % self.period < self.period // 2)
        if level and not self.drive[self.pin]:
            self.rising.append(cycle + 1)  # the pad shows it on the next cycle
        self.drive[self.pin] = level


class Responder:
    """Raises `response` exactly `delay` (1 or more) clock cycles after `stimulus` rises,
    and drops it when the stimulus drops: a target that takes a known time."""

    def __init__(self, stimulus, response, delay):
        self.stimulus, self.response = stimulus, response
        self.delay = delay
        self.drive = {response: 0}
        self._since = None

    def step(self, pads, cycle):
        if (pads >> self.stimulus) & 1:
            self._since = 0 if self._since is None else self._since + 1
            if self._since >= self.delay - 1:
                self.drive[self.response] = 1
        else:
            self._since = None
            self.drive[self.response] = 0


class SpiBusDriver:
    """A third-party SPI master on the bus, mode 0, most significant bit first.

    It bit-bangs `data_bytes` on SCK and a data pin at a chosen speed, so the
    sniffer and the pattern-triggered glitch program can watch a bus they do
    not themselves drive. It records the cycle of each rising SCK edge and the
    byte boundary each belongs to, so a test knows exactly when every bit and
    byte appeared on the wire."""

    def __init__(self, sck, data, data_bytes, half=4, start_cycle=0, gap=24):
        self.sck, self.data = sck, data
        self.half = half
        self.start_cycle = start_cycle
        self.gap = gap          # idle SCK cycles between bytes, so a watcher can re-sync
        self.drive = {sck: 0, data: 0}
        self.bit_rising = []    # (byte_index, bit_index, cycle), filled as edges occur
        self._levels, self._rising_at = self._build(data_bytes)
        self._prev_sck = 0

    def _build(self, data_bytes):
        levels = [(0, 0)] * self.start_cycle
        rising_at = {}
        for byte_index, byte in enumerate(data_bytes):
            for bit_index in range(8):
                bit = (byte >> (7 - bit_index)) & 1
                levels += [(0, bit)] * self.half
                rising_at[len(levels)] = (byte_index, bit_index)
                levels += [(1, bit)] * self.half
            levels += [(0, 0)] * self.gap
        return levels, rising_at

    def step(self, pads, cycle):
        sck, data = self._levels[cycle] if cycle < len(self._levels) else (0, 0)
        if sck and not self._prev_sck and cycle in self._rising_at:
            byte_index, bit_index = self._rising_at[cycle]
            self.bit_rising.append((byte_index, bit_index, cycle))
        self._prev_sck = sck
        self.drive[self.sck] = sck
        self.drive[self.data] = data

    @property
    def done_cycle(self):
        return len(self._levels)
