// SPDX-License-Identifier: GPL-2.0
/* PAMTS nfsd read collector.
 *
 * Emits one record per NFS read RPC: timestamp, inode, device, offset, length,
 * the method nfsd chose, and the client address. Userspace does everything else
 * -- sessionising, classification, path resolution -- so this stays small.
 *
 * Attachment is by RAW TRACEPOINT NAME, which matters for two reasons:
 *   - it needs no tracefs/debugfs, so it works inside a privileged container
 *     without mounting anything or restarting the host
 *   - the kernel resolves tracepoint names across modules, and nfsd IS a module,
 *     so its tracepoints are absent from vmlinux BTF
 *
 * TP_PROTO, verified against the target kernel's nfsd module BTF rather than
 * assumed:
 *   read_start/done/splice/vector/direct:
 *       (struct svc_rqst *, struct svc_fh *, u64 offset, u32 len)
 *   read_err:
 *       (struct svc_rqst *, struct svc_fh *, loff_t offset, int status)
 * As raw tracepoint args that is ctx->args[0..3] -- BTF encodes a leading void*
 * for the tracepoint typedef, but raw_tp args do not include it.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

char LICENSE[] SEC("license") = "GPL";

/* struct svc_fh is defined by the nfsd module, so it is NOT in vmlinux.h.
 * Declare only the field we need: CO-RE resolves its real offset at load time
 * from /sys/kernel/btf/nfsd. The ___pamts suffix is a CO-RE "flavor" -- it is
 * stripped when matching, so this still relocates against the real svc_fh.
 */
struct svc_fh___pamts {
	struct dentry *fh_dentry;
} __attribute__((preserve_access_index));

enum pamts_kind {
	PAMTS_START  = 0,
	PAMTS_SPLICE = 1,
	PAMTS_VECTOR = 2,
	PAMTS_DIRECT = 3,
	PAMTS_DONE   = 4,
	PAMTS_ERR    = 5,
};

/* Layout is fixed and must match EVENT in pamts_bpf.py: "<QQQqIIIi16sB7x".
 * Field order avoids all implicit padding; the tail padding is explicit.
 */
struct pamts_event {
	__u64 ts;
	__u64 ino;
	__u64 offset;
	__s64 len;
	__u32 dev;
	__u32 xid;
	__u32 kind;
	__s32 status;
	__u8  addr[16];
	__u8  af;
	__u8  _pad[7];
};

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 24);		/* 16 MB */
} events SEC(".maps");

/* Observed rate on a live server was ~26 events/s, so no in-kernel filtering is
 * justified yet -- userspace drops what it does not care about. If volume ever
 * matters, filter on dev here rather than widening this program's job.
 */
static __always_inline int
emit(struct svc_rqst *rqstp, struct svc_fh___pamts *fhp,
     __u64 offset, __s64 len, __u32 kind, __s32 status)
{
	struct pamts_event *e;
	struct super_block *sb = NULL;
	struct dentry *dent = NULL;
	struct inode *inode = NULL;
	unsigned long ino = 0;
	__u8 raw[28] = {};
	__u16 fam = 0;
	__be32 xid = 0;

	e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e)
		return 0;			/* buffer full: drop, never block */

	__builtin_memset(e, 0, sizeof(*e));
	e->ts     = bpf_ktime_get_ns();
	e->kind   = kind;
	e->offset = offset;
	e->len    = len;
	e->status = status;

	if (fhp) {
		BPF_CORE_READ_INTO(&dent, fhp, fh_dentry);
		if (dent) {
			BPF_CORE_READ_INTO(&inode, dent, d_inode);
			if (inode) {
				BPF_CORE_READ_INTO(&ino, inode, i_ino);
				e->ino = ino;
				BPF_CORE_READ_INTO(&sb, inode, i_sb);
				if (sb)
					BPF_CORE_READ_INTO(&e->dev, sb, s_dev);
			}
		}
	}

	if (rqstp) {
		/* rq_xid is __be32; the kernel's own tracepoints byte-swap it
		 * before printing, so do the same or captures disagree.
		 */
		BPF_CORE_READ_INTO(&xid, rqstp, rq_xid);
		e->xid = bpf_ntohl(xid);

		/* rq_addr is a __kernel_sockaddr_storage whose first member is
		 * an anonymous union, which is awkward to walk with CO-RE. Read
		 * it as bytes instead: family first, then the address at +4
		 * (sockaddr_in) or +8 (sockaddr_in6).
		 */
		if (bpf_core_read(raw, sizeof(raw), &rqstp->rq_addr) == 0) {
			fam = *(__u16 *)raw;
			e->af = (__u8)fam;
			if (fam == 2)			/* AF_INET */
				__builtin_memcpy(e->addr, raw + 4, 4);
			else if (fam == 10)		/* AF_INET6 */
				__builtin_memcpy(e->addr, raw + 8, 16);
		}
	}

	bpf_ringbuf_submit(e, 0);
	return 0;
}

#define READ_TP(fn, tp, kind)                                                  \
	SEC("raw_tp/" tp)                                                      \
	int fn(struct bpf_raw_tracepoint_args *ctx)                            \
	{                                                                      \
		return emit((struct svc_rqst *)ctx->args[0],                    \
			    (struct svc_fh___pamts *)ctx->args[1],             \
			    (__u64)ctx->args[2],                               \
			    (__s64)(__u32)ctx->args[3], kind, 0);              \
	}

READ_TP(pamts_read_start,  "nfsd_read_start",  PAMTS_START)
READ_TP(pamts_read_splice, "nfsd_read_splice", PAMTS_SPLICE)
READ_TP(pamts_read_vector, "nfsd_read_vector", PAMTS_VECTOR)
READ_TP(pamts_read_direct, "nfsd_read_direct", PAMTS_DIRECT)
READ_TP(pamts_read_done,   "nfsd_read_done",   PAMTS_DONE)

/* read_err differs: loff_t offset and an int status, not a length. */
SEC("raw_tp/nfsd_read_err")
int pamts_read_err(struct bpf_raw_tracepoint_args *ctx)
{
	return emit((struct svc_rqst *)ctx->args[0],
		    (struct svc_fh___pamts *)ctx->args[1],
		    (__u64)ctx->args[2], -1, PAMTS_ERR,
		    (__s32)ctx->args[3]);
}
