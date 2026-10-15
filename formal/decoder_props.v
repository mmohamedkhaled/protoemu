/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Formal properties of pe_decoder, proved for every instruction word and
// every side-set configuration (formal/decoder.sby).
module decoder_props (
    input wire [15:0] instr,
    input wire [1:0]  side_cnt,
    input wire        side_opt,
    // A second, unrelated word and configuration for the independence proofs.
    input wire [15:0] other,
    input wire [1:0]  other_cnt,
    input wire        other_opt
);

  wire       op_jmp, op_wait, op_in, op_out, op_push, op_pull, op_mov, op_set, illegal;
  wire       op_trig, op_tstart, op_irqset, op_irqclr;
  wire [1:0] irq_index;
  wire [2:0] sel;
  wire [4:0] arg;
  wire [5:0] count;
  wire       wait_pol, wait_rel, wait_flag;
  wire [2:0] wait_idx;
  wire       pp_if, pp_block;
  wire [1:0] mov_op;
  wire [2:0] mov_src;
  wire [4:0] delay;
  wire       side_en;
  wire [2:0] side_val;

  pe_decoder dut (
      .instr(instr), .side_cnt(side_cnt), .side_opt(side_opt),
      .op_jmp(op_jmp), .op_wait(op_wait), .op_in(op_in), .op_out(op_out), .op_push(op_push),
      .op_pull(op_pull), .op_mov(op_mov), .op_set(op_set), .op_trig(op_trig),
      .op_tstart(op_tstart), .op_irqset(op_irqset), .op_irqclr(op_irqclr),
      .irq_index(irq_index), .illegal(illegal),
      .sel(sel), .arg(arg), .count(count), .wait_pol(wait_pol), .wait_rel(wait_rel),
      .wait_flag(wait_flag), .wait_idx(wait_idx), .pp_if(pp_if), .pp_block(pp_block),
      .mov_op(mov_op), .mov_src(mov_src), .delay(delay), .side_en(side_en), .side_val(side_val)
  );

  // ---------------------------------------------------------------- legality

  // The legal encodings, written out from the tables in docs/isa.md section 4.
  wire [2:0] opcode = instr[15:13];
  wire [2:0] hi     = instr[7:5];
  wire [4:0] lo     = instr[4:0];

  reg legal;
  always @(*) begin
    case (opcode)
      3'b000:  legal = 1'b1;                                             // JMP
      3'b001:  legal = (instr[6:5] != 2'b11) && (lo[4:3] == 2'b00) &&    // WAIT
                       !(instr[6:5] == 2'b10 && lo[2]);                  //   flag idx 0..3
      3'b010:  legal = (hi <= 3'b011);                                   // IN
      3'b011:  legal = (hi <= 3'b101);                                   // OUT
      3'b100:  legal = (lo == 5'd0);                                     // PUSH, PULL
      3'b101:  legal = (hi == 3'b001 || hi == 3'b010 || hi == 3'b101 ||  // MOV
                        hi == 3'b110 || hi == 3'b111) &&
                       (lo[2:0] != 3'b100) && (lo[4:3] != 2'b11);
      // TRIG/TSTART (hi 00x, lo = 0) and IRQ set/clear (hi 01x, flag in lo[1:0]).
      3'b110:  legal = (hi[2:1] == 2'b00 && lo == 5'd0) ||
                       (hi[2:1] == 2'b01 && lo[4:2] == 3'd0);
      3'b111:  legal = (hi == 3'b000 || hi == 3'b001 || hi == 3'b010 || hi == 3'b100);  // SET
      default: legal = 1'b0;
    endcase
  end

  wire [12:0] ops      = {op_irqset, op_irqclr, op_trig, op_tstart, op_jmp, op_wait, op_in, op_out,
                          op_push, op_pull, op_mov, op_set, illegal};
  wire [3:0] ops_count = op_jmp + op_wait + op_in + op_out + op_push + op_pull + op_mov + op_set +
                         op_trig + op_tstart + op_irqset + op_irqclr + illegal;

  always @(*) begin
    // Exactly one of the operations or `illegal` is decoded.
    assert (ops_count == 4'd1);
    // `illegal` is raised for the reserved encodings and for nothing else.
    assert (illegal == !legal);
    // Each operation is decoded only from its own opcode.
    assert (!op_jmp || opcode == 3'b000);
    assert (!op_wait || opcode == 3'b001);
    assert (!op_in || opcode == 3'b010);
    assert (!op_out || opcode == 3'b011);
    assert (!(op_push || op_pull) || opcode == 3'b100);
    assert (!op_push || !instr[7]);
    assert (!op_pull || instr[7]);
    assert (!op_mov || opcode == 3'b101);
    assert (!op_set || opcode == 3'b111);
    assert (!(op_trig || op_tstart || op_irqset || op_irqclr) || opcode == 3'b110);
    assert (!op_trig || hi == 3'b000);
    assert (!op_tstart || hi == 3'b001);
    assert (!op_irqset || hi == 3'b010);
    assert (!op_irqclr || hi == 3'b011);
    assert (irq_index == lo[1:0]);
    // wait_flag is the raw decode of the source field (used only when op_wait).
    assert (wait_flag == (instr[6:5] == 2'b10));
    // A bit count is never 0: a field of 0 means 32.
    assert (count >= 6'd1 && count <= 6'd32);
  end

  // ---------------------------------------------------------------- delay and side-set

  wire [2:0] used  = {1'b0, side_cnt} + {2'b00, side_opt};  // bits that are not delay
  wire [2:0] width = 3'd5 - used;                           // delay bits

  always @(*) begin
    // The delay never exceeds what its bits can hold ...
    assert ({1'b0, delay} < (6'd1 << width));
    // ... the side-set value fits in side_cnt bits ...
    assert ({1'b0, side_val} < (4'd1 << side_cnt));
    // ... and together with the enable bit they account for every bit of the
    // field, so no two fields decode to the same delay and side-set.
    if (side_en || !side_opt)
      assert (instr[12:8] == (({4'd0, side_opt} << 4) | ({2'b00, side_val} << width) | delay));
    // Side-set is off with no side-set bits, always on when not optional,
    // and otherwise follows the enable bit.
    assert (side_en == ((side_cnt != 2'd0) && (!side_opt || instr[12])));
  end

  // ---------------------------------------------------------------- independence

  // Same field and configuration, any other opcode and operands.
  wire [4:0] delay_b;
  wire       side_en_b;
  wire [2:0] side_val_b;

  pe_decoder same_field (
      .instr({other[15:13], instr[12:8], other[7:0]}), .side_cnt(side_cnt), .side_opt(side_opt),
      .op_jmp(), .op_wait(), .op_in(), .op_out(), .op_push(), .op_pull(), .op_mov(), .op_set(),
      .op_trig(), .op_tstart(), .op_irqset(), .op_irqclr(), .irq_index(), .illegal(), .sel(),
      .arg(), .count(), .wait_pol(), .wait_rel(), .wait_flag(), .wait_idx(), .pp_if(),
      .pp_block(), .mov_op(), .mov_src(),
      .delay(delay_b), .side_en(side_en_b), .side_val(side_val_b)
  );

  // Same opcode and operands, any other field and configuration.
  wire [12:0] ops_c;
  wire [2:0] sel_c;
  wire [4:0] arg_c;
  wire [5:0] count_c;

  pe_decoder same_operation (
      .instr({instr[15:13], other[12:8], instr[7:0]}), .side_cnt(other_cnt), .side_opt(other_opt),
      .op_irqset(ops_c[12]), .op_irqclr(ops_c[11]), .op_trig(ops_c[10]), .op_tstart(ops_c[9]),
      .op_jmp(ops_c[8]), .op_wait(ops_c[7]), .op_in(ops_c[6]), .op_out(ops_c[5]),
      .op_push(ops_c[4]), .op_pull(ops_c[3]), .op_mov(ops_c[2]), .op_set(ops_c[1]),
      .irq_index(), .illegal(ops_c[0]), .sel(sel_c), .arg(arg_c), .count(count_c),
      .wait_pol(), .wait_rel(), .wait_flag(),
      .wait_idx(), .pp_if(), .pp_block(), .mov_op(), .mov_src(),
      .delay(), .side_en(), .side_val()
  );

  always @(*) begin
    // Timing does not depend on what the instruction is, legal or not ...
    assert (delay == delay_b && side_en == side_en_b && side_val == side_val_b);
    // ... and what the instruction does does not depend on its timing bits.
    assert (ops == ops_c && sel == sel_c && arg == arg_c && count == count_c);
  end

  wire _unused = &{wait_pol, wait_rel, wait_idx, pp_if, pp_block, mov_op, mov_src, 1'b0};

endmodule
