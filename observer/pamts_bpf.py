#!/usr/bin/env python3
"""Pure-Python loader for the PAMTS nfsd read collector.

Loads a pre-compiled CO-RE BPF object through libbpf via ctypes, so the machine
that RUNS this needs no compiler, no kernel headers and no clang -- only
`libbpf.so.1`, which is usually already present. That matters when the host doing
the serving is one you would rather not install a toolchain on.

The object itself is built elsewhere (see the Makefile). CO-RE makes it portable
across kernel versions, which is the entire reason for choosing it over BCC.

Requires CAP_BPF and CAP_PERFMON (or CAP_SYS_ADMIN). It does NOT require tracefs,
because attachment is by raw tracepoint name -- so it works inside a privileged
container without mounting debugfs or restarting anything.
"""
import collections
import ctypes
import ctypes.util
import os
import struct

# Must match struct pamts_event in pamts_nfsd.bpf.c exactly. Field order is
# chosen so there is no implicit padding anywhere but the explicit tail.
#
# The size is NOT a sufficient check on its own: `uid` was added into what had
# been tail padding, so the struct is 72 bytes both before and after. _check_abi()
# compares against the object's own BTF instead.
EVENT = struct.Struct("<QQQqIIIiI16sB3x")
assert EVENT.size == 72, EVENT.size

#: Matches PAMTS_UID_UNKNOWN. Not 0, because uid 0 is a real client here.
UID_UNKNOWN = 0xFFFFFFFF

KINDS = {0: "read_start", 1: "read_splice", 2: "read_vector",
         3: "read_direct", 4: "read_done", 5: "read_err",
         6: "write_start", 7: "write_done", 8: "write_err", 9: "commit_done"}
WRITE_KINDS = frozenset(("write_start", "write_done", "write_err", "commit_done"))

AF_INET, AF_INET6 = 2, 10


def _fmt_addr(af, raw):
    try:
        import socket
        if af == AF_INET:
            return socket.inet_ntop(socket.AF_INET, raw[:4])
        if af == AF_INET6:
            return socket.inet_ntop(socket.AF_INET6, raw[:16])
    except (OSError, ValueError):
        pass
    return None


class BpfError(RuntimeError):
    pass


SAMPLE_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_size_t)
PRINT_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                            ctypes.c_char_p, ctypes.c_void_p)


class Collector:
    """Attaches the collector and yields Records.

    The ring buffer callback runs inside libbpf's poll, so samples are appended
    to a list and drained by the generator rather than yielded from the callback
    (you cannot yield across a C call boundary).
    """

    #: Programs not attached by default. read_start triples the event rate and
    #: carries nothing the method event does not -- the method supplies the
    #: requested length, so EOF and coverage still work without it. One 17-hour
    #: period dropped 40.3M events, so a third fewer is worth having.
    SKIP_PROGRAMS = frozenset(("pamts_read_start",))

    #: Ceiling on events decoded but not yet consumed. Sized above the largest burst
    #: the kernel ring itself holds (~233,000 observed), so reaching it means userspace
    #: is genuinely not keeping up rather than merely absorbing a spike.
    max_pending = 500_000

    def __init__(self, obj_path, map_name="events", verbose=False,
                 poll_ms=200, record_factory=None, attach_all=False,
                 max_pending=None):
        if not os.path.exists(obj_path):
            raise BpfError(
                f"{obj_path} not found -- build it first (see observer/Makefile). "
                "The object is compiled with clang on a build host; this host "
                "only needs libbpf.")
        self.obj_path = obj_path
        self.poll_ms = poll_ms
        self.verbose = verbose
        if max_pending:
            self.max_pending = int(max_pending)
        # A deque, NOT a list. ring_buffer__poll drains everything the kernel has
        # into this via the callback, and a list drained with pop(0) is O(n) per item
        # -- one observed burst held ~233,000 events, which is ~2.7e10 element shifts
        # to drain. popleft() is O(1).
        #
        # maxlen bounds it too. The kernel ring is 128 MB and userspace was unbounded,
        # so if sessionising fell behind, memory grew until MemoryMax killed the one
        # process that must not fall over. Past the bound the OLDEST pending events are
        # dropped and counted, which is the same trade the kernel ring already makes.
        self._queue = collections.deque(maxlen=self.max_pending)
        self.queue_dropped = 0
        self._stop = False
        self._links = []
        self.attached = []
        self.lost = 0

        libname = ctypes.util.find_library("bpf") or "libbpf.so.1"
        try:
            self.lib = ctypes.CDLL(libname, use_errno=True)
        except OSError as e:
            raise BpfError(f"cannot load {libname}: {e}") from e
        self._bind()

        if not verbose:
            # libbpf is chatty on stderr; silence info but keep warnings.
            self._quiet = PRINT_FN(lambda lvl, fmt, args: 0)
            self.lib.libbpf_set_print(self._quiet)

        self._record = record_factory or self._default_record
        self.attach_all = attach_all
        self.skipped = []
        self._open_and_load()

    # -- ctypes plumbing ---------------------------------------------------
    def _bind(self):
        L = self.lib
        L.bpf_object__open_file.restype = ctypes.c_void_p
        L.bpf_object__open_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        L.bpf_object__load.restype = ctypes.c_int
        L.bpf_object__load.argtypes = [ctypes.c_void_p]
        L.bpf_object__next_program.restype = ctypes.c_void_p
        L.bpf_object__next_program.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        L.bpf_program__name.restype = ctypes.c_char_p
        L.bpf_program__name.argtypes = [ctypes.c_void_p]
        L.bpf_program__attach.restype = ctypes.c_void_p
        L.bpf_program__attach.argtypes = [ctypes.c_void_p]
        L.bpf_object__find_map_by_name.restype = ctypes.c_void_p
        L.bpf_object__find_map_by_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        L.bpf_map__fd.restype = ctypes.c_int
        L.bpf_map__fd.argtypes = [ctypes.c_void_p]
        L.bpf_map_lookup_elem.restype = ctypes.c_int
        L.bpf_map_lookup_elem.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p]
        L.ring_buffer__new.restype = ctypes.c_void_p
        L.ring_buffer__new.argtypes = [ctypes.c_int, SAMPLE_FN,
                                       ctypes.c_void_p, ctypes.c_void_p]
        L.ring_buffer__poll.restype = ctypes.c_int
        L.ring_buffer__poll.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.ring_buffer__free.restype = None
        L.ring_buffer__free.argtypes = [ctypes.c_void_p]
        L.bpf_object__close.restype = None
        L.bpf_object__close.argtypes = [ctypes.c_void_p]
        L.libbpf_set_print.restype = ctypes.c_void_p
        L.libbpf_set_print.argtypes = [PRINT_FN]
        # BTF, for the layout assertion in _check_abi(). Optional: an older
        # libbpf without these is a reason to skip the check, not to refuse.
        for name, res, args in (
                ("bpf_object__btf", ctypes.c_void_p, [ctypes.c_void_p]),
                ("btf__find_by_name_kind", ctypes.c_int,
                 [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]),
                ("btf__resolve_size", ctypes.c_long,
                 [ctypes.c_void_p, ctypes.c_uint])):
            try:
                fn = getattr(L, name)
            except AttributeError:
                continue
            fn.restype, fn.argtypes = res, args

    BTF_KIND_STRUCT = 4

    def _check_abi(self):
        """Refuse to run if the object emits a different struct than we unpack.

        `uid` was added into bytes that used to be tail padding, so sizeof did not
        change and the `size < EVENT.size` guard on the data path cannot see a
        mismatch. A stale object paired with this decoder would hand back a uid
        read from the first four bytes of the client address -- plausible-looking
        numbers, wrong every time, and nothing would ever complain.

        Compares against the object's OWN BTF, so it keeps working for whatever
        the next field turns out to be, with no version constant to remember to
        bump. Missing BTF is not an error: `-g` could be absent, or libbpf too
        old to expose it, and in both cases we are no worse off than before.
        """
        if not hasattr(self.lib, "btf__resolve_size"):
            return None
        btf = self.lib.bpf_object__btf(self.obj)
        if not btf:
            return None
        tid = self.lib.btf__find_by_name_kind(btf, b"pamts_event",
                                              self.BTF_KIND_STRUCT)
        if tid <= 0:
            return None
        size = self.lib.btf__resolve_size(btf, tid)
        if size <= 0:
            return None
        if size != EVENT.size:
            raise BpfError(
                f"{self.obj_path} emits a {size}-byte struct pamts_event but this "
                f"loader unpacks {EVENT.size} bytes. The object is out of step with "
                f"pamts_bpf.py -- rebuild it (make -C observer) and redeploy both.")
        return size

    def _open_and_load(self):
        self.obj = self.lib.bpf_object__open_file(self.obj_path.encode(), None)
        if not self.obj:
            raise BpfError(f"bpf_object__open_file failed (errno "
                           f"{ctypes.get_errno()}) for {self.obj_path}")
        # Before load, not after: BTF is readable from the opened object and needs
        # no privileges, so a mismatch fails cheaply instead of after the verifier.
        self.abi_size = self._check_abi()
        rc = self.lib.bpf_object__load(self.obj)
        if rc != 0:
            raise BpfError(
                f"bpf_object__load failed rc={rc} errno={ctypes.get_errno()}. "
                "Common causes: missing CAP_BPF/CAP_PERFMON, a kernel without "
                "BTF at /sys/kernel/btf/vmlinux, or a tracepoint this kernel "
                "does not have. Run with verbose=True for libbpf's own log.")

        prog = self.lib.bpf_object__next_program(self.obj, None)
        while prog:
            name = self.lib.bpf_program__name(prog)
            pname = name.decode() if name else "?"
            if not self.attach_all and pname in self.SKIP_PROGRAMS:
                self.skipped.append(pname)
                prog = self.lib.bpf_object__next_program(self.obj, prog)
                continue
            link = self.lib.bpf_program__attach(prog)
            if not link:
                raise BpfError(
                    f"failed to attach {name.decode() if name else '?'} "
                    f"(errno {ctypes.get_errno()})")
            self._links.append(link)
            self.attached.append(name.decode() if name else "?")
            prog = self.lib.bpf_object__next_program(self.obj, prog)
        if not self.attached:
            raise BpfError("object contains no programs")

        m = self.lib.bpf_object__find_map_by_name(self.obj, b"events")
        if not m:
            raise BpfError("no 'events' ring buffer map in the object")
        fd = self.lib.bpf_map__fd(m)
        if fd < 0:
            raise BpfError(f"bpf_map__fd returned {fd}")

        m = self.lib.bpf_object__find_map_by_name(self.obj, b"stats")
        self._stats_fd = self.lib.bpf_map__fd(m) if m else -1

        self._cb = SAMPLE_FN(self._on_sample)
        self.rb = self.lib.ring_buffer__new(fd, self._cb, None, None)
        if not self.rb:
            raise BpfError(f"ring_buffer__new failed (errno {ctypes.get_errno()})")

    ST_EMITTED, ST_DROPPED = 0, 1

    def counters(self):
        """Events the kernel side emitted and dropped.

        A full ring buffer drops silently, so without this "nothing is happening"
        and "we stopped keeping up" look identical. Userspace can stall: a path
        index rebuild once took 87.9s under pool contention.
        """
        out = {"emitted": None, "dropped": None}
        if getattr(self, "_stats_fd", -1) < 0:
            return out
        for name, idx in (("emitted", self.ST_EMITTED), ("dropped", self.ST_DROPPED)):
            k = ctypes.c_uint32(idx)
            v = ctypes.c_uint64(0)
            if self.lib.bpf_map_lookup_elem(self._stats_fd, ctypes.byref(k),
                                            ctypes.byref(v)) == 0:
                out[name] = v.value
        return out

    # -- data path ---------------------------------------------------------
    def _on_sample(self, ctx, data, size):
        if size < EVENT.size:
            self.lost += 1
            return 0
        buf = ctypes.string_at(data, EVENT.size)
        if len(self._queue) == self._queue.maxlen:
            # Count it rather than let it vanish: "we stopped keeping up" and "nothing
            # happened" must not look the same.
            self.queue_dropped += 1
        self._queue.append(self.decode(buf))
        return 0

    @staticmethod
    def decode(buf):
        (ts, ino, offset, length, dev, xid, kind, status, uid, addr, af) = \
            EVENT.unpack(buf)
        return {
            "ts": ts / 1e9,                      # ktime ns -> seconds
            "kind": KINDS.get(kind, "read_unknown"),
            "xid": xid,
            "offset": offset,
            "len": None if length < 0 else length,
            "status": status if kind in (5, 8) else None,
            "ino": ino,
            "dev": dev,
            "client": _fmt_addr(af, addr),
            # None rather than the sentinel, so consumers never have to know it.
            "uid": None if uid == UID_UNKNOWN else uid,
        }

    def _default_record(self, d):
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import pamts_observer as obs
        return obs.Record.from_json(d)

    def stop(self):
        """Ask records() to return after the current poll.

        Signal handlers call this rather than relying on KeyboardInterrupt
        propagating out of a ctypes call that is blocked in libbpf: that proved
        unreliable in practice -- SIGTERM killed the process while SIGINT left it
        running with the programs still attached.
        """
        self._stop = True

    def records(self):
        """Generator of Records. Returns when stop() is called."""
        try:
            while not self._stop:
                rc = self.lib.ring_buffer__poll(self.rb, self.poll_ms)
                if rc < 0 and rc not in (-4,):   # -EINTR is fine
                    raise BpfError(f"ring_buffer__poll returned {rc}")
                while self._queue:
                    yield self._record(self._queue.popleft())
        finally:
            self.close()

    def close(self):
        if getattr(self, "rb", None):
            self.lib.ring_buffer__free(self.rb)
            self.rb = None
        if getattr(self, "obj", None):
            self.lib.bpf_object__close(self.obj)
            self.obj = None
