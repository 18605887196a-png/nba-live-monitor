#!/usr/bin/env python3
"""One-off backfill: 2026-10-24 (Beijing) pushes + the user's own bets.

Push rows are the reconciled record of what was actually pushed that day
(main-chat reconciliation, 2026-10-24). Bets are from the user's Gate
history screenshots sent 2026-10-24 13:15 CST. Run once; guarded.
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import experience as exp

if any("20261023" == r.get("date_us") for r in exp.load(exp.PUSHES)):
    print("backfill already present, aborting")
    sys.exit(0)

D = "20261023"  # US date of the games

# (game, event_id, market, pick, line, price_c, prob, kind, delivered, result, final, note)
P = [
    # BOS@NYK 107:103, total 210
    ("BOS@NYK", None, "total", "Under", 223.5, 94, None, "hit", True, "win", "BOS 107:103 NYK", ""),
    ("BOS@NYK", None, "ml", "BOS", None, 85, None, "hit", True, "win", "BOS 107:103 NYK", "Q4"),
    ("BOS@NYK", None, "spread", "BOS", -2.5, 81, None, "hit", True, "win", "BOS 107:103 NYK", ""),
    # ATL@CHA 128:81, total 209
    ("ATL@CHA", None, "ml", "ATL", None, 99.9, None, "hit", True, "win", "ATL 128:81 CHA", ""),
    ("ATL@CHA", None, "spread", "ATL", -2.5, 98.1, None, "hit", True, "win", "ATL 128:81 CHA", ""),
    ("ATL@CHA", None, "total", "Under", 227.5, 92.8, None, "hit", True, "win", "ATL 128:81 CHA", ""),
    # DAL@IND 121:105, total 226
    ("DAL@IND", None, "ml", "DAL", None, 99.9, None, "hit", True, "win", "DAL 121:105 IND", ""),
    ("DAL@IND", None, "spread", "DAL", 6.5, 99, None, "hit", True, "win", "DAL 121:105 IND", ""),
    # MIN@ORL 115:93, total 208
    ("MIN@ORL", None, "ml", "MIN", None, 99, None, "hit", True, "win", "MIN 115:93 ORL", ""),
    ("MIN@ORL", None, "spread", "MIN", -1.5, 99.8, None, "hit", True, "win", "MIN 115:93 ORL", ""),
    ("MIN@ORL", None, "total", "Under", 234.5, 99, None, "hit", True, "win", "MIN 115:93 ORL", ""),
    ("MIN@ORL", None, "spread", "MIN", -12.5, 86, None, "hit", True, "win", "MIN 115:93 ORL", "supplement"),
    ("MIN@ORL", None, "total", "Under", 217.5, 93, None, "hit", True, "win", "MIN 115:93 ORL", "supplement"),
    # TOR@WAS 112:102, total 214
    ("TOR@WAS", None, "total", "Under", 235.5, 72, 0.958, "hit", True, "win", "TOR 112:102 WAS", "pushed 08:52"),
    # DET@MIA 128:111, total 239
    ("DET@MIA", None, "ml", "DET", None, 99.3, None, "hit", True, "win", "DET 128:111 MIA", ""),
    ("DET@MIA", None, "spread", "DET", -5.5, 97, None, "hit", True, "win", "DET 128:111 MIA", "later 99c"),
    ("DET@MIA", None, "spread", "DET", -11.5, 81, 0.96, "hit", True, "win", "DET 128:111 MIA", ""),
    ("DET@MIA", None, "total", "Under", 249.5, 97, None, "hit", True, "win", "DET 128:111 MIA", "model ~89% vs market ~95%, noted"),
    # NOP@UTA 119:120, total 239
    ("NOP@UTA", "401909849", "total", "Under", 256.5, None, None, "hit", True, "win", "NOP 119:120 UTA", ""),
    ("NOP@UTA", "401909849", "total", "Under", 253.5, None, None, "hit", True, "win", "NOP 119:120 UTA", ""),
    ("NOP@UTA", "401909849", "total", "Under", 250.5, None, None, "hit", True, "win", "NOP 119:120 UTA", ""),
    ("NOP@UTA", "401909849", "total", "Over", 218.5, None, None, "hit", True, "win", "NOP 119:120 UTA", ""),
    ("NOP@UTA", "401909849", "ml", "UTA", None, 85, None, "hit", True, "win", "NOP 119:120 UTA", "Q4 after UTA took lead"),
    ("NOP@UTA", "401909849", "spread", "UTA", -1.5, 83, None, "attach", True, "loss", "NOP 119:120 UTA", "won by 1, did not cover"),
    # HOU@SAS 114:110, total 224
    ("HOU@SAS", "401909096", "spread", "HOU", 5.5, 83, 0.999, "hit", True, "win", "HOU 114:110 SAS", "sent by launch task 12:07"),
    ("HOU@SAS", "401909096", "ml", "HOU", None, 70, None, "hit", True, "win", "HOU 114:110 SAS", "pushed 12:13"),
    ("HOU@SAS", "401909096", "total", "Under", 239.5, 89, None, "leak", True, "win", "HOU 114:110 SAS", "inversion leak, cheaper than 236.5"),
    ("HOU@SAS", "401909096", "total", "Under", 236.5, None, None, "hit", True, "win", "HOU 114:110 SAS", ""),
    ("HOU@SAS", "401909096", "total", "Over", 211.5, 96, None, "leak", True, "win", "HOU 114:110 SAS", "leak pushed 11:53"),
    ("HOU@SAS", "401909096", "total", "Over", 214.5, None, None, "hit", True, "win", "HOU 114:110 SAS", ""),
    ("HOU@SAS", "401909096", "total", None, None, None, None, "false_alarm", True, "void", "HOU 114:110 SAS", "two early-Q1 totals alerts from stale clock, voided by watermark"),
    # LAC@LAL 117:125, total 242
    ("LAC@LAL", "401909851", "ml", "LAL", None, 95, None, "hit", True, "win", "LAC 117:125 LAL", "pushed 95-99c"),
    ("LAC@LAL", "401909851", "total", "Over", 222.5, None, None, "hit", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "total", "Over", 224.5, None, None, "hit", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "total", "Over", 236.5, None, None, "hit", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "spread", "LAL", -1.5, None, None, "attach", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "spread", "LAL", -2.5, None, None, "attach", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "spread", "LAL", -3.5, None, None, "attach", True, "win", "LAC 117:125 LAL", ""),
    ("LAC@LAL", "401909851", "spread", "LAL", -6.5, 88, None, "leak", True, "win", "LAC 117:125 LAL", "leak pushed 12:13; user called it not-that-safe, won by 8"),
    # POR@SAC 128:120, total 248
    ("POR@SAC", "401909852", "ml", "POR", None, 91, None, "hit", True, "win", "POR 128:120 SAC", "pushed 91-94c"),
    ("POR@SAC", "401909852", "total", "Over", 218.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 221.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 224.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 233.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 234.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 237.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "total", "Over", 240.5, 99, None, "hit", True, "win", "POR 128:120 SAC", "fixed-price line"),
    ("POR@SAC", "401909852", "spread", "POR", -3.5, 86, None, "leak", True, "win", "POR 128:120 SAC", ""),
    ("POR@SAC", "401909852", "spread", "POR", -2.5, 87, None, "leak", True, "win", "POR 128:120 SAC", "Q4 0:43 left"),
    ("POR@SAC", "401909852", "total", "Over", 232.5, 52, 0.96, "missed", False, "win", "POR 128:120 SAC", "alert generated 10:28, watchers died, never delivered"),
    ("POR@SAC", "401909852", "ml", "POR", None, 90, 0.99, "missed", False, "win", "POR 128:120 SAC", "alert generated 11:57, never delivered"),
    ("POR@SAC", "401909852", "spread", "POR", -6.5, 73, 0.92, "missed", False, "win", "POR 128:120 SAC", "alert generated 11:57, never delivered"),
    # MEM@GSW 111:108, total 219
    ("MEM@GSW", "401909850", "total", "Over", 211.5, 99, None, "hit", True, "win", "MEM 111:108 GSW", "fixed-price line"),
    ("MEM@GSW", "401909850", "total", "Over", 213.5, 99, None, "hit", True, "win", "MEM 111:108 GSW", "fixed-price line"),
    ("MEM@GSW", "401909850", "total", "Over", 215.5, 99, None, "hit", True, "win", "MEM 111:108 GSW", "fixed-price line"),
    ("MEM@GSW", "401909850", "total", "Over", 217.5, 99, None, "hit", True, "win", "MEM 111:108 GSW", "won by 1.5 pts"),
    ("MEM@GSW", "401909850", "total", "Under", 249.5, None, None, "hit", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "total", "Under", 246.5, None, None, "hit", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "total", "Under", 243.5, 97, None, "hit", True, "win", "MEM 111:108 GSW", "the single pick given 12:30"),
    ("MEM@GSW", "401909850", "total", "Under", 240.5, 83, None, "leak", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "total", "Under", 238.5, None, None, "hit", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "total", "Under", 237.5, None, None, "hit", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "total", "Under", 235.5, 71, None, "leak", True, "win", "MEM 111:108 GSW", ""),
    ("MEM@GSW", "401909850", "ml", "GSW", None, 74, 0.90, "hit", True, "loss", "MEM 111:108 GSW", "pushed 12:22; GSW blew an 11-pt Q4 lead, 18 pts in Q4 - the day's one bad formal push"),
    ("MEM@GSW", "401909850", "spread", "GSW", -4.5, None, None, "attach", True, "loss", "MEM 111:108 GSW", "attached mention only, never a formal push"),
]

n = 0
for game, eid, mkt, pick, line, price, prob, kind, delivered, result, final, note in P:
    rec = {"date_us": D, "event_id": eid, "game": game, "market": mkt, "pick": pick,
           "line": line, "price_c": price, "prob": prob, "kind": kind,
           "delivered": delivered, "result": result, "final": final, "note": note,
           "source": "backfill_20261024"}
    exp._append(exp.PUSHES, rec)
    n += 1
# GSW moneyline was pushed twice (74c at 12:22 and 97c fixed-price at ~12:39); both lost.
# Same dedup key as the 74c record, so it is appended directly on purpose.
exp._append(exp.PUSHES, {"date_us": D, "event_id": "401909850", "game": "MEM@GSW",
                         "market": "ml", "pick": "GSW", "line": None, "price_c": 97,
                         "prob": 0.99, "kind": "hit", "delivered": True, "result": "loss",
                         "final": "MEM 111:108 GSW", "note": "second push at fixed price ~12:39",
                         "source": "backfill_20261024"})
n += 1
print(f"pushes backfilled: {n}")

# (game, market, pick, line, price_c, shares, stake_gross, refund, payout, buy_ts)
B = [
    ("DAL@IND", "ml", "DAL", None, 97, 7.7, 7.99, 0.52, 7.70, "2026-10-24 08:44"),
    ("MIN@ORL", "ml", "MIN", None, 98, 4.74, 4.99, 0.32, 4.74, "2026-10-24 09:05"),
    ("TOR@WAS", "total", "Under", 235.5, 95, 4.91, 4.99, 0.32, 4.91, "2026-10-24 09:13"),
    ("NOP@UTA", "total", "Under", 256.5, 92, 5.07, 5.00, 0.33, 5.07, "2026-10-24 11:57"),
    ("HOU@SAS", "total", "Under", 239.5, 90, 5.18, 4.99, 0.32, 5.18, "2026-10-24 12:09"),
    ("MEM@GSW", "total", "Under", 249.5, 98, 4.76, 4.99, 0.32, 4.76, "2026-10-24 12:31"),
]
for game, mkt, pick, line, price, shares, gross, refund, payout, bts in B:
    exp.log_bet({"date_bj": "2026-10-24", "game": game, "market": mkt, "pick": pick,
                 "line": line, "price_c": price, "shares": shares,
                 "stake_gross": gross, "refund": refund, "stake_net": round(gross - refund, 2),
                 "payout": payout, "profit": round(payout - (gross - refund), 2),
                 "buy_ts": bts, "result": "win", "source": "user screenshots 2026-10-24 13:15"})
print(f"bets backfilled: {len(B)}")
