/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// One state machine with everything that belongs to it alone: its
// configuration registers, clock divider and TX and RX FIFOs. The host reaches
// the registers through a byte-wide bus (register map in docs/info.md).
module pe_smslot (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        ena,

    // Register bus
    input  wire        sel,
    input  wire [4:0]  addr,
    input  wire [7:0]  wdata,
    input  wire        we,
    input  wire        re,        // the byte at `addr` has been read out
    output reg  [7:0]  rdata,

    // Instruction memory
    output wire [4:0]  pc,
    input  wire [15:0] instr,

    // Pins
    input  wire [7:0]  pins_in,
    output wire [7:0]  out_mask,
    output wire [7:0]  out_val,
    output wire [7:0]  dir_mask,
    output wire [7:0]  dir_val,

    // Trigger and cycle timer
    input  wire [31:0] timer,
    output wire        trig_fire,
    output wire        timer_reset,

    // Shared inter-state-machine flags
    input  wire [3:0]  flags_in,
    output wire [3:0]  flag_set,
    output wire [3:0]  flag_clr,

    // Status
    output reg         en,
    output wire        rx_ready,  // RX FIFO holds data
    output wire        tx_ready   // TX FIFO has room
);

  localparam [4:0] A_CTRL        = 5'h00,
                   A_STATUS      = 5'h01,
                   A_FLEVEL      = 5'h02,
                   A_PC          = 5'h03,
                   A_CLKDIV_FRAC = 5'h04,
                   A_CLKDIV_INTL = 5'h05,
                   A_CLKDIV_INTH = 5'h06,
                   A_WRAP_BOTTOM = 5'h07,
                   A_WRAP_TOP    = 5'h08,
                   A_PINCTRL0    = 5'h09,
                   A_PINCTRL1    = 5'h0A,
                   A_PINCTRL2    = 5'h0B,
                   A_EXECCTRL    = 5'h0C,
                   A_PUSH_THRESH = 5'h0D,
                   A_PULL_THRESH = 5'h0E,
                   A_TXF0        = 5'h10,
                   A_TXF1        = 5'h11,
                   A_TXF2        = 5'h12,
                   A_TXF3        = 5'h13,
                   A_RXF0        = 5'h14,
                   A_RXF1        = 5'h15,
                   A_RXF2        = 5'h16,
                   A_RXF3        = 5'h17;

  wire wr = sel && we;

  // ------------------------------------------------------------ registers

  reg [7:0]  div_frac;
  reg [15:0] div_int;
  reg [4:0]  wrap_bottom, wrap_top;
  reg [2:0]  in_base, out_base, set_base, set_count, side_base, jmp_pin;
  reg [7:0]  execctrl;
  reg [4:0]  push_thresh, pull_thresh;
  reg [23:0] tx_hold;  // low three bytes of the TX word being written

  always @(posedge clk) begin
    if (!rst_n) begin
      en          <= 1'b0;
      div_frac    <= 8'd0;
      div_int     <= 16'd1;
      wrap_bottom <= 5'd0;
      wrap_top    <= 5'd31;
      in_base     <= 3'd0;
      out_base    <= 3'd0;
      set_base    <= 3'd0;
      set_count   <= 3'd0;
      side_base   <= 3'd0;
      jmp_pin     <= 3'd0;
      execctrl    <= 8'd0;
      push_thresh <= 5'd0;
      pull_thresh <= 5'd0;
      tx_hold     <= 24'd0;
    end else if (wr) begin
      case (addr)
        A_CTRL:        en <= wdata[0];
        A_CLKDIV_FRAC: div_frac <= wdata;
        A_CLKDIV_INTL: div_int[7:0] <= wdata;
        A_CLKDIV_INTH: div_int[15:8] <= wdata;
        A_WRAP_BOTTOM: wrap_bottom <= wdata[4:0];
        A_WRAP_TOP:    wrap_top <= wdata[4:0];
        A_PINCTRL0:    {out_base, in_base} <= wdata[5:0];
        A_PINCTRL1:    {set_count, set_base} <= wdata[5:0];
        A_PINCTRL2:    {jmp_pin, side_base} <= wdata[5:0];
        A_EXECCTRL:    execctrl <= wdata;
        A_PUSH_THRESH: push_thresh <= wdata[4:0];
        A_PULL_THRESH: pull_thresh <= wdata[4:0];
        A_TXF0:        tx_hold[7:0] <= wdata;
        A_TXF1:        tx_hold[15:8] <= wdata;
        A_TXF2:        tx_hold[23:16] <= wdata;
        default:       ;
      endcase
    end
  end

  // Write-one strobes in CTRL.
  wire sm_restart   = wr && (addr == A_CTRL) && wdata[1];
  wire div_restart  = wr && (addr == A_CTRL) && wdata[2];
  wire fifo_clear   = wr && (addr == A_CTRL) && wdata[3];

  // ------------------------------------------------------------ datapath

  wire        tick;
  wire [31:0] tx_data, rx_data, rx_head;
  wire        tx_empty, tx_full, tx_pop;
  wire        rx_empty, rx_full, rx_push;
  wire [2:0]  tx_level, rx_level;
  wire        flag_illegal, flag_rxover, stalled;

  pe_clkdiv clkdiv (
      .clk     (clk),
      .rst_n   (rst_n),
      .en      (en && ena),
      .restart (div_restart),
      .div_int (div_int),
      .div_frac(div_frac),
      .tick    (tick)
  );

  pe_sm sm (
      .clk         (clk),
      .rst_n       (rst_n),
      .tick        (tick),
      .restart     (sm_restart),
      .pc_wr       (wr && (addr == A_PC)),
      .pc_wdata    (wdata[4:0]),
      .pc          (pc),
      .instr       (instr),
      .wrap_bottom (wrap_bottom),
      .wrap_top    (wrap_top),
      .in_base     (in_base),
      .out_base    (out_base),
      .set_base    (set_base),
      .set_count   (set_count),
      .side_base   (side_base),
      .side_cnt    (execctrl[1:0]),
      .side_opt    (execctrl[2]),
      .side_pindir (execctrl[3]),
      .jmp_pin     (jmp_pin),
      .in_dir      (execctrl[4]),
      .out_dir     (execctrl[5]),
      .autopush    (execctrl[6]),
      .autopull    (execctrl[7]),
      .push_thresh (push_thresh),
      .pull_thresh (pull_thresh),
      .pins_in     (pins_in),
      .out_mask    (out_mask),
      .out_val     (out_val),
      .dir_mask    (dir_mask),
      .dir_val     (dir_val),
      .tx_data     (tx_data),
      .tx_empty    (tx_empty),
      .tx_pop      (tx_pop),
      .rx_data     (rx_data),
      .rx_full     (rx_full),
      .rx_push     (rx_push),
      .timer       (timer),
      .trig_fire   (trig_fire),
      .timer_reset (timer_reset),
      .flags_in    (flags_in),
      .flag_set    (flag_set),
      .flag_clr    (flag_clr),
      .flag_illegal(flag_illegal),
      .flag_rxover (flag_rxover),
      .stalled     (stalled)
  );

  // Writing the top byte pushes the whole word.
  pe_fifo tx_fifo (
      .clk  (clk),
      .rst_n(rst_n),
      .clear(fifo_clear),
      .push (wr && (addr == A_TXF3)),
      .din  ({wdata, tx_hold}),
      .pop  (tx_pop),
      .dout (tx_data),
      .full (tx_full),
      .empty(tx_empty),
      .level(tx_level)
  );

  // Reading the top byte pops the word.
  pe_fifo rx_fifo (
      .clk  (clk),
      .rst_n(rst_n),
      .clear(fifo_clear),
      .push (rx_push),
      .din  (rx_data),
      .pop  (sel && re && (addr == A_RXF3)),
      .dout (rx_head),
      .full (rx_full),
      .empty(rx_empty),
      .level(rx_level)
  );

  assign rx_ready = !rx_empty;
  assign tx_ready = !tx_full;

  // ------------------------------------------------------------ read

  always @(*) begin
    case (addr)
      A_CTRL:        rdata = {7'd0, en};
      A_STATUS:      rdata = {1'b0, stalled, flag_rxover, flag_illegal,
                              rx_empty, rx_full, tx_empty, tx_full};
      A_FLEVEL:      rdata = {1'b0, rx_level, 1'b0, tx_level};
      A_PC:          rdata = {3'd0, pc};
      A_CLKDIV_FRAC: rdata = div_frac;
      A_CLKDIV_INTL: rdata = div_int[7:0];
      A_CLKDIV_INTH: rdata = div_int[15:8];
      A_WRAP_BOTTOM: rdata = {3'd0, wrap_bottom};
      A_WRAP_TOP:    rdata = {3'd0, wrap_top};
      A_PINCTRL0:    rdata = {2'd0, out_base, in_base};
      A_PINCTRL1:    rdata = {2'd0, set_count, set_base};
      A_PINCTRL2:    rdata = {2'd0, jmp_pin, side_base};
      A_EXECCTRL:    rdata = execctrl;
      A_PUSH_THRESH: rdata = {3'd0, push_thresh};
      A_PULL_THRESH: rdata = {3'd0, pull_thresh};
      A_RXF0:        rdata = rx_head[7:0];
      A_RXF1:        rdata = rx_head[15:8];
      A_RXF2:        rdata = rx_head[23:16];
      A_RXF3:        rdata = rx_head[31:24];
      default:       rdata = 8'd0;
    endcase
  end

endmodule
