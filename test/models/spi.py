# SPDX-License-Identifier: Apache-2.0
"""SPI target reference model, mode 0, most significant bit first."""


class SpiTarget:
    """Shifts MOSI in on rising SCK edges and changes MISO on falling edges.
    Selected while `cs` is low; without a `cs` pin it is always selected."""

    def __init__(self, sck, mosi, miso, cs=None, reply=()):
        self.sck, self.mosi, self.miso, self.cs = sck, mosi, miso, cs
        self.drive = {miso: None}
        self.received = []          # bytes seen on MOSI
        self.reply = list(reply)    # bytes to send on MISO, in order
        self.sck_idle_violations = 0
        self._last_sck = 0
        self._selected = False
        self._in = 0
        self._count = 0
        self._out = 0xFF

    def _begin(self):
        self._in = 0
        self._count = 0
        self._out = self.reply.pop(0) if self.reply else 0xFF

    def step(self, pads, cycle):
        sck = (pads >> self.sck) & 1
        selected = self.cs is None or not (pads >> self.cs) & 1
        if selected and not self._selected:
            self._begin()
            if sck:
                self.sck_idle_violations += 1  # mode 0 idles low
        if selected:
            if sck and not self._last_sck:
                self._in = (self._in << 1) | ((pads >> self.mosi) & 1)
                self._count += 1
            elif not sck and self._last_sck:
                self._out = (self._out << 1) & 0xFF
                if self._count == 8:
                    self.received.append(self._in)
                    self._begin()
            self.drive[self.miso] = (self._out >> 7) & 1
        else:
            self.drive[self.miso] = None
        self._selected = selected
        self._last_sck = sck
