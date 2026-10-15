/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Input and output shift registers of one state machine, with their bit
// counters (docs/isa.md, sections 4.3 and 4.4).
//
// Each register uses a single 32-bit rotator for both shift directions:
// a shift by n is a rotate by n with the n vacated bits masked or replaced.
module pe_shift (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        clear,           // ISR, OSR and isr_count to 0, osr_count to 32

    // Input shift register
    input  wire        in_dir,          // 1 = shift right
    input  wire        in_shift,        // shift in the low in_n bits of in_data
    input  wire [5:0]  in_n,            // 1 to 32
    input  wire [31:0] in_data,
    input  wire        isr_load,        // ISR <- isr_load_data, isr_count <- 0
    input  wire [31:0] isr_load_data,
    input  wire        isr_clear,       // ISR <- 0, isr_count <- 0; wins over load and shift
    output reg  [31:0] isr,
    output reg  [5:0]  isr_count,
    output wire [31:0] isr_shifted,     // ISR as it would be after the shift
    output wire [5:0]  isr_count_next,  // isr_count as it would be after the shift

    // Output shift register
    input  wire        out_dir,         // 1 = shift right
    input  wire        out_shift,       // shift out out_n bits
    input  wire [5:0]  out_n,           // 1 to 32
    input  wire        osr_load,        // OSR <- osr_load_data, osr_count <- 0; wins over shift
    input  wire [31:0] osr_load_data,
    output reg  [31:0] osr,
    output reg  [5:0]  osr_count,
    output wire [31:0] out_data,        // the out_n bits being shifted out, right-justified
    output wire [5:0]  osr_count_next   // osr_count as it would be after the shift
);

  function [31:0] rotl(input [31:0] v, input [4:0] k);
    rotl = (v << k) | (v >> (6'd32 - {1'b0, k}));
  endfunction

  // Low n bits set, n = 1 to 32.
  function [31:0] low_mask(input [5:0] n);
    low_mask = n[5] ? 32'hFFFF_FFFF : ((32'd1 << n[4:0]) - 32'd1);
  endfunction

  function [31:0] reverse(input [31:0] v);
    integer i;
    for (i = 0; i < 32; i = i + 1) reverse[i] = v[31-i];
  endfunction

  function [5:0] sat_add(input [5:0] count, input [5:0] n);
    reg [6:0] sum;
    begin
      sum     = {1'b0, count} + {1'b0, n};
      sat_add = (sum > 7'd32) ? 6'd32 : sum[5:0];
    end
  endfunction

  // ---------------------------------------------------------------- ISR

  wire [31:0] in_mask = low_mask(in_n);

  // Left:  rotate left by n, then put the new bits in the low n bits.
  // Right: put the new bits in the low n bits, then rotate right by n.
  wire [31:0] isr_merged  = (isr & ~in_mask) | (in_data & in_mask);
  wire [31:0] isr_rot_in  = in_dir ? isr_merged : isr;
  wire [4:0]  isr_rot_amt = in_dir ? (5'd0 - in_n[4:0]) : in_n[4:0];
  wire [31:0] isr_rot     = rotl(isr_rot_in, isr_rot_amt);

  assign isr_shifted    = in_dir ? isr_rot : ((isr_rot & ~in_mask) | (in_data & in_mask));
  assign isr_count_next = sat_add(isr_count, in_n);

  always @(posedge clk) begin
    if (!rst_n || clear || isr_clear) begin
      isr       <= 32'd0;
      isr_count <= 6'd0;
    end else if (isr_load) begin
      isr       <= isr_load_data;
      isr_count <= 6'd0;
    end else if (in_shift) begin
      isr       <= isr_shifted;
      isr_count <= isr_count_next;
    end
  end

  // ---------------------------------------------------------------- OSR

  wire [31:0] out_mask = low_mask(out_n);

  // Right: the bits leave from the bottom; rotate right by n and clear the top n.
  // Left:  rotate left by n; the bits that left are now the low n bits.
  wire [4:0]  osr_rot_amt = out_dir ? (5'd0 - out_n[4:0]) : out_n[4:0];
  wire [31:0] osr_rot     = rotl(osr, osr_rot_amt);

  assign out_data       = (out_dir ? osr : osr_rot) & out_mask;
  assign osr_count_next = sat_add(osr_count, out_n);

  always @(posedge clk) begin
    if (!rst_n || clear) begin
      osr       <= 32'd0;
      osr_count <= 6'd32;
    end else if (osr_load) begin
      osr       <= osr_load_data;
      osr_count <= 6'd0;
    end else if (out_shift) begin
      osr       <= osr_rot & ~(out_dir ? reverse(out_mask) : out_mask);
      osr_count <= osr_count_next;
    end
  end

endmodule
