/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Instruction decoder (docs/isa.md, sections 2 to 4). Purely combinational.
//
// Exactly one of the op_* outputs or `illegal` is high. An illegal instruction
// keeps its delay and side-set and does nothing else.
module pe_decoder (
    input  wire [15:0] instr,
    input  wire [1:0]  side_cnt,   // number of side-set data bits, 0 to 3
    input  wire        side_opt,   // bit 12 is a per-instruction side-set enable

    output wire        op_jmp,
    output wire        op_wait,
    output wire        op_in,
    output wire        op_out,
    output wire        op_push,
    output wire        op_pull,
    output wire        op_mov,
    output wire        op_set,
    output wire        op_trig,    // fire the trigger pulse generator
    output wire        op_tstart,  // restart the cycle timer
    output wire        op_irqset,  // set a shared flag
    output wire        op_irqclr,  // clear a shared flag
    output wire [1:0]  irq_index,  // which flag, 0 to 3
    output wire        illegal,

    output wire [2:0]  sel,        // JMP cond; IN src; OUT, MOV, SET dst
    output wire [4:0]  arg,        // JMP addr; SET data
    output wire [5:0]  count,      // IN, OUT bit count, 1 to 32
    output wire        wait_pol,
    output wire        wait_rel,   // 1 = pin index is relative to IN_BASE
    output wire        wait_flag,  // WAIT tests a shared flag, not a pin
    output wire [2:0]  wait_idx,
    output wire        pp_if,      // PUSH iffull, PULL ifempty
    output wire        pp_block,
    output wire [1:0]  mov_op,
    output wire [2:0]  mov_src,

    output wire [4:0]  delay,
    output wire        side_en,
    output wire [2:0]  side_val
);

  wire [2:0] opcode = instr[15:13];
  wire [4:0] field  = instr[12:8];
  wire [2:0] hi     = instr[7:5];
  wire [4:0] lo     = instr[4:0];

  wire is_jmp  = (opcode == 3'b000);
  wire is_wait = (opcode == 3'b001);
  wire is_in   = (opcode == 3'b010);
  wire is_out  = (opcode == 3'b011);
  wire is_pp   = (opcode == 3'b100);
  wire is_mov  = (opcode == 3'b101);
  wire is_trig = (opcode == 3'b110);
  wire is_set  = (opcode == 3'b111);

  wire [1:0] wait_src = instr[6:5];  // 00 gpio, 01 pin (relative), 10 flag, 11 reserved

  // Reserved encodings, per opcode.
  wire bad_wait = (wait_src == 2'b11) || (lo[4:3] != 2'b00) || (wait_src == 2'b10 && lo[2]);
  wire bad_in   = hi[2];
  wire bad_out  = (hi == 3'b110) || (hi == 3'b111);
  wire bad_pp   = (lo != 5'd0);
  wire bad_mov  = (hi == 3'b000) || (hi == 3'b011) || (hi == 3'b100) ||
                  (lo[2:0] == 3'b100) || (lo[4:3] == 2'b11);
  // Opcode 110: trig/tstart (hi 00x, lo = 0) and irq set/clr (hi 01x, flag in lo[1:0]).
  wire bad_trig = hi[2] || (hi[2:1] == 2'b00 && lo != 5'd0) ||
                  (hi[2:1] == 2'b01 && lo[4:2] != 3'd0);
  wire bad_set  = (hi == 3'b011) || (hi[2] && (hi[1:0] != 2'b00));

  assign op_jmp  = is_jmp;
  assign op_wait = is_wait && !bad_wait;
  assign op_in   = is_in && !bad_in;
  assign op_out  = is_out && !bad_out;
  assign op_push = is_pp && !bad_pp && !instr[7];
  assign op_pull = is_pp && !bad_pp && instr[7];
  assign op_mov  = is_mov && !bad_mov;
  assign op_set  = is_set && !bad_set;

  assign op_trig   = is_trig && !bad_trig && (hi == 3'b000);
  assign op_tstart = is_trig && !bad_trig && (hi == 3'b001);
  assign op_irqset = is_trig && !bad_trig && (hi == 3'b010);
  assign op_irqclr = is_trig && !bad_trig && (hi == 3'b011);
  assign irq_index = lo[1:0];

  assign illegal = !(op_jmp || op_wait || op_in || op_out || op_push || op_pull || op_mov ||
                     op_set || op_trig || op_tstart || op_irqset || op_irqclr);

  assign sel      = hi;
  assign arg      = lo;
  assign count    = {lo == 5'd0, lo};
  assign wait_pol = instr[7];
  assign wait_rel = instr[5];
  assign wait_flag = (wait_src == 2'b10);
  assign wait_idx = lo[2:0];
  assign pp_if    = instr[6];
  assign pp_block = instr[5];
  assign mov_op   = lo[4:3];
  assign mov_src  = lo[2:0];

  // Delay / side-set field. From bit 12 down: the optional enable bit, then
  // side_cnt data bits, then the delay in whatever is left.
  wire [2:0] side_top = side_opt ? field[3:1] : field[4:2];  // the three bits side data can use
  wire [2:0] side_raw = (side_cnt == 2'd1) ? {2'b00, side_top[2]}   :
                        (side_cnt == 2'd2) ? {1'b0,  side_top[2:1]} :
                        (side_cnt == 2'd3) ? side_top               : 3'b000;
  wire [2:0] used     = {1'b0, side_cnt} + {2'b00, side_opt};    // 0 to 4 bits not delay

  assign side_en  = (side_cnt != 2'd0) && (!side_opt || field[4]);
  assign side_val = side_raw;
  assign delay    = field & (5'b11111 >> used);

endmodule
