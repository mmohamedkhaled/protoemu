# cocotb.mk
# One cocotb run. With UNIT=<block> the toplevel is src/pe_<block>.v and the
# tests are test_<block>.py; without it the toplevel is tb.v (the whole chip).
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v \
                  pe_clkdiv.v \
                  pe_shift.v \
                  pe_fifo.v \
                  pe_decoder.v \
                  pe_sm.v \
                  pe_imem.v \
                  pe_smslot.v \
                  pe_core.v \
                  pe_cfg_spi.v \
                  pe_trigger.v

# The assembler is shared with the tests.
export PYTHONPATH := $(PWD)/../tools:$(PWD):$(PYTHONPATH)

ifdef UNIT

# Unit test of one RTL block:
SIM_BUILD            = sim_build/$(UNIT)
VERILOG_SOURCES     += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
TOPLEVEL             = pe_$(UNIT)
COCOTB_TEST_MODULES  = test_$(UNIT)
export COCOTB_RESULTS_FILE = results_$(UNIT).xml

else

export COCOTB_RESULTS_FILE = results_top.xml

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v
# The stdcell models instantiate UDP primitives (ihp_mux*, ihp_dff*) defined here.
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
