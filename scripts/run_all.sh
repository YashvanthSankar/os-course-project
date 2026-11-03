#!/usr/bin/env bash
# One-command reproduction: build, run all experiments, analyse.
# Usage (inside the Linux VM, from the project root):  sudo bash scripts/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
make
python3 scripts/experiment.py all
python3 analysis/analyze.py --raw data/raw --out results
python3 analysis/no_signal.py --raw data/raw --res results
python3 analysis/make_tables.py --res results --out report/generated
echo "done: see results/ (figures, tables, results.json)"
