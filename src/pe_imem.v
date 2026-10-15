/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Instruction memory: 32 words of 16 bits, one byte-wide write port for the
// host and one combinational read port per state machine.
//
// Built from flip-flops so the design does not depend on an SRAM macro. It is
// the only block that would change if one is used instead.
module pe_imem (
    input  wire        clk,
    input  wire        we,       // write one byte
    input  wire [4:0]  waddr,
    input  wire        wsel,     // 0 = low byte, 1 = high byte
    input  wire [7:0]  wdata,
    input  wire [4:0]  raddr0,
    output wire [15:0] rdata0,
    input  wire [4:0]  raddr1,
    output wire [15:0] rdata1
);

  // No reset: the host loads a program before it enables a state machine.
  reg [7:0] lo [0:31];
  reg [7:0] hi [0:31];

  always @(posedge clk) begin
    if (we && !wsel) lo[waddr] <= wdata;
    if (we && wsel) hi[waddr] <= wdata;
  end

  assign rdata0 = {hi[raddr0], lo[raddr0]};
  assign rdata1 = {hi[raddr1], lo[raddr1]};

endmodule
