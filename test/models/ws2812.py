# SPDX-License-Identifier: Apache-2.0
"""WS2812 LED reference model: decodes pixels from pulse widths."""


class Ws2812Receiver:
    """Measures every high and low time in clock cycles. A bit is 1 when its
    high time is longer than half the bit period; 24 bits make a pixel."""

    def __init__(self, pin, bit_cycles):
        self.pin = pin
        self.bit_cycles = bit_cycles
        self.drive = {}
        self.high_times = []
        self.periods = []
        self.bits = []
        self._last = 0
        self._rose = None

    @property
    def pixels(self):
        whole = len(self.bits) // 24 * 24
        return [int("".join(map(str, self.bits[i:i + 24])), 2) for i in range(0, whole, 24)]

    def step(self, pads, cycle):
        level = (pads >> self.pin) & 1
        if level and not self._last:
            if self._rose is not None:
                self.periods.append(cycle - self._rose)
            self._rose = cycle
        elif not level and self._last:
            high = cycle - self._rose
            self.high_times.append(high)
            self.bits.append(int(high > self.bit_cycles // 2))
        self._last = level
