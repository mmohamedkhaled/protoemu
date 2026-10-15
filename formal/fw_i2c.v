/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// firmware/i2c_master.pio on the real state machine, against any target: the
// target may hold SDA and SCL low on any cycle (clock stretching included) and
// the host may send any sequence of commands.
//
//   - The master changes SDA while SCL is high only inside the `start` and
//     `stop` routines. So a byte transfer can never be mistaken for a START or
//     a STOP, whatever the data and whatever the target does.
//   - The master only ever releases the lines or pulls them low.
module fw_i2c (
    input wire        clk,
    input wire        tick,
    input wire        target_sda,  // what the target does to each line: 0 pulls it low
    input wire        target_scl,
    input wire [31:0] tx_data,
    input wire        tx_empty,
    input wire        rx_full
);

  localparam [2:0] SDA = 3'd6, SCL = 3'd7;

  reg rst_n = 1'b0;
  always @(posedge clk) rst_n <= 1'b1;

  // Open-drain pins, both outputs, both released at reset (as the host sets them).
  reg [7:0] pin_out = (8'd1 << SDA) | (8'd1 << SCL);
  reg [7:0] pin_dir = (8'd1 << SDA) | (8'd1 << SCL);

  wire master_sda = !(pin_dir[SDA] && !pin_out[SDA]);  // 0 = pulling low
  wire master_scl = !(pin_dir[SCL] && !pin_out[SCL]);
  wire sda_line   = master_sda && target_sda;
  wire scl_line   = master_scl && target_scl;

  wire [4:0]  pc;
  wire [7:0]  out_mask, out_val, dir_mask, dir_val;
  wire        tx_pop, rx_push, trig_fire, timer_reset, stalled;
  wire [31:0] rx_data, osr;

  i2c_master_sm #(.SET_BASE(SDA), .OUT_BASE(SDA), .IN_BASE(SDA), .SIDE_BASE(SCL)) dut (
      .clk(clk), .rst_n(rst_n), .tick(tick),
      .pins_in({scl_line, sda_line, 6'd0}),
      .tx_data(tx_data), .timer(32'd0), .tx_empty(tx_empty), .rx_full(rx_full),
      .pc(pc), .out_mask(out_mask), .out_val(out_val), .dir_mask(dir_mask), .dir_val(dir_val),
      .tx_pop(tx_pop), .rx_push(rx_push), .trig_fire(trig_fire), .timer_reset(timer_reset),
      .stalled(stalled), .rx_data(rx_data), .f_osr(osr)
  );

  // The host sends only the three commands.
  wire [4:0] routine = tx_data[31:27];
  always @(*)
    if (tx_pop && (pc == `FW_I2C_MASTER_DISPATCH))
      assume ((routine == `FW_I2C_MASTER_START) || (routine == `FW_I2C_MASTER_STOP) ||
              (routine == `FW_I2C_MASTER_BYTE));

  // Helper for the induction: the word `out pc` is about to use is a command.
  always @(*)
    if (rst_n && (pc == `FW_I2C_MASTER_DISPATCH + 5'd1))
      assert ((osr[31:27] == `FW_I2C_MASTER_START) || (osr[31:27] == `FW_I2C_MASTER_STOP) ||
              (osr[31:27] == `FW_I2C_MASTER_BYTE));

  always @(posedge clk) begin
    if (rst_n) begin
      pin_out <= (pin_out & ~out_mask) | (out_val & out_mask);
      pin_dir <= (pin_dir & ~dir_mask) | (dir_val & dir_mask);
    end
  end

  reg       f_past = 1'b0;
  reg       prev_sda = 1'b1;
  reg       prev_scl_line = 1'b1;
  reg [4:0] prev_pc = 5'd0;
  always @(posedge clk) begin
    f_past        <= 1'b1;
    prev_sda      <= master_sda;
    prev_scl_line <= scl_line;
    prev_pc       <= pc;
  end

  wire in_start_or_stop = (prev_pc >= `FW_I2C_MASTER_START) && (prev_pc < `FW_I2C_MASTER_BYTE);

  always @(*) begin
    if (f_past && rst_n) begin
      if ((master_sda != prev_sda) && prev_scl_line && scl_line) assert (in_start_or_stop);
      // The master never drives a line high: the pins stay outputs and only their level changes.
      assert (pin_dir[SDA] && pin_dir[SCL]);
    end
  end

  always @(posedge clk) begin
    cover (f_past && rst_n && (master_sda != prev_sda) && prev_scl_line && scl_line);
    cover (f_past && rst_n && rx_push);
  end

  wire _unused = &{trig_fire, timer_reset, stalled, rx_data, 1'b0};

endmodule
