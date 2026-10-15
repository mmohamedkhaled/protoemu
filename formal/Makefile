# Formal proofs with SymbiYosys. Needs sby, yosys and yices (all in OSS CAD Suite).
#
#   make            run every proof
#   make decoder    the instruction decoder
#   make sm         the state machine: delay guarantee, atomicity, FIFO safety
#   make trigger    the trigger pulse generator and cycle timer: exact timing
#   make firmware   the programs in firmware/, running on the real state machine
#   make fw_valid   every shipped program is well-formed on the real decoder

.PHONY: all decoder sm trigger firmware fw_valid gen clean

all: decoder sm trigger firmware fw_valid

# Programs with a correctness proof of their own (fw_*.sby), and the full set
# that the well-formedness proof (fw_valid.sby) covers.
FIRMWARE = uart_tx spi_master jtag swd i2c_master
ALL_FIRMWARE = uart_tx uart_rx spi_master i2c_master jtag swd ws2812 \
               latency trigger_edge sniffer glitch_on_match \
               armed_glitch_watch armed_glitch_fire

# Wrap each program around pe_sm, through the same assembler the host uses.
gen:
	mkdir -p gen
	for p in $(ALL_FIRMWARE); do python3 fw_gen.py ../firmware/$$p.pio gen/$${p}_sm.v || exit 1; done

firmware: gen
	sby -f fw_uart_tx.sby
	sby -f fw_clock_data.sby
	sby -f fw_i2c.sby

fw_valid: gen
	sby -f fw_valid.sby

decoder:
	sby -f decoder.sby

sm:
	sby -f sm.sby

trigger:
	sby -f trigger.sby

clean:
	rm -rf decoder sm sm_prove sm_cover trigger trigger_prove trigger_cover gen \
	       fw_uart_tx fw_clock_data fw_clock_data_* fw_i2c fw_valid fw_valid_*
