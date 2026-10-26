#!/usr/bin/env python3
"""policy_config.py - single place for v1 thresholds and versions.

These values are INITIAL, EXPERIMENTAL defaults, not backtested optima.
They only ever trigger a REVIEW note or a risk update; they never change
the raw model probability, never create a push on their own, and never
block a push in bypass stage (see decision.py).

- POLICY_VERSION: bump when any threshold or decision rule changes.
- MODEL_VERSION: the fast_check model as of 2026-10-25 (WP normal model,
  totals pace projection). No manual +/- points are ever added to it.
- CALIBRATED_PROBABILITY is always None in v1: the raw model is
  explicitly uncalibrated, per the 2026-10-25 architecture document.
"""

POLICY_VERSION = "v1-20261026-risk-priority"
MODEL_VERSION = "fast_check-20261025-wp-pace"

# Risk-update priority (user 2026-10-26): risk is the LOWEST priority,
# below main pushes (hit/leak) and fixed nodes (Q3-end, Q4 6:00).
# A REVIEW/INVALIDATED risk update is SENT only when at least one of:
#  - state is INVALIDATED (any period), or
#  - the game is in Q4/OT late: period >= 4 and remaining_min <= 5.0, or
#  - the candidate's raw probability fell below its push threshold
#    (hit < 0.95, leak/leak_plus < 0.90) -- the original push no longer
#    qualifies, so the change is a real decision change.
# Otherwise (e.g. early Q3, price repricing while prob/cutoff still hold)
# the risk is LOGGED ONLY: written to risk_updates.jsonl and sender.log
# as RISK held, never tg-sent, never SENT_RISK_MSG, and never marked in
# risk_alerts, so it can still fire later if the gate opens (Q4 late).
RISK_SEND_LATE_REMAINING_MIN = 5.0
RISK_SEND_PROB_HIT = 0.95
RISK_SEND_PROB_LEAK = 0.90

# Game-process windows (game minutes, not wall minutes).
GAME_WINDOWS_MIN = (2, 5)
# A window is "covered" when we have a snapshot at or before its start.
# With no history at all the window is unknown, never zero.
WINDOW_MIN_COVERAGE = 1.0  # snapshot must reach the window start for full coverage

# Descriptive risk tags only (never prob adjustments):
# - lead narrowing of at least this many points in the 5-min window,
#   against the candidate's direction, is a REVIEW reason.
LEAD_NARROW_POINTS_5M = 10
# - blowout tail: absolute lead at least this in Q4/OT.
BLOWOUT_LEAD = 20
# - close late game: absolute lead at most this with this much time left.
CLOSE_LATE_LEAD = 5
CLOSE_LATE_REMAINING_MIN = 3.0

# Market windows (wall seconds, with actual delta always reported).
MARKET_WINDOWS_SEC = (30, 120)
# Price move in cents over the 120s window, with the score unchanged,
# is a REVIEW reason (initial value, not proven). A move with a matching
# score change is normal repricing, not an anomaly.
MARKET_ANOMALY_MOVE_CENTS_120S = 10
# Bid/ask spread widening in cents over the window.
SPREAD_WIDEN_CENTS = 10

# Quote freshness: gamma/ESPN give no source timestamp, so source age is
# always unknown. Received-at age above this is a REVIEW reason, because
# the snapshot may be stale even though we cannot prove its source age.
QUOTE_MAX_RECEIVED_AGE_SEC = 120

# Cost: no target amount and no channel fee are configured by default,
# so cost is unknown, never zero. Set these explicitly to enable cost.
TARGET_USD_DEFAULT = None
FEE_PER_SHARE_DEFAULT = None
