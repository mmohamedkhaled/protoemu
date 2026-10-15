/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Clock/data discipline of the clocked-protocol programs, on the real state
// machine. For every tick pattern (so every clock divider), every word in the
// FIFOs and every level on the input pins:
//
//   SPI   MOSI changes only while SCK is low
//   JTAG  TMS and TDI change only while TCK is low
//   SWD   SWDIO's level and direction change only while SWCLK is low
//
// so the target, which samples on the rising clock edge, always sees a
// settled bit. Select the program with -DFW_SPI, -DFW_JTAG or -DFW_SWD.
module fw_clock_data (
    input wire        clk,
    input wire        tick,
    input wire [7:0]  pins_in,
    input wire [31:0] tx_data,
    input wire        tx_empty,
    input wire        rx_full
);

  reg rst_n = 1'b0;
  always @(posedge clk) rst_n <= 1'b1;

  wire [4:0]  pc;
  wire [7:0]  out_mask, out_val, dir_mask, dir_val;
  wire        tx_pop, rx_push, trig_fire, timer_reset, stalled;
  wire [31:0] rx_data, osr;

`ifdef FW_SPI
  localparam [2:0] CLOCK = 3'd0, DATA = 3'd1;
  localparam [7:0] WATCH = 8'b0000_0010;          // MOSI
  spi_master_sm #(.OUT_BASE(DATA), .IN_BASE(3'd2), .SIDE_BASE(CLOCK)) dut (
`elsif FW_JTAG
  localparam [2:0] CLOCK = 3'd3, DATA = 3'd0;
  localparam [7:0] WATCH = 8'b0000_0011;          // TMS and TDI
  jtag_sm #(.OUT_BASE(DATA), .IN_BASE(3'd2), .SIDE_BASE(CLOCK)) dut (
`else
  localparam [2:0] CLOCK = 3'd5, DATA = 3'd4;
  localparam [7:0] WATCH = 8'b0001_0000;          // SWDIO
  swd_sm #(.OUT_BASE(DATA), .IN_BASE(DATA), .SET_BASE(DATA), .SIDE_BASE(CLOCK)) dut (
`endif
      .clk(clk), .rst_n(rst_n), .tick(tick), .pins_in(pins_in),
      .tx_data(tx_data), .timer(32'd0), .tx_empty(tx_empty), .rx_full(rx_full),
      .pc(pc), .out_mask(out_mask), .out_val(out_val), .dir_mask(dir_mask), .dir_val(dir_val),
      .tx_pop(tx_pop), .rx_push(rx_push), .trig_fire(trig_fire), .timer_reset(timer_reset),
      .stalled(stalled), .rx_data(rx_data), .f_osr(osr)
  );

`ifdef FW_SWD
  // The host sends only `write` and `read` commands.
  always @(*)
    if (tx_pop && (pc == `FW_SWD_DISPATCH))
      assume ((tx_data[4:0] == `FW_SWD_WRITE) || (tx_data[4:0] == `FW_SWD_READ));

  // Helper for the induction: the word `out pc` is about to use is a command.
  always @(*)
    if (rst_n && (pc == `FW_SWD_DISPATCH + 5'd1))
      assert ((osr[4:0] == `FW_SWD_WRITE) || (osr[4:0] == `FW_SWD_READ));
`endif

  // The pins as the program leaves them; clock low and data driven at reset.
  reg [7:0] pin_out = 8'd0;
  reg [7:0] pin_dir = (8'd1 << CLOCK) | WATCH;
  always @(posedge clk) begin
    if (rst_n) begin
      pin_out <= (pin_out & ~out_mask) | (out_val & out_mask);
      pin_dir <= (pin_dir & ~dir_mask) | (dir_val & dir_mask);
    end
  end

  reg [7:0] prev_out = 8'd0;
  reg [7:0] prev_dir = 8'd0;
  reg       f_past = 1'b0;
  always @(posedge clk) begin
    prev_out <= pin_out;
    prev_dir <= pin_dir;
    f_past   <= 1'b1;
  end

  wire data_changed = ((pin_out ^ prev_out) | (pin_dir ^ prev_dir)) & WATCH ? 1'b1 : 1'b0;

  always @(*) begin
    if (f_past && rst_n) begin
      // Data changes only on an edge that leaves the clock low ...
      if (data_changed) assert (!pin_out[CLOCK]);
      // ... and the clock is always driven.
      assert (pin_dir[CLOCK]);
    end
  end

  always @(posedge clk) cover (rst_n && data_changed && !pin_out[CLOCK]);

  wire _unused = &{rx_push, trig_fire, timer_reset, stalled, rx_data, osr, 1'b0};

endmodule
