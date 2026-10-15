/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Protocol emulator: two programmable state machines that bit-bang protocols
// on the eight bidirectional pins, programmed by a host over SPI.
//
//   ui_in[0]  SCK          uo_out[0]  MISO
//   ui_in[1]  CSn          uo_out[1]  state machine 0 RX FIFO has data
//   ui_in[2]  MOSI         uo_out[2]  state machine 0 TX FIFO has room
//                          uo_out[3]  state machine 1 RX FIFO has data
//                          uo_out[4]  state machine 1 TX FIFO has room
//                          uo_out[5]  trigger pulse
//   uio[7:0]  protocol pins
module tt_um_mmohamedkhaled_protoemu (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire [6:0] bus_addr;
  wire [7:0] bus_wdata, bus_rdata;
  wire       bus_we, bus_re;
  wire       miso;
  wire [1:0] rx_ready, tx_ready;
  wire       trig_out;

  pe_cfg_spi cfg_spi (
      .clk      (clk),
      .rst_n    (rst_n),
      .sck      (ui_in[0]),
      .csn      (ui_in[1]),
      .mosi     (ui_in[2]),
      .miso     (miso),
      .bus_addr (bus_addr),
      .bus_wdata(bus_wdata),
      .bus_we   (bus_we),
      .bus_re   (bus_re),
      .bus_rdata(bus_rdata)
  );

  pe_core core (
      .clk      (clk),
      .rst_n    (rst_n),
      .ena      (ena),
      .bus_addr (bus_addr),
      .bus_wdata(bus_wdata),
      .bus_we   (bus_we),
      .bus_re   (bus_re),
      .bus_rdata(bus_rdata),
      .pad_in   (uio_in),
      .pad_out  (uio_out),
      .pad_oe   (uio_oe),
      .rx_ready (rx_ready),
      .tx_ready (tx_ready),
      .trig_out (trig_out)
  );

  assign uo_out = {2'b00, trig_out, tx_ready[1], rx_ready[1], tx_ready[0], rx_ready[0], miso};

  // List all unused inputs to prevent warnings
  wire _unused = &{ui_in[7:3], 1'b0};

endmodule
