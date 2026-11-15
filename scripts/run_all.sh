#!/usr/bin/env bash
# One-command reproduction: build, run all experiments, analyse.
# Usage (inside the Linux VM, from the project root):  sudo bash scripts/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
make
python3 scripts/experiment.py all
python3 analysis/analyze.py --raw data/raw --out results
python3 analysis/no_os_delay.py --raw data/raw --res results
python3 analysis/robustness_checks.py --raw data/raw --res results
python3 analysis/preempt_fix_check.py --raw data/raw --res results
# report tables/macros: report/ in the repository, ../latex_report/ in the submission ZIP
GEN=report/generated
if [ ! -d report ] && [ -d ../latex_report ]; then GEN=../latex_report/generated; fi
python3 analysis/make_tables.py --res results --out "$GEN"
mkdir -p "$(dirname "$GEN")/figures" && cp results/fig_*.pdf "$(dirname "$GEN")/figures/"
echo "done: see results/ (figures, tables, results.json) and $GEN"
