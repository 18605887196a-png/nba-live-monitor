#!/usr/bin/env python3
"""One-off backfill: NBA history 2026-10-21..23 (Beijing dates).

Pushes are the reconciled record from the main-chat summaries; bets are
only what the user confirmed himself. Incomplete money fields stay null
(stats counts P&L over money-complete bets only) - never estimated.
Run once; guarded.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import experience as exp

DATES = {"20261020", "20261021", "20261022"}  # US dates for BJ 10-21/22/23
if any(r.get("date_us") in DATES for r in exp.load(exp.PUSHES)):
    print("history backfill already present, aborting")
    sys.exit(0)

# (date_us, game, event_id, market, pick, line, price_c, prob, kind, delivered, result, final, note)
P = [
    # ---- 2026-10-21 (BJ) ----
    ("20261020", "OKC@SAS", None, "ml", "OKC", None, 88, 0.944, "hit", True, "win",
     "OKC 116:104 SAS", "Q4 push; user declined it"),
    # ---- 2026-10-22 (BJ) ----
    ("20261021", "MIN@MIA", None, "ml", "MIN", None, 88, None, "hit", True, "win",
     "MIN 131:106 MIA", "user did not bet"),
    ("20261021", "MIN@MIA", None, "spread", "MIN", 1.5, 91, None, "hit", True, "win",
     "MIN 131:106 MIA", "user did not bet"),
    ("20261021", "CHA@BKN", None, "spread", "CHA", -1.5, 94, None, "hit", True, "win",
     "CHA 110:103 BKN", "user bet unconfirmed"),
    ("20261021", "CHA@BKN", None, "ml", "CHA", None, 95, None, "hit", True, "win",
     "CHA 110:103 BKN", "user bet unconfirmed"),
    ("20261021", "UTA@MEM", None, "ml", "MEM", None, 96, None, "hit", True, "win",
     "UTA 103:123 MEM", ""),
    ("20261021", "POR@PHX", None, "total", None, None, None, None, "false_alarm", True, "void",
     "POR 124:83 PHX", "watcher falsely reported a Q4 under as qualified; main agent corrected it publicly"),
    ("20261021", "GSW@LAL", None, "spread", "LAL", -19.5, 95, None, "hit", True, "win",
     "GSW 105:127 LAL", "user asked about LAL -10.5 @96.7c (also won); his bet on it unconfirmed, not counted"),
    ("20261021", "SAC@LAC", None, "spread", "LAC", -5.5, 95, None, "hit", True, "win",
     "SAC 96:121 LAC", ""),
    ("20261021", "SAC@LAC", None, "spread", "LAC", -9.5, 94, None, "hit", True, "win",
     "SAC 96:121 LAC", "Gate actual 94c"),
    ("20261021", "MIL@WSH", None, None, None, None, None, None, "lesson", False, None,
     "MIL 116:128 WSH", "no coverage: planner ran late, whole game missed; fix was moving planner to 22:00 the night before"),
    ("20261021", "CHI@TOR", None, None, None, None, None, None, "lesson", False, None,
     "CHI 92:135 TOR", "no coverage: planner ran late, whole game missed; fix was moving planner to 22:00 the night before"),
    # ---- 2026-10-23 (BJ) ----
    ("20261022", "CLE@PHI", None, "total", "Under", 237.5, 87, None, "hit", True, "win",
     "CLE 106:114 PHI", "Q4 push"),
    ("20261022", "DEN@OKC", None, "spread", "OKC", -8.5, 83, None, "hit", True, "win",
     "DEN 84:96 OKC", "Q4 1:43 left; user complained it was late"),
    ("20261022", "DEN@OKC", None, "ml", "OKC", None, 82, 0.945, "lesson", False, None,
     "DEN 84:96 OKC", "retrospective: at Q4 ~11 min OKC ML was already 94.5% @82c and should have been pushed then; it was not"),
]

n = 0
for dus, game, eid, mkt, pick, line, price, prob, kind, delivered, result, final, note in P:
    exp._append(exp.PUSHES, {"date_us": dus, "event_id": eid, "game": game, "market": mkt,
                             "pick": pick, "line": line, "price_c": price, "prob": prob,
                             "kind": kind, "delivered": delivered, "result": result,
                             "final": final, "note": note, "source": "backfill_history_1021_1023"})
    n += 1
print(f"history pushes backfilled: {n}")

# User's own confirmed bets from those days (money fields null where unknown).
B = [
    {"date_bj": "2026-10-21", "game": "PHI@NYK", "market": "total", "pick": "Under", "line": 231.5,
     "price_c": 93, "shares": None, "stake_gross": 4.66, "refund": None, "stake_net": None,
     "payout": None, "profit": None, "result": "win",
     "note": "settlement verified; shares/payout not recorded, never estimated",
     "source": "backfill_history_1021_1023"},
    {"date_bj": "2026-10-22", "game": "POR@PHX", "market": None, "pick": "POR", "line": None,
     "price_c": None, "shares": None, "stake_gross": None, "refund": None, "stake_net": None,
     "payout": None, "profit": None, "result": "win",
     "note": "user said he bet the Portland game; market and amount never told; counted as win on POR ML/spread",
     "source": "backfill_history_1021_1023"},
    {"date_bj": "2026-10-23", "game": "CLE@PHI", "market": "total", "pick": "Under", "line": 240.5,
     "price_c": None, "shares": None, "stake_gross": None, "refund": None, "stake_net": None,
     "payout": None, "profit": None, "result": "win",
     "note": "user's own pick; buy price and amount unknown",
     "source": "backfill_history_1021_1023"},
]
for b in B:
    exp.log_bet(b)
print(f"history bets backfilled: {len(B)}")
