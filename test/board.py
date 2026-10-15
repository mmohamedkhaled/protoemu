# SPDX-License-Identifier: Apache-2.0
"""The board around the chip in the whole-chip tests.

`Board` resolves the eight bidirectional pads once per clock: the chip drives a
pad when its output enable is set, attached device models may drive it too, and
an undriven pad reads its pull-up. Device models see only the pads, never the
chip's internals, so the same tests run on the gate-level netlist.

A device model has a `drive` dictionary ({pin: 0, 1 or None}) and a
`step(pads, cycle)` method called once per clock with the resolved pad values.
"""

from cocotb.triggers import FallingEdge


class Board:
    def __init__(self, dut, pullups=0xFF):
        self.dut = dut
        self.pullups = pullups
        self.devices = []
        self.pads = pullups
        self.cycle = 0
        self.contention = []  # (cycle, pin): drivers disagreed
        self.uo_trace = None  # set to a dictionary to record {cycle: uo_out}

    def attach(self, device):
        self.devices.append(device)
        return device

    def detach(self, device):
        self.devices.remove(device)

    def pad(self, pin):
        return (self.pads >> pin) & 1

    async def run(self):
        dut = self.dut
        while True:
            await FallingEdge(dut.clk)
            out, oe = dut.uio_out.value, dut.uio_oe.value
            if not (out.is_resolvable and oe.is_resolvable):
                continue  # before reset
            out, oe = int(out), int(oe)
            pads = 0
            for pin in range(8):
                drivers = [(out >> pin) & 1] if (oe >> pin) & 1 else []
                drivers += [d.drive[pin] for d in self.devices if d.drive.get(pin) is not None]
                if not drivers:
                    level = (self.pullups >> pin) & 1
                else:
                    level = min(drivers)
                    if level != max(drivers):
                        self.contention.append((self.cycle, pin))
                pads |= level << pin
            self.pads = pads
            dut.uio_in.value = pads
            for device in self.devices:
                device.step(pads, self.cycle)
            if self.uo_trace is not None:
                self.uo_trace[self.cycle] = int(dut.uo_out.value)
            self.cycle += 1
