# SPDX-License-Identifier: Apache-2.0
"""I2C target reference model: a small memory device, like a 24Cxx EEPROM."""


class I2cMemory:
    """7-bit address, open-drain SDA and SCL.

    Write: [address+W] [pointer] [data...]     Read: [address+R] -> data from the pointer.
    With `stretch` > 0 it holds SCL low for that many clock cycles before every
    ACK slot, as a slow target would.
    """

    def __init__(self, sda, scl, address=0x50, stretch=0):
        self.sda, self.scl = sda, scl
        self.address = address
        self.stretch = stretch
        self.drive = {sda: None, scl: None}
        self.mem = {}
        self.pointer = 0
        self.starts = 0
        self.stops = 0
        self.stretched = 0       # how many times it stretched the clock
        self._last_sda = 1
        self._last_scl = 1
        self._phase = "idle"     # idle, address, pointer, write, read, ignore
        self._n = 0              # SCL rising edges seen in the current 9-bit frame
        self._shift = 0
        self._hold = 0

    def _pull(self, bit):
        """Open drain: pull low for a 0, release for a 1."""
        self.drive[self.sda] = 0 if bit == 0 else None

    def step(self, pads, cycle):
        sda, scl = (pads >> self.sda) & 1, (pads >> self.scl) & 1

        if self._hold:
            self._hold -= 1
            if not self._hold:
                self.drive[self.scl] = None

        if scl and self._last_scl and sda != self._last_sda:
            if not sda:                       # START (or repeated START)
                self.starts += 1
                self._phase, self._n, self._shift = "address", 0, 0
            else:                             # STOP
                self.stops += 1
                self._phase = "idle"
            self._pull(1)
        elif scl and not self._last_scl:
            self._rising(sda)
        elif not scl and self._last_scl:
            self._falling()

        self._last_sda, self._last_scl = sda, scl

    def _rising(self, sda):
        if self._phase in ("idle", "ignore"):
            return
        if self._n < 8:
            self._shift = ((self._shift << 1) | sda) & 0xFF
        elif self._phase == "read":
            self._acked = (sda == 0)          # the controller's ACK of our byte
        self._n += 1

    def _falling(self):
        phase = self._phase
        if phase in ("idle", "ignore"):
            return

        if phase == "read":
            if self._n < 8:
                self._pull((self._byte >> (7 - self._n)) & 1)
            elif self._n == 8:
                self._pull(1)                 # release for the controller's ACK
                self._stretch()
            else:
                self._n = 0
                if self._acked:
                    self._load_read_byte()
                else:
                    self._phase = "ignore"
                    self._pull(1)
            return

        if self._n == 8:                      # a byte has arrived: acknowledge it or not
            ack = True
            if phase == "address":
                ack = (self._shift >> 1) == self.address
                self._next = ("read" if self._shift & 1 else "pointer") if ack else "ignore"
            elif phase == "pointer":
                self.pointer = self._shift
                self._next = "write"
            else:
                self.mem[self.pointer] = self._shift
                self.pointer = (self.pointer + 1) & 0xFF
                self._next = "write"
            self._pull(0 if ack else 1)
            if ack:
                self._stretch()
        elif self._n == 9:                    # end of the ACK slot
            self._phase, self._n, self._shift = self._next, 0, 0
            self._pull(1)
            if self._phase == "read":
                self._load_read_byte()

    def _load_read_byte(self):
        self._byte = self.mem.get(self.pointer, 0xFF)
        self.pointer = (self.pointer + 1) & 0xFF
        self._pull((self._byte >> 7) & 1)

    def _stretch(self):
        if self.stretch:
            self.drive[self.scl] = 0
            self._hold = self.stretch
            self.stretched += 1
