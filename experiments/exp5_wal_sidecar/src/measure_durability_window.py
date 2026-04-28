# NOTE: Obsolete (superseded by durability_gap.py).
#
# This was an early sampler that polled INFO persistence to estimate
# the at-risk AOF window. The measurement methodology proved noisy
# inside a containerized sandbox (the page cache survives kill -9 on
# ext4, hiding everysec losses). The replacement, durability_gap.py,
# computes the worst-case loss window directly from ack timestamps,
# which is the operator-facing RPO metric anyway.
