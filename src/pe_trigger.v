/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Trigger pulse generator and cycle timer, shared by both state machines
// (docs/isa.md, section 4.8). Both count clock cycles, not ticks, so their
// resolution does not depend on any clock divider.
//
// Trigger: when `fire` is high on clock edge E, `out` goes high at edge
// E + delay and low again width + 1 cycles later. A new `fire` restarts it.
//
// Timer: `timer_reset` on edge E makes an instruction that executes on edge
// E + k read the value k.
module pe_trigger (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        ena,
    input  wire        fire,
    input  wire [15:0] delay,        // clock cycles from `fire` to the pulse
    input  wire [7:0]  width,        // the pulse lasts width + 1 clock cycles
    output reg         out,
    output wire        busy,
    input  wire        timer_reset,
    output reg  [31:0] timer
);

  localparam [1:0] IDLE = 2'd0, DELAY = 2'd1, PULSE = 2'd2;

  reg [1:0]  state;
  reg [15:0] dcnt;
  reg [7:0]  wcnt;

  assign busy = (state != IDLE);

  // `out` is a register of its own so the pin never glitches.
  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      dcnt  <= 16'd0;
      wcnt  <= 8'd0;
      out   <= 1'b0;
    end else if (fire) begin
      if (delay == 16'd0) begin
        state <= PULSE;
        wcnt  <= width;
        out   <= 1'b1;
      end else begin
        state <= DELAY;
        dcnt  <= delay - 16'd1;
        out   <= 1'b0;
      end
    end else if (state == DELAY) begin
      if (dcnt == 16'd0) begin
        state <= PULSE;
        wcnt  <= width;
        out   <= 1'b1;
      end else begin
        dcnt <= dcnt - 16'd1;
      end
    end else if (state == PULSE) begin
      if (wcnt == 8'd0) begin
        state <= IDLE;
        out   <= 1'b0;
      end else begin
        wcnt <= wcnt - 8'd1;
      end
    end
  end

  always @(posedge clk) begin
    if (!rst_n) timer <= 32'd0;
    else if (timer_reset) timer <= 32'd1;
    else if (ena) timer <= timer + 32'd1;
  end

  // ------------------------------------------------------------ formal properties
  //
  // The full specification of the trigger, proved for every delay and width
  // (formal/trigger.sby): `out` is high exactly when the number of clock
  // cycles since the last `fire` is between delay and delay + width.

`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);

  // The host does not rewrite the settings while a trigger is in flight.
  always @(posedge clk) if (f_past_valid) assume ($stable(delay) && $stable(width));

  reg        f_fired = 1'b0;   // a `fire` has happened since reset
  reg [17:0] f_age = 18'd0;    // clock cycles since that `fire`, saturating
  always @(posedge clk) begin
    if (!rst_n) f_fired <= 1'b0;
    else if (fire) begin
      f_fired <= 1'b1;
      f_age   <= 18'd0;
    end else if (f_age != 18'h3FFFF) f_age <= f_age + 18'd1;
  end

  wire [17:0] f_first = {2'b00, delay};
  wire [17:0] f_last  = {2'b00, delay} + {10'd0, width};

  always @(*) begin
    if (rst_n) begin
      if (!f_fired) assert (state == IDLE && !out);
      else begin
        assert (out == ((f_age >= f_first) && (f_age <= f_last)));
        // What ties the counters to the specification, for the induction.
        assert (state != 2'd3);
        if (f_age < f_first) assert (state == DELAY && {2'b00, dcnt} == f_first - 18'd1 - f_age);
        else if (f_age <= f_last) assert (state == PULSE && {10'd0, wcnt} == f_last - f_age);
        else assert (state == IDLE);
      end
      assert (out == (state == PULSE));
    end
  end

  // The timer reads k on the k-th edge after a reset.
  reg        f_timed = 1'b0;
  reg [31:0] f_since = 32'd0;
  always @(posedge clk) begin
    if (!rst_n) f_timed <= 1'b0;
    else if (timer_reset) begin
      f_timed <= 1'b1;
      f_since <= 32'd1;
    end else f_since <= f_since + 32'd1;
  end
  always @(*) if (!f_past_valid || f_timed) assume (ena);
  always @(*) if (rst_n && f_timed) assert (timer == f_since);

  always @(posedge clk) begin
    if (rst_n) begin
      cover (f_fired && $fell(out) && (delay == 16'd5) && (width == 8'd2));
      cover (f_timed && (timer == 32'd20));
    end
  end
`endif

endmodule
