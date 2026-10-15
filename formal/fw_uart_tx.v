/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// firmware/uart_tx.pio on the real state machine: for every byte, and for any
// timing of the TX FIFO, the TX pin carries a correct 8N1 frame with every bit
// exactly 8 ticks long, and stays high between frames.
module fw_uart_tx (
    input wire        clk,
    input wire        tx_empty,  // the FIFO may be empty or not on any cycle
    input wire [31:0] tx_data    // and may hold any word
);

  localparam [2:0] TX = 3'd2;

  reg rst_n = 1'b0;
  always @(posedge clk) rst_n <= 1'b1;

  wire [4:0]  pc;
  wire [7:0]  out_mask, out_val, dir_mask, dir_val;
  wire        tx_pop, rx_push, trig_fire, timer_reset, stalled;
  wire [31:0] rx_data, osr;

  uart_tx_sm #(.OUT_BASE(TX), .SIDE_BASE(TX)) dut (
      .clk(clk), .rst_n(rst_n), .tick(1'b1), .pins_in(8'd0),
      .tx_data(tx_data), .timer(32'd0), .tx_empty(tx_empty), .rx_full(1'b0),
      .pc(pc), .out_mask(out_mask), .out_val(out_val), .dir_mask(dir_mask), .dir_val(dir_val),
      .tx_pop(tx_pop), .rx_push(rx_push), .trig_fire(trig_fire), .timer_reset(timer_reset),
      .stalled(stalled), .rx_data(rx_data), .f_osr(osr)
  );

  // The TX pin, idle high as the host leaves it.
  reg line = 1'b1;
  always @(posedge clk)
    if (rst_n && out_mask[TX]) line <= out_val[TX];

  // Reference: k counts ticks since the byte left the FIFO.
  reg       active = 1'b0;
  reg [7:0] k = 8'd0;
  reg [7:0] byte_q = 8'd0;
  always @(posedge clk) begin
    if (rst_n && tx_pop) begin
      active <= 1'b1;
      k      <= 8'd0;
      byte_q <= tx_data[7:0];
    end else if (active && k != 8'hFF) k <= k + 8'd1;
  end

  wire [7:0] data_k   = k - 8'd16;
  wire       expected = (k < 8'd8)  ? 1'b1 :                // stop bit of the previous frame, or idle
                        (k < 8'd16) ? 1'b0 :                // start bit
                        (k < 8'd80) ? byte_q[data_k[5:3]] : // data, least significant bit first
                                      1'b1;                 // stop bit, then idle
  always @(*) begin
    if (rst_n) begin
      assert (line == (active ? expected : 1'b1));
      // A new byte never cuts a frame short.
      if (tx_pop && active) assert (k >= 8'd79);
    end
  end

  always @(posedge clk) cover (active && k == 8'd90);

  wire _unused = &{pc, dir_mask, dir_val, rx_push, trig_fire, timer_reset, stalled, rx_data, osr,
                   1'b0};

endmodule
