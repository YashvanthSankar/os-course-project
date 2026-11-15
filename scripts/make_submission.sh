#!/usr/bin/env bash
# Build the single submission archive in the layout required by the course:
#   Team_<No>_Assignment_A1.zip -> README.md, latex_report/, code/,
#   documentation/, supplementary/
# Usage (from the repository root, after the report is compiled):
#   bash scripts/make_submission.sh <team-number>
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 1 ] || { echo "usage: $0 <team-number 1-29>"; exit 1; }
TEAM=$(printf "%02d" "$((10#$1))")
NAME="Team_${TEAM}_Assignment_A1"
OUT="dist/$NAME"
[ -f report/main.pdf ] || { echo "compile report/main.pdf first"; exit 1; }
rm -rf "$OUT" "dist/$NAME.zip"
mkdir -p "$OUT"/{latex_report,code,documentation}
mkdir -p "$OUT"/supplementary/{datasets,experimental_results,additional_figures,other_supporting_materials}

cp docs/SUBMISSION_README.md "$OUT/README.md"

# report: sources + compiled PDF
cp report/main.tex report/references.bib "$OUT/latex_report/"
cp report/main.pdf "$OUT/latex_report/report.pdf"
cp -R report/figures report/sections report/generated "$OUT/latex_report/"

# code: everything needed to build, run and analyse
cp -R src scripts analysis configs opensource "$OUT/code/"
cp Makefile requirements.txt "$OUT/code/"

# documentation
cp docs/REPRODUCE.md "$OUT/documentation/reproducibility_guide.md"
cp docs/CODE_GUIDE.pdf "$OUT/documentation/reproducibility_guide.pdf"
cp docs/LEARN.md "$OUT/documentation/background_concepts.md"

# supplementary
cp -R data/sample/. "$OUT/supplementary/datasets/"
cp results/*.csv results/*.json "$OUT/supplementary/experimental_results/"
cp results/fig_*.pdf "$OUT/supplementary/additional_figures/"
cp results/environment.txt "$OUT/supplementary/other_supporting_materials/measured_machine_environment.txt"
cp TODO.md "$OUT/supplementary/other_supporting_materials/review_and_fixes_log.md"
[ -f opensource/bcc_test_log.txt ] && cp opensource/bcc_test_log.txt "$OUT/supplementary/other_supporting_materials/"

# no caches, virtualenvs, build outputs or OS junk
find "$OUT" \( -name '__pycache__' -o -name '.venv' -o -name 'build' \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT" \( -name '.DS_Store' -o -name '*.pyc' \) -delete
find "$OUT/latex_report" \( -name '*.aux' -o -name '*.log' -o -name '*.bbl' -o -name '*.blg' -o -name '*.out' \) -delete

(cd dist && zip -qr "$NAME.zip" "$NAME")
echo "built dist/$NAME.zip ($(du -h "dist/$NAME.zip" | cut -f1))"
