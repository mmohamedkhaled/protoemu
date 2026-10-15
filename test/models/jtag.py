# SPDX-License-Identifier: Apache-2.0
"""JTAG TAP reference model (IEEE 1149.1) with IDCODE and BYPASS."""

# state: (next state when TMS = 0, next state when TMS = 1)
TAP_STATES = {
    "test_logic_reset": ("run_test_idle", "test_logic_reset"),
    "run_test_idle": ("run_test_idle", "select_dr"),
    "select_dr": ("capture_dr", "select_ir"),
    "capture_dr": ("shift_dr", "exit1_dr"),
    "shift_dr": ("shift_dr", "exit1_dr"),
    "exit1_dr": ("pause_dr", "update_dr"),
    "pause_dr": ("pause_dr", "exit2_dr"),
    "exit2_dr": ("shift_dr", "update_dr"),
    "update_dr": ("run_test_idle", "select_dr"),
    "select_ir": ("capture_ir", "test_logic_reset"),
    "capture_ir": ("shift_ir", "exit1_ir"),
    "shift_ir": ("shift_ir", "exit1_ir"),
    "exit1_ir": ("pause_ir", "update_ir"),
    "pause_ir": ("pause_ir", "exit2_ir"),
    "exit2_ir": ("shift_ir", "update_ir"),
    "update_ir": ("run_test_idle", "select_dr"),
}

IR_LEN = 4
IR_IDCODE = 0b1110
IR_BYPASS = 0b1111


class JtagTap:
    """Samples TMS and TDI on rising TCK, changes TDO on falling TCK. TDO is
    driven only in the two shift states, as the standard requires."""

    def __init__(self, tck, tms, tdi, tdo, idcode=0x4BA00477):
        self.tck, self.tms, self.tdi, self.tdo = tck, tms, tdi, tdo
        self.idcode = idcode
        self.drive = {tdo: None}
        self.state = "test_logic_reset"
        self.ir = IR_IDCODE
        self.visited = [self.state]
        self.tck_cycles = 0
        self._last_tck = 0
        self._shift = 0
        self._length = 1

    def step(self, pads, cycle):
        tck = (pads >> self.tck) & 1
        if tck and not self._last_tck:
            self._rising((pads >> self.tms) & 1, (pads >> self.tdi) & 1)
        elif not tck and self._last_tck:
            shifting = self.state in ("shift_dr", "shift_ir")
            self.drive[self.tdo] = (self._shift & 1) if shifting else None
        self._last_tck = tck

    def _rising(self, tms, tdi):
        self.tck_cycles += 1
        state = self.state
        if state == "capture_dr":
            if self.ir == IR_IDCODE:
                self._shift, self._length = self.idcode, 32
            else:
                self._shift, self._length = 0, 1
        elif state == "capture_ir":
            self._shift, self._length = 0b0001, IR_LEN
        elif state in ("shift_dr", "shift_ir"):
            self._shift = (self._shift >> 1) | (tdi << (self._length - 1))
        elif state == "update_ir":
            self.ir = self._shift
        self.state = TAP_STATES[state][tms]
        if self.state == "test_logic_reset":
            self.ir = IR_IDCODE
        if self.state != self.visited[-1]:
            self.visited.append(self.state)
