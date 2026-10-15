/*
 * Copyright (c) 2026 Mohamed Khaled
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// SPI-slave configuration port: turns SPI frames from the host into accesses
// on the byte-wide register bus.
//
// SPI mode 0, most significant bit first. A frame is one command byte, then
// any number of data bytes, with the address incrementing after each one:
//
//   command  = {write, address[6:0]}
//   write    : MOSI carries the data bytes
//   read     : MISO carries the data bytes
//
// The SPI pins are sampled with `clk`, so SCK must be at most clk / 8.
module pe_cfg_spi (
    input  wire       clk,
    input  wire       rst_n,

    input  wire       sck,
    input  wire       csn,
    input  wire       mosi,
    output wire       miso,

    output wire [6:0] bus_addr,
    output wire [7:0] bus_wdata,
    output wire       bus_we,
    output wire       bus_re,     // the byte at bus_addr has been shifted out
    input  wire [7:0] bus_rdata
);

  reg [1:0] sck_sync, csn_sync, mosi_sync;
  reg       sck_prev;

  always @(posedge clk) begin
    if (!rst_n) begin
      sck_sync  <= 2'b00;
      csn_sync  <= 2'b11;
      mosi_sync <= 2'b00;
      sck_prev  <= 1'b0;
    end else begin
      sck_sync  <= {sck_sync[0], sck};
      csn_sync  <= {csn_sync[0], csn};
      mosi_sync <= {mosi_sync[0], mosi};
      sck_prev  <= sck_sync[1];
    end
  end

  wire active = !csn_sync[1];
  wire rise   = active && sck_sync[1] && !sck_prev;
  wire fall   = active && !sck_sync[1] && sck_prev;

  reg [2:0] bitcnt;
  reg       have_cmd;
  reg       is_write;
  reg [6:0] addr;
  reg [6:0] sh_in;
  reg [7:0] sh_out;

  wire [7:0] byte_in   = {sh_in, mosi_sync[1]};
  wire       byte_done = rise && (bitcnt == 3'd7);

  assign bus_addr  = addr;
  assign bus_wdata = byte_in;
  assign bus_we    = byte_done && have_cmd && is_write;
  assign bus_re    = byte_done && have_cmd && !is_write;
  assign miso      = sh_out[7];

  always @(posedge clk) begin
    if (!rst_n || !active) begin
      bitcnt   <= 3'd0;
      have_cmd <= 1'b0;
      is_write <= 1'b0;
      addr     <= 7'd0;
      sh_in    <= 7'd0;
      sh_out   <= 8'd0;
    end else begin
      if (rise) begin
        sh_in  <= byte_in[6:0];
        bitcnt <= bitcnt + 3'd1;
        if (byte_done) begin
          if (!have_cmd) begin
            have_cmd <= 1'b1;
            is_write <= byte_in[7];
            addr     <= byte_in[6:0];
          end else begin
            addr <= addr + 7'd1;
          end
        end
      end
      if (fall) begin
        // At a byte boundary of a read, load the next byte; otherwise shift.
        if ((bitcnt == 3'd0) && have_cmd && !is_write) sh_out <= bus_rdata;
        else sh_out <= {sh_out[6:0], 1'b0};
      end
    end
  end

endmodule
