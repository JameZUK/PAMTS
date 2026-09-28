# Build the CO-RE collector object.
#
# Only this build host needs clang and bpftool. The machine that RUNS the
# collector needs libbpf.so.1 and nothing else -- see pamts_bpf.py.
#
# vmlinux.h must be generated from the TARGET kernel's BTF, not the build host's:
#     make vmlinux.h BTF=/path/to/target/vmlinux.btf
# Copy it from the target with:
#     ssh target cat /sys/kernel/btf/vmlinux > vmlinux.btf
CLANG    ?= clang
BPFTOOL  ?= bpftool
BTF      ?= /sys/kernel/btf/vmlinux
ARCH     ?= x86
CFLAGS   ?= -g -O2 -Wall -Wno-unused-function

OBJ = pamts_nfsd.bpf.o

all: $(OBJ)

vmlinux.h:
	$(BPFTOOL) btf dump file $(BTF) format c > $@

$(OBJ): pamts_nfsd.bpf.c vmlinux.h
	$(CLANG) $(CFLAGS) -target bpf -D__TARGET_ARCH_$(ARCH) -I. -c $< -o $@
	@echo "built $@ -- ship this plus the Python; no compiler needed on the target"

verify: $(OBJ)
	@$(BPFTOOL) prog load $(OBJ) /sys/fs/bpf/pamts_verify 2>&1 | head -40 || true
	@rm -f /sys/fs/bpf/pamts_verify

clean:
	rm -f $(OBJ)

distclean: clean
	rm -f vmlinux.h

.PHONY: all clean distclean verify
