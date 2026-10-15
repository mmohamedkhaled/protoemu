/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// The protocol emulator without its host port: two state machines, the shared
// instruction memory and the shared pin registers, behind a byte-wide register
// bus (register map in docs/info.md).
//
//   0x00-0x17  state machine 0        0x18-0x1C  pins and ID
//   0x20-0x37  state machine 1        0x38-0x3B  trigger
//   0x40-0x7F  instruction memory
module pe_core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       ena,

    // Register bus
    input  wire [6:0] bus_addr,
    input  wire [7:0] bus_wdata,
    input  wire       bus_we,
    input  wire       bus_re,       // the byte at bus_addr has been read out
    output reg  [7:0] bus_rdata,

    // Pins
    input  wire [7:0] pad_in,
    output wire [7:0] pad_out,
    output wire [7:0] pad_oe,

    // FIFO status, for the host to poll on pins
    output wire [1:0] rx_ready,
    output wire [1:0] tx_ready,

    // Trigger pulse
    output wire       trig_out
);

  localparam [7:0] ID = 8'h5E;

  localparam [6:0] A_PIN_OUT = 7'h18,
                   A_PIN_DIR = 7'h19,
                   A_PIN_OD  = 7'h1A,
                   A_PIN_IN  = 7'h1B,
                   A_ID      = 7'h1C,
                   A_TRIG_DL = 7'h38,
                   A_TRIG_DH = 7'h39,
                   A_TRIG_W  = 7'h3A,
                   A_TRIG_C  = 7'h3B,
                   A_FLAGS   = 7'h3C,
                   A_TRIG_ML = 7'h3D,
                   A_TRIG_MH = 7'h3E;

  wire sel_sm0  = (bus_addr[6:5] == 2'b00);
  wire sel_sm1  = (bus_addr[6:5] == 2'b01);
  wire sel_imem = bus_addr[6];

  // ------------------------------------------------------------ pins

  reg [7:0] pin_out, pin_dir, pin_od;
  reg [7:0] sync1, pins_in;

  wire [7:0] out_mask0, out_val0, dir_mask0, dir_val0;
  wire [7:0] out_mask1, out_val1, dir_mask1, dir_val1;

  always @(posedge clk) begin
    if (!rst_n) begin
      sync1   <= 8'd0;
      pins_in <= 8'd0;
    end else begin
      sync1   <= pad_in;
      pins_in <= sync1;
    end
  end

  // Later assignments win: host, then state machine 0, then state machine 1.
  reg [7:0] pin_out_next, pin_dir_next;
  always @(*) begin
    pin_out_next = pin_out;
    pin_dir_next = pin_dir;
    if (bus_we && (bus_addr == A_PIN_OUT)) pin_out_next = bus_wdata;
    if (bus_we && (bus_addr == A_PIN_DIR)) pin_dir_next = bus_wdata;
    pin_out_next = (pin_out_next & ~out_mask0) | (out_val0 & out_mask0);
    pin_dir_next = (pin_dir_next & ~dir_mask0) | (dir_val0 & dir_mask0);
    pin_out_next = (pin_out_next & ~out_mask1) | (out_val1 & out_mask1);
    pin_dir_next = (pin_dir_next & ~dir_mask1) | (dir_val1 & dir_mask1);
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      pin_out <= 8'd0;
      pin_dir <= 8'd0;
      pin_od  <= 8'd0;
    end else begin
      pin_out <= pin_out_next;
      pin_dir <= pin_dir_next;
      if (bus_we && (bus_addr == A_PIN_OD)) pin_od <= bus_wdata;
    end
  end

  // An open-drain pin drives only when its output level is 0.
  assign pad_out = pin_out;
  assign pad_oe  = pin_dir & ~(pin_od & pin_out) & {8{ena}};

  // ------------------------------------------------------------ trigger and cycle timer

  reg  [15:0] trig_delay;
  reg  [7:0]  trig_width;
  reg         trig_invert;
  reg  [15:0] trig_match;    // fire when TIMER reaches this value (scheduled glitch)
  reg         trig_match_en;
  wire [31:0] timer;
  wire        trig_fire0, trig_fire1, timer_reset0, timer_reset1;
  wire        trig_pulse, trig_busy;

  always @(posedge clk) begin
    if (!rst_n) begin
      trig_delay    <= 16'd0;
      trig_width    <= 8'd0;
      trig_invert   <= 1'b0;
      trig_match    <= 16'd0;
      trig_match_en <= 1'b0;
    end else if (bus_we) begin
      if (bus_addr == A_TRIG_DL) trig_delay[7:0] <= bus_wdata;
      if (bus_addr == A_TRIG_DH) trig_delay[15:8] <= bus_wdata;
      if (bus_addr == A_TRIG_W) trig_width <= bus_wdata;
      if (bus_addr == A_TRIG_C) {trig_match_en, trig_invert} <= {bus_wdata[2], bus_wdata[0]};
      if (bus_addr == A_TRIG_ML) trig_match[7:0] <= bus_wdata;
      if (bus_addr == A_TRIG_MH) trig_match[15:8] <= bus_wdata;
    end
  end

  // The host can fire the trigger by hand: write 1 to bit 1 of the control register.
  wire host_fire = bus_we && (bus_addr == A_TRIG_C) && bus_wdata[1];
  // Scheduled fire: when the cycle timer reaches the match value (once per run,
  // within the first 65536 cycles after a tstart). Reuses the timer, so it adds
  // no always-on switching of its own.
  wire match_fire = trig_match_en && ena && (timer == {16'd0, trig_match});

  pe_trigger trigger (
      .clk        (clk),
      .rst_n      (rst_n),
      .ena        (ena),
      .fire       (trig_fire0 || trig_fire1 || host_fire || match_fire),
      .delay      (trig_delay),
      .width      (trig_width),
      .out        (trig_pulse),
      .busy       (trig_busy),
      .timer_reset(timer_reset0 || timer_reset1),
      .timer      (timer)
  );

  assign trig_out = trig_pulse ^ trig_invert;

  // ------------------------------------------------------------ shared flags

  reg  [3:0] flags;
  wire [3:0] set0, set1, clr0, clr1;
  // Both state machines and the host can touch the flags on the same clock.
  // A set wins over a clear, and a state machine wins over the host, so the
  // flag a program raises is seen even if the host clears it in the same cycle.
  wire [3:0] host_set = (bus_we && (bus_addr == A_FLAGS)) ? bus_wdata[3:0] : 4'd0;
  wire [3:0] host_clr = (bus_we && (bus_addr == A_FLAGS)) ? ~bus_wdata[3:0] : 4'd0;
  wire [3:0] flag_set = set0 | set1;
  wire [3:0] flag_clr = clr0 | clr1;

  always @(posedge clk) begin
    if (!rst_n) flags <= 4'd0;
    else flags <= (((flags & ~host_clr) | host_set) & ~flag_clr) | flag_set;
  end

  // ------------------------------------------------------------ instruction memory

  wire [4:0]  pc0, pc1;
  wire [15:0] instr0, instr1;
  wire        en0, en1;

  // The host reads the memory back through state machine 0's port, so the
  // read-back is valid only while state machine 0 is disabled.
  pe_imem imem (
      .clk   (clk),
      .we    (bus_we && sel_imem),
      .waddr (bus_addr[5:1]),
      .wsel  (bus_addr[0]),
      .wdata (bus_wdata),
      .raddr0(en0 ? pc0 : bus_addr[5:1]),
      .rdata0(instr0),
      .raddr1(pc1),
      .rdata1(instr1)
  );

  // ------------------------------------------------------------ state machines

  wire [7:0] rdata0, rdata1;

  pe_smslot slot0 (
      .clk     (clk),
      .rst_n   (rst_n),
      .ena     (ena),
      .sel     (sel_sm0),
      .addr    (bus_addr[4:0]),
      .wdata   (bus_wdata),
      .we      (bus_we),
      .re      (bus_re),
      .rdata   (rdata0),
      .pc      (pc0),
      .instr   (instr0),
      .pins_in (pins_in),
      .out_mask(out_mask0),
      .out_val (out_val0),
      .dir_mask(dir_mask0),
      .dir_val (dir_val0),
      .timer   (timer),
      .trig_fire(trig_fire0),
      .timer_reset(timer_reset0),
      .flags_in(flags),
      .flag_set(set0),
      .flag_clr(clr0),
      .en      (en0),
      .rx_ready(rx_ready[0]),
      .tx_ready(tx_ready[0])
  );

  pe_smslot slot1 (
      .clk     (clk),
      .rst_n   (rst_n),
      .ena     (ena),
      .sel     (sel_sm1),
      .addr    (bus_addr[4:0]),
      .wdata   (bus_wdata),
      .we      (bus_we),
      .re      (bus_re),
      .rdata   (rdata1),
      .pc      (pc1),
      .instr   (instr1),
      .pins_in (pins_in),
      .out_mask(out_mask1),
      .out_val (out_val1),
      .dir_mask(dir_mask1),
      .dir_val (dir_val1),
      .timer   (timer),
      .trig_fire(trig_fire1),
      .timer_reset(timer_reset1),
      .flags_in(flags),
      .flag_set(set1),
      .flag_clr(clr1),
      .en      (en1),
      .rx_ready(rx_ready[1]),
      .tx_ready(tx_ready[1])
  );

  // ------------------------------------------------------------ read

  always @(*) begin
    if (sel_imem) bus_rdata = bus_addr[0] ? instr0[15:8] : instr0[7:0];
    else begin
      case (bus_addr)
        A_PIN_OUT: bus_rdata = pin_out;
        A_PIN_DIR: bus_rdata = pin_dir;
        A_PIN_OD:  bus_rdata = pin_od;
        A_PIN_IN:  bus_rdata = pins_in;
        A_ID:      bus_rdata = ID;
        A_TRIG_DL: bus_rdata = trig_delay[7:0];
        A_TRIG_DH: bus_rdata = trig_delay[15:8];
        A_TRIG_W:  bus_rdata = trig_width;
        A_TRIG_C:  bus_rdata = {trig_busy, 4'd0, trig_match_en, 1'b0, trig_invert};
        A_FLAGS:   bus_rdata = {4'd0, flags};
        A_TRIG_ML: bus_rdata = trig_match[7:0];
        A_TRIG_MH: bus_rdata = trig_match[15:8];
        default:   bus_rdata = sel_sm1 ? rdata1 : rdata0;
      endcase
    end
  end

  wire _unused = &{en1, 1'b0};

endmodule
