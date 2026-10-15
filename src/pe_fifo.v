/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Small synchronous FIFO. `dout` is the oldest entry and is valid whenever
// `empty` is low. A push while full and a pop while empty are ignored.
module pe_fifo #(
    parameter WIDTH = 32,
    parameter AW    = 2            // depth is 2**AW
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             clear,
    input  wire             push,
    input  wire [WIDTH-1:0] din,
    input  wire             pop,
    output wire [WIDTH-1:0] dout,
    output wire             full,
    output wire             empty,
    output wire [AW:0]      level
);

  // The storage has no reset: an entry is only read after it has been written.
  reg [WIDTH-1:0] mem [0:(1<<AW)-1];
  reg [AW:0]      wptr;
  reg [AW:0]      rptr;

  assign level = wptr - rptr;
  assign empty = (wptr == rptr);
  assign full  = (level == {1'b1, {AW{1'b0}}});
  assign dout  = mem[rptr[AW-1:0]];

  wire do_push = push && !full;
  wire do_pop  = pop && !empty;

  always @(posedge clk) begin
    if (!rst_n || clear) begin
      wptr <= {(AW + 1) {1'b0}};
      rptr <= {(AW + 1) {1'b0}};
    end else begin
      if (do_push) wptr <= wptr + 1'b1;
      if (do_pop)  rptr <= rptr + 1'b1;
    end
  end

  always @(posedge clk) begin
    if (do_push) mem[wptr[AW-1:0]] <= din;
  end

endmodule
