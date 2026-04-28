# NOTE: Obsolete (superseded by durability_gap.py).
#
# This file drove the original kill -9 comparison for the per-write
# fsync WAL prototype. That design was rejected (see wal_client.py).
#
# The current durability comparison lives in durability_gap.py and
# uses worst-case-window accounting — which is correct on ext4
# whether or not the kernel page cache survives kill -9.
