#!/usr/bin/env bash
# Build and test the runqlat --split patch against upstream bcc.
# Run inside the Linux VM:  bash opensource/build_and_test.sh
# Needs the packages from scripts/setup_vm.sh (incl. libssl-dev for bpftool).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BCC_COMMIT=c398a9a7a41cba623b520cda27457b92d7fcad2b   # upstream master, 2 Oct 2026
WORK=${WORK:-$HOME/work}
mkdir -p "$WORK" && cd "$WORK"
if [ ! -d bcc ]; then
	git clone https://github.com/iovisor/bcc.git
fi
cd bcc
git fetch -q origin "$BCC_COMMIT" 2>/dev/null || true
git checkout -q -f "$BCC_COMMIT"          # -f: safe to re-run (drops an earlier applied patch)
git checkout -q -B runqlat-split-cause
# bpftool has its own libbpf submodule, so this must be recursive
git submodule update --init --recursive --depth 1 src/cc/libbpf libbpf-tools/bpftool
git apply "$HERE/runqlat-split.patch"
cd libbpf-tools
make -j"$(nproc)" USE_BLAZESYM=0 runqlat

echo "== test 1: plain runqlat still works (no -S)"
sudo ./runqlat 2 1 | tail -5

echo "== test 2: -S with a CPU hog sharing CPU 0 -> preemption histogram appears"
"$HERE/../build/stressor" cpu -c 0 -n 2 -d 4 &
sudo ./runqlat -S 3 1
wait

echo "== test 3: -S -P per process"
"$HERE/../build/stressor" cpu -c 0 -n 2 -d 4 &
sudo ./runqlat -S -P 3 1 | grep -A3 stressor | head -20
wait

echo "== test 4: -S with --pidnss is rejected"
sudo ./runqlat -S --pidnss 1 1 && echo "UNEXPECTED" || echo "rejected as expected"
