# SPDX-License-Identifier: Apache-2.0
"""Merge the per-testbench cocotb result files into one JUnit file.

usage: merge_results.py <output.xml> <input.xml>...

Exits non-zero if an input is missing or any test failed, so `make` fails too.
"""

import sys
import xml.etree.ElementTree as ET


def main(out_path, in_paths):
    merged = ET.Element("testsuites", name="results")
    tests = 0
    failed = 0
    for path in in_paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            print(f"merge_results: cannot read {path}: {exc}")
            return 1
        for suite in root.iter("testsuite"):
            # The CI greps this file for "failure", so drop pytest's failures="0" counters.
            for key in ("failures", "errors"):
                suite.attrib.pop(key, None)
            merged.append(suite)
            for case in suite.iter("testcase"):
                tests += 1
                if case.find("failure") is not None or case.find("error") is not None:
                    failed += 1
    ET.ElementTree(merged).write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"merge_results: {tests} tests, {failed} failed -> {out_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
