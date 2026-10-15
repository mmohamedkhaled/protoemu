# Makefile
# Runs every cocotb testbench: one per RTL block, then the whole chip.
# The per-testbench rules are in cocotb.mk.
#
#   make                 run everything, merge the results into results.xml
#   make clkdiv          run one block's unit test
#   make asm             run the assembler's tests
#   make top             run the whole-chip tests
#   make GATES=yes       gate-level simulation of the whole chip only

UNITS = clkdiv shift fifo decoder sm trigger imem core cfg_spi

ifeq ($(GATES),yes)
RUNS = top
else
RUNS = asm $(UNITS) top
endif

PYTHON ?= python3

.PHONY: all clean asm top $(UNITS)

all: $(RUNS)
	$(PYTHON) merge_results.py results.xml $(patsubst %,results_%.xml,$(RUNS))

$(UNITS):
	rm -f results_$@.xml
	$(MAKE) -f cocotb.mk UNIT=$@

asm:
	$(PYTHON) -m pytest -q ../tools --junitxml=results_asm.xml

top:
	rm -f results_top.xml
	$(MAKE) -f cocotb.mk

clean:
	rm -rf sim_build __pycache__ models/__pycache__ ../tools/__pycache__ ../.pytest_cache results.xml results_*.xml tb.fst
