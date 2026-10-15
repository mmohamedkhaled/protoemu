/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Fractional clock divider. Produces a one-cycle `tick` every
// div_int + div_frac/256 clock cycles on average (docs/isa.md, section 1.1).
module pe_clkdiv (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,        // the divider holds its state while low
    input  wire        restart,   // the next enabled cycle ticks
    input  wire [15:0] div_int,   // 0 means 65536
    input  wire [7:0]  div_frac,
    output wire        tick
);

  reg  [16:0] cnt;  // cycles left before the next tick
  reg  [7:0]  acc;  // fractional accumulator

  wire [8:0]  acc_sum   = {1'b0, acc} + {1'b0, div_frac};
  wire [15:0] period_m1 = div_int - 16'd1;

  assign tick = en && (cnt == 17'd0);

  always @(posedge clk) begin
    if (!rst_n || restart) begin
      cnt <= 17'd0;
      acc <= 8'd0;
    end else if (en) begin
      if (cnt == 17'd0) begin
        // A carry out of the accumulator stretches the next period by one cycle.
        cnt <= {1'b0, period_m1} + {16'd0, acc_sum[8]};
        acc <= acc_sum[7:0];
      end else begin
        cnt <= cnt - 17'd1;
      end
    end
  end

endmodule
