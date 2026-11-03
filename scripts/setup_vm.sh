#!/usr/bin/env bash
# Install the toolchain inside the Ubuntu 24.04 VM (run as a sudo-capable user).
set -euo pipefail
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
	build-essential clang llvm libbpf-dev libelf-dev zlib1g-dev pkg-config \
	"linux-tools-$(uname -r)" linux-tools-common \
	python3-numpy python3-pandas python3-sklearn python3-matplotlib python3-scipy
echo "toolchain installed"
clang --version | head -1
bpftool version | head -1
