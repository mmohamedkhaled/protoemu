# SPDX-License-Identifier: Apache-2.0
"""Serial Wire Debug target reference model (ARM ADIv5 SW-DP), read-only."""


def parity(value):
    return bin(value).count("1") & 1


class SwdTarget:
    """A debug port that answers reads of DPIDR (DP register 0).

    It follows the wire protocol bit by bit on rising SWCLK edges:
      - it stays silent until it has seen a line reset (50 or more clocks with
        SWDIO high) followed by a low bit;
      - a request is start(1), APnDP, RnW, A[2], A[3], parity, stop(0), park(1);
      - after one turnaround clock it drives ACK (OK = 1, 0, 0), then for a
        read 32 data bits, least significant first, and an even parity bit;
      - a malformed request makes it deaf until the next line reset.
    """

    def __init__(self, swclk, swdio, dpidr=0x2BA01477):
        self.swclk, self.swdio = swclk, swdio
        self.dpidr = dpidr
        self.drive = {swdio: None}
        self.line_resets = 0
        self.requests = []          # request bytes received, as sent on the wire
        self.protocol_errors = 0
        self.reads_answered = 0
        self._last_clk = 0
        self._ones = 0              # consecutive high bits from the host
        self._ready = False         # a line reset has been seen
        self._armed = False         # ... and a low bit after it
        self._request = None        # request bits collected so far
        self._reply = []            # bits still to drive

    def step(self, pads, cycle):
        clk = (pads >> self.swclk) & 1
        if clk and not self._last_clk:
            self._rising((pads >> self.swdio) & 1)
        self._last_clk = clk

    def _rising(self, bit):
        if self._reply:
            self._reply.pop(0)
            self.drive[self.swdio] = self._reply[0] if self._reply else None
            return

        self._ones = self._ones + 1 if bit else 0
        if self._ones == 50:
            self.line_resets += 1
            self._ready, self._armed, self._request = True, False, None
            return
        if not self._ready:
            return

        if self._request is None:
            if not bit:
                self._armed = True
            elif self._armed:
                self._request = [1]
            return

        self._request.append(bit)
        if len(self._request) < 8:
            return
        start, apndp, rnw, a2, a3, par, stop, park = self._request
        self.requests.append(sum(b << i for i, b in enumerate(self._request)))
        self._request = None
        if par != (apndp ^ rnw ^ a2 ^ a3) or stop != 0 or park != 1:
            self.protocol_errors += 1
            self._ready = False
            return
        if (apndp, rnw, a2, a3) != (0, 1, 0, 0):
            self._ready = False     # this model implements nothing but DPIDR
            return
        data = [(self.dpidr >> i) & 1 for i in range(32)]
        # The first and last entries are the turnaround clocks, when nothing is driven.
        self._reply = [None, 1, 0, 0] + data + [parity(self.dpidr), None]
        self.reads_answered += 1
