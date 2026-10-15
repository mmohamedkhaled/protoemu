#!/bin/bash
# Synthesise the design with Yosys for the IHP SG13CMOS5L cell library and
# print the mapped cell count, cell area and an estimated maximum frequency.
#
# usage: tools/area.sh [path/to/sg13cmos5l_stdcell_typ_1p20V_25C.lib]
#
# This is a quick estimate. The real numbers come from the `gds` workflow:
# synthesis here has no routing parasitics and does not resize high-fanout
# nets, so the frequency is pessimistic on those and the area has no fill.
set -e
cd "$(dirname "$0")/.."

LIB=${1:-${PE_LIBERTY:-$HOME/protoemu-tools/pdk/sg13cmos5l_stdcell_typ_1p20V_25C.lib}}
OUT=${PE_SYNTH_DIR:-/tmp/protoemu-synth}
mkdir -p "$OUT"

yosys -q -l "$OUT/yosys.log" -p "
  read_verilog src/project.v src/pe_*.v
  synth -top tt_um_mmohamedkhaled_protoemu -flatten
  dfflibmap -liberty $LIB
  abc -liberty $LIB
  opt_clean
  tee -o $OUT/stat.txt stat -liberty $LIB
  write_verilog -noattr $OUT/netlist.v
  write_verilog -noattr -simple-lhs $OUT/netlist_sta.v
"

python3 - "$OUT/stat.txt" <<'EOF'
import re, sys

text = open(sys.argv[1]).read()
# Both layouts of `stat` are accepted: older Yosys prints "name count", newer "count area name".
cells = int(max(re.findall(r"(?:Number of cells:\s+(\d+)|^\s*(\d+)\s+[\d.eE+]+\s+cells$)", text, re.M)[-1]))
area = float(re.search(r"Chip area for (?:top )?module.*?:\s*([\d.]+)", text).group(1))
flops = sum(int(a or b) for a, b in re.findall(
    r"^\s*(?:\S+_s?df\w*\s+(\d+)|(\d+)\s+[\d.eE+]+\s+\S+_s?df\w*)\s*$", text, re.M))

TILES = 24                    # 6x4
CELL_BUDGET = 24000           # about 1K cells per tile
TILE_UM2 = 167.0 * 108.0      # one tile per the template; the competition post says about 200 x 150
budget_um2 = TILES * TILE_UM2

print(f"mapped cells : {cells} of about {CELL_BUDGET} ({100 * cells / CELL_BUDGET:.0f}%)")
print(f"flip-flops   : {flops}")
print(f"cell area    : {area:.0f} um^2 = {100 * area / budget_um2:.0f}% of {TILES} tiles ({budget_um2:.0f} um^2)")
if cells > 0.75 * CELL_BUDGET or area > 0.75 * budget_um2:
    print("WARNING: over 75% of the budget")
EOF

# Estimated maximum frequency, if OpenSTA is installed. Finds the longest
# register-to-register setup path at the typical corner.
if command -v sta >/dev/null 2>&1; then
  cat > "$OUT/fmax.tcl" <<TCL
read_liberty $LIB
read_verilog $OUT/netlist_sta.v
link_design tt_um_mmohamedkhaled_protoemu
create_clock -name clk -period 100 [get_ports clk]
set_input_delay  -clock clk 0 [remove_from_collection [all_inputs] [get_ports clk]]
set_output_delay -clock clk 0 [all_outputs]
set wns [sta::worst_slack -max]
puts "FMAX critical_path_ns [format %.2f [expr 100.0 - \$wns]] fmax_MHz [format %.1f [expr 1000.0/(100.0 - \$wns)]]"
TCL
  sta -no_init -exit "$OUT/fmax.tcl" 2>/dev/null | grep "^FMAX" | \
    sed -E 's/FMAX critical_path_ns ([0-9.]+) fmax_MHz ([0-9.]+)/critical path : \1 ns -> Fmax about \2 MHz (typical, pre-layout)/'
else
  echo "Fmax         : install OpenSTA (sta) for a timing estimate"
fi
