# Build everything into build/. Run inside the Linux VM (see docs/REPRODUCE.md).
CC      ?= gcc
CLANG   ?= clang
BPFTOOL ?= bpftool
ARCH    := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')
B       := build

CFLAGS  := -O2 -g -Wall -Wextra -Wno-unused-parameter
LIBS    := -lbpf -lelf -lz

all: $(B)/tlbench $(B)/tlat $(B)/stressor $(B)/hiccup

$(B):
	mkdir -p $(B)

$(B)/vmlinux.h: | $(B)
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(B)/tlat.bpf.o: src/tlat.bpf.c src/tlat.h $(B)/vmlinux.h
	$(CLANG) -O2 -g -target bpf -mcpu=v3 -D__TARGET_ARCH_$(ARCH) -I$(B) -Isrc -c $< -o $@

$(B)/tlat.skel.h: $(B)/tlat.bpf.o
	$(BPFTOOL) gen skeleton $< > $@

$(B)/tlat: src/tlat.c src/tlat.h $(B)/tlat.skel.h
	$(CC) $(CFLAGS) -I$(B) -Isrc $< -o $@ $(LIBS)

# -fno-omit-frame-pointer keeps the uprobe anchors easy to inspect
$(B)/tlbench: src/tlbench.c src/tlat.h | $(B)
	$(CC) $(CFLAGS) -Isrc -fno-omit-frame-pointer $< -o $@ -lpthread -lm

$(B)/stressor: src/stressor.c | $(B)
	$(CC) $(CFLAGS) $< -o $@ -lpthread

$(B)/hiccup: src/hiccup.c | $(B)
	$(CC) $(CFLAGS) $< -o $@ -lpthread

clean:
	rm -rf $(B)

.PHONY: all clean

# Submission bundles (run on the host after results/ and report/ are final)
package:
	rm -rf dist && mkdir -p dist
	cd report && zip -qr ../dist/report_latex.zip main.tex refs.bib sections figures generated
	zip -qr dist/project_code.zip Makefile README.md src scripts analysis docs opensource results -x '*.pyc' -x '*/__pycache__/*'
	cp report/main.pdf dist/TailTag_report.pdf
	cp docs/CODE_GUIDE.pdf dist/CODE_GUIDE.pdf
	@echo "dist/: report_latex.zip, project_code.zip, TailTag_report.pdf, CODE_GUIDE.pdf"

.PHONY: package
