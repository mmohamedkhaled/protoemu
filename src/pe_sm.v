/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// One protocol state machine: program counter, X and Y, the shift registers
// and the execute logic (docs/isa.md). The clock divider, the FIFOs and the
// instruction memory are outside; this block sees them as `tick`, the tx_*/rx_*
// ports and `instr`.
//
// On a tick the machine either executes the instruction at `pc`, stalls on it,
// or counts down a delay. Pin writes are presented combinationally in the tick
// cycle as mask/value pairs; the pin registers themselves are shared and live
// in pe_core.
module pe_sm (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        tick,
    input  wire        restart,      // clear ISR, OSR, counts, delay and flags
    input  wire        pc_wr,        // host write to the program counter
    input  wire [4:0]  pc_wdata,

    // Instruction memory
    output reg  [4:0]  pc,
    input  wire [15:0] instr,

    // Configuration
    input  wire [4:0]  wrap_bottom,
    input  wire [4:0]  wrap_top,
    input  wire [2:0]  in_base,
    input  wire [2:0]  out_base,
    input  wire [2:0]  set_base,
    input  wire [2:0]  set_count,
    input  wire [2:0]  side_base,
    input  wire [1:0]  side_cnt,
    input  wire        side_opt,
    input  wire        side_pindir,
    input  wire [2:0]  jmp_pin,
    input  wire        in_dir,
    input  wire        out_dir,
    input  wire        autopush,
    input  wire        autopull,
    input  wire [4:0]  push_thresh,  // 0 means 32
    input  wire [4:0]  pull_thresh,  // 0 means 32

    // Pins
    input  wire [7:0]  pins_in,      // synchronised pad values
    output wire [7:0]  out_mask,     // PIN_OUT bits written this cycle
    output wire [7:0]  out_val,
    output wire [7:0]  dir_mask,     // PIN_DIR bits written this cycle
    output wire [7:0]  dir_val,

    // FIFOs
    input  wire [31:0] tx_data,
    input  wire        tx_empty,
    output wire        tx_pop,
    output wire [31:0] rx_data,
    input  wire        rx_full,
    output wire        rx_push,

    // Trigger and cycle timer, shared with the other state machine
    input  wire [31:0] timer,
    output wire        trig_fire,
    output wire        timer_reset,

    // Shared inter-state-machine flags
    input  wire [3:0]  flags_in,
    output wire [3:0]  flag_set,  // flags this instruction sets this tick
    output wire [3:0]  flag_clr,  // flags this instruction clears this tick

    // Status
    output reg         flag_illegal, // sticky: an illegal instruction was executed
    output reg         flag_rxover,  // sticky: a non-blocking PUSH dropped data
    output wire        stalled
`ifdef FORMAL
    ,
    output wire [31:0] f_osr         // for proofs about firmware (formal/fw_*.v)
`endif
);

  function [7:0] rotl8(input [7:0] v, input [2:0] k);
    rotl8 = (v << k) | (v >> (4'd8 - {1'b0, k}));
  endfunction

  function [7:0] rotr8(input [7:0] v, input [2:0] k);
    rotr8 = (v >> k) | (v << (4'd8 - {1'b0, k}));
  endfunction

  function [31:0] reverse(input [31:0] v);
    integer i;
    for (i = 0; i < 32; i = i + 1) reverse[i] = v[31-i];
  endfunction

  // ------------------------------------------------------------ decode

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

  pe_decoder decoder (
      .instr   (instr),
      .side_cnt(side_cnt),
      .side_opt(side_opt),
      .op_jmp  (op_jmp),
      .op_wait (op_wait),
      .op_in   (op_in),
      .op_out  (op_out),
      .op_push (op_push),
      .op_pull (op_pull),
      .op_mov  (op_mov),
      .op_set  (op_set),
      .op_trig (op_trig),
      .op_tstart(op_tstart),
      .op_irqset(op_irqset),
      .op_irqclr(op_irqclr),
      .irq_index(irq_index),
      .illegal (illegal),
      .sel     (sel),
      .arg     (arg),
      .count   (count),
      .wait_pol(wait_pol),
      .wait_rel(wait_rel),
      .wait_flag(wait_flag),
      .wait_idx(wait_idx),
      .pp_if   (pp_if),
      .pp_block(pp_block),
      .mov_op  (mov_op),
      .mov_src (mov_src),
      .delay   (delay),
      .side_en (side_en),
      .side_val(side_val)
  );

  // ------------------------------------------------------------ state

  reg  [31:0] x;
  reg  [31:0] y;
  reg  [4:0]  delay_cnt;

  wire [31:0] isr, osr, isr_shifted, out_data;
  wire [5:0]  isr_count, osr_count, isr_count_next, osr_count_next;

  // ------------------------------------------------------------ stall

  wire [5:0] push_thr = {push_thresh == 5'd0, push_thresh};
  wire [5:0] pull_thr = {pull_thresh == 5'd0, pull_thresh};

  wire [7:0] pins_rel = rotr8(pins_in, in_base);  // pin IN_BASE in bit 0
  wire       wait_pin = wait_rel ? pins_rel[wait_idx] : pins_in[wait_idx];
  // WAIT tests a shared flag when wait_flag is set, otherwise a pin.
  wire       wait_bit = wait_flag ? flags_in[wait_idx[1:0]] : wait_pin;

  wire osr_empty    = (osr_count >= pull_thr);
  wire autopush_due = op_in && autopush && (isr_count_next >= push_thr);
  wire push_cond    = op_push && (!pp_if || (isr_count >= push_thr));
  wire pull_cond    = op_pull && (!pp_if || osr_empty);

  wire stall = (op_wait && (wait_bit != wait_pol)) ||
               (autopush_due && rx_full) ||
               (op_out && autopull && osr_empty) ||
               (push_cond && pp_block && rx_full) ||
               (pull_cond && pp_block && tx_empty);

  wire slot = tick && (delay_cnt == 5'd0);  // an instruction is presented
  wire exec = slot && !stall;               // ... and takes effect

  assign stalled = slot && stall;

  // ------------------------------------------------------------ operands

  wire [31:0] pins_word = {24'd0, pins_rel};

  wire [31:0] in_src = (sel[1:0] == 2'b00) ? pins_word :
                       (sel[1:0] == 2'b01) ? x         :
                       (sel[1:0] == 2'b10) ? y         : 32'd0;

  wire [31:0] mov_raw = (mov_src == 3'b000) ? pins_word :
                        (mov_src == 3'b001) ? x         :
                        (mov_src == 3'b010) ? y         :
                        (mov_src == 3'b101) ? timer     :
                        (mov_src == 3'b110) ? isr       :
                        (mov_src == 3'b111) ? osr       : 32'd0;

  wire [31:0] mov_val = (mov_op == 2'b01) ? ~mov_raw         :
                        (mov_op == 2'b10) ? reverse(mov_raw) : mov_raw;

  wire x_zero = (x == 32'd0);
  wire y_zero = (y == 32'd0);

  reg jmp_cond;
  always @(*) begin
    case (sel)
      3'b000:  jmp_cond = 1'b1;
      3'b001:  jmp_cond = x_zero;
      3'b010:  jmp_cond = !x_zero;
      3'b011:  jmp_cond = y_zero;
      3'b100:  jmp_cond = !y_zero;
      3'b101:  jmp_cond = (x != y);
      3'b110:  jmp_cond = pins_in[jmp_pin];
      default: jmp_cond = !osr_empty;
    endcase
  end

  // ------------------------------------------------------------ X, Y

  always @(posedge clk) begin
    if (!rst_n) begin
      x <= 32'd0;
    end else if (exec) begin
      if (op_jmp && (sel == 3'b010)) x <= x - 32'd1;
      else if (op_out && (sel == 3'b001)) x <= out_data;
      else if (op_mov && (sel == 3'b001)) x <= mov_val;
      else if (op_set && (sel == 3'b001)) x <= {27'd0, arg};
    end
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      y <= 32'd0;
    end else if (exec) begin
      if (op_jmp && (sel == 3'b100)) y <= y - 32'd1;
      else if (op_out && (sel == 3'b010)) y <= out_data;
      else if (op_mov && (sel == 3'b010)) y <= mov_val;
      else if (op_set && (sel == 3'b010)) y <= {27'd0, arg};
    end
  end

  // ------------------------------------------------------------ PC, delay

  wire       pc_set = (op_jmp && jmp_cond) || ((op_out || op_mov) && (sel == 3'b101));
  wire [4:0] pc_tgt = op_jmp ? arg : op_out ? out_data[4:0] : mov_val[4:0];
  wire [4:0] pc_seq = (pc == wrap_top) ? wrap_bottom : (pc + 5'd1);

  always @(posedge clk) begin
    if (!rst_n) pc <= 5'd0;
    else if (pc_wr) pc <= pc_wdata;
    else if (exec) pc <= pc_set ? pc_tgt : pc_seq;
  end

  always @(posedge clk) begin
    if (!rst_n || restart) delay_cnt <= 5'd0;
    else if (exec) delay_cnt <= delay;
    else if (tick && (delay_cnt != 5'd0)) delay_cnt <= delay_cnt - 5'd1;
  end

  // ------------------------------------------------------------ shift registers and FIFOs

  wire pull_exec = exec && pull_cond;
  wire mov_osr   = exec && op_mov && (sel == 3'b111);
  wire out_exec  = exec && op_out;

  // Autopull refill runs on every tick, on the OSR count as it stands after
  // this tick's instruction, so the OUT that uses the last bit also refills.
  wire [5:0] osr_count_post = out_exec ? osr_count_next : osr_count;
  wire       refill = tick && autopull && !tx_empty && !pull_exec && !mov_osr &&
                      (osr_count_post >= pull_thr);

  assign trig_fire   = exec && op_trig;
  assign timer_reset = exec && op_tstart;

  // One-hot over the four flags, only on the tick the irq instruction executes.
  wire [3:0] irq_onehot = 4'd1 << irq_index;
  assign flag_set = (exec && op_irqset) ? irq_onehot : 4'd0;
  assign flag_clr = (exec && op_irqclr) ? irq_onehot : 4'd0;

  assign tx_pop  = refill || (pull_exec && !tx_empty);
  assign rx_push = exec && (autopush_due || (push_cond && !rx_full));
  assign rx_data = op_in ? isr_shifted : isr;

  pe_shift shift (
      .clk           (clk),
      .rst_n         (rst_n),
      .clear         (restart),
      .in_dir        (in_dir),
      .in_shift      (exec && op_in),
      .in_n          (count),
      .in_data       (in_src),
      .isr_load      (exec && op_mov && (sel == 3'b110)),
      .isr_load_data (mov_val),
      .isr_clear     (exec && (autopush_due || push_cond)),
      .isr           (isr),
      .isr_count     (isr_count),
      .isr_shifted   (isr_shifted),
      .isr_count_next(isr_count_next),
      .out_dir       (out_dir),
      .out_shift     (out_exec),
      .out_n         (count),
      .osr_load      (refill || pull_exec || mov_osr),
      .osr_load_data (mov_osr ? mov_val : (pull_exec && tx_empty) ? x : tx_data),
      .osr           (osr),
      .osr_count     (osr_count),
      .out_data      (out_data),
      .osr_count_next(osr_count_next)
  );

  // ------------------------------------------------------------ pins

  wire [7:0] out_n_mask = (count >= 6'd8) ? 8'hFF : ((8'd1 << count[2:0]) - 8'd1);
  wire [4:0] set_n_mask = (set_count >= 3'd5) ? 5'b11111 : ((5'd1 << set_count) - 5'd1);
  wire [2:0] side_n_mask = (3'd1 << side_cnt) - 3'd1;

  wire       to_pins = (sel == 3'b000);
  wire       to_dirs = (sel == 3'b100);
  wire [7:0] wr_mask = !exec  ? 8'd0 :
                       op_out ? rotl8(out_n_mask, out_base) :
                       op_set ? rotl8({3'b000, set_n_mask}, set_base) : 8'd0;
  wire [7:0] wr_val  = op_out ? rotl8(out_data[7:0], out_base) : rotl8({3'b000, arg}, set_base);

  // Side-set applies on every tick the instruction is presented, stalled or
  // not, and wins over OUT and SET on the same pin.
  wire [7:0] side_mask = (slot && side_en) ? rotl8({5'd0, side_n_mask}, side_base) : 8'd0;
  wire [7:0] side_bits = rotl8({5'd0, side_val}, side_base);

  wire [7:0] side_out = side_pindir ? 8'd0 : side_mask;
  wire [7:0] side_dir = side_pindir ? side_mask : 8'd0;
  wire [7:0] wr_out   = to_pins ? wr_mask : 8'd0;
  wire [7:0] wr_dir   = to_dirs ? wr_mask : 8'd0;

  assign out_mask = wr_out | side_out;
  assign out_val  = (wr_val & ~side_out) | (side_bits & side_out);
  assign dir_mask = wr_dir | side_dir;
  assign dir_val  = (wr_val & ~side_dir) | (side_bits & side_dir);

  // ------------------------------------------------------------ flags

  always @(posedge clk) begin
    if (!rst_n || restart) begin
      flag_illegal <= 1'b0;
      flag_rxover  <= 1'b0;
    end else begin
      if (exec && illegal) flag_illegal <= 1'b1;
      if (exec && push_cond && rx_full) flag_rxover <= 1'b1;
    end
  end

  // ------------------------------------------------------------ formal properties
  //
  // Proved by SymbiYosys for every instruction stream, configuration and
  // input sequence (formal/sm.sby). `instr` is left unconstrained, so the
  // proofs do not depend on what is in the instruction memory.

`ifdef FORMAL
  assign f_osr = osr;

  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);

  // Shadow of the delay guarantee: after an instruction executes with delay
  // value f_d, f_n counts the ticks that have passed since.
  reg       f_valid = 1'b0;
  reg [4:0] f_d = 5'd0;
  reg [4:0] f_n = 5'd0;
  always @(posedge clk) begin
    if (!rst_n || restart) f_valid <= 1'b0;
    else if (tick) begin
      if (exec) begin
        f_valid <= 1'b1;
        f_d     <= delay;
        f_n     <= 5'd0;
      end else if (slot) f_valid <= 1'b0;
      else f_n <= f_n + 5'd1;
    end
  end

  always @(*) begin
    if (rst_n) begin
      // Delay guarantee (docs/isa.md section 3): the next instruction is
      // presented on exactly the (d + 1)-th tick after this one executed.
      if (f_valid) assert (f_n <= f_d);
      if (f_valid) assert (delay_cnt == f_d - f_n);
      if (f_valid && tick && slot) assert (f_n == f_d);
      if (f_valid && tick && !slot) assert (f_n < f_d);

      // Outside the tick on which an instruction is presented, nothing
      // reaches the pins or the RX FIFO.
      if (!slot) assert (out_mask == 8'd0 && dir_mask == 8'd0 && !rx_push && !stalled);
      if (!slot) assert (!trig_fire && !timer_reset);
      if (!slot) assert (flag_set == 4'd0 && flag_clr == 4'd0);
      if (!tick) assert (!tx_pop);

      // A stalled instruction applies its side-set and nothing else.
      if (stalled) assert (wr_mask == 8'd0 && !rx_push && !trig_fire && !timer_reset);
      if (stalled) assert (flag_set == 4'd0 && flag_clr == 4'd0);
      // The same flag is never set and cleared on one tick.
      assert ((flag_set & flag_clr) == 4'd0);

      // The FIFOs are never overrun or underrun.
      if (rx_push) assert (!rx_full);
      if (tx_pop) assert (!tx_empty);

      assert (isr_count <= 6'd32 && osr_count <= 6'd32);
    end
  end

  always @(posedge clk) begin
    if (f_past_valid && $past(rst_n) && rst_n && !$past(restart)) begin
      // Atomicity (section 1.2): on a cycle where no instruction executes -
      // no tick, a delay tick or a stall - the architectural state holds.
      // The only exception is an autopull refill of the OSR.
      if (!$past(exec)) begin
        if (!$past(pc_wr)) assert (pc == $past(pc));
        assert (x == $past(x) && y == $past(y));
        assert (isr == $past(isr) && isr_count == $past(isr_count));
        assert (flag_illegal == $past(flag_illegal) && flag_rxover == $past(flag_rxover));
        if (!$past(refill)) assert (osr == $past(osr) && osr_count == $past(osr_count));
      end

      // The delay counter loads on execute and counts ticks, nothing else.
      if ($past(exec)) assert (delay_cnt == $past(delay));
      else if ($past(tick) && ($past(delay_cnt) != 5'd0)) assert (delay_cnt == $past(delay_cnt) - 5'd1);
      else assert (delay_cnt == $past(delay_cnt));

      // An illegal instruction is a no-op that sets the flag (section 2).
      if ($past(exec) && $past(illegal)) begin
        assert (flag_illegal);
        assert (x == $past(x) && y == $past(y));
        assert (isr == $past(isr) && isr_count == $past(isr_count));
        if (!$past(pc_wr)) assert (pc == $past(pc_seq));
        if (!$past(refill)) assert (osr == $past(osr) && osr_count == $past(osr_count));
      end
    end
  end

  // Reachability, so the proofs above are not vacuous.
  always @(posedge clk) begin
    if (rst_n) begin
      cover (f_valid && tick && slot && (f_d == 5'd31));  // a full 31-tick delay elapsed
      cover (stalled && (out_mask != 8'd0));              // side-set while stalled
      cover (exec && illegal);
      cover (rx_push && tx_pop);
    end
  end
`endif

endmodule
