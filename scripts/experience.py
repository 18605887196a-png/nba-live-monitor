#!/usr/bin/env python3
"""experience.py - NBA live-monitor experience bank.

Stores, as JSONL under ../experience/ next to this skill:
  pushes.jsonl - every qualified push (fast_check HIT/LEAK, monitor TRIGGER,
      plus manual backfills), with model probability and price at push time,
      settled against the final score.
  bets.jsonl   - the user's own bets, with stake / Gate refund / payout,
      so his real P&L lives in the same bank.

Writers call log_push() / log_bet(). log_push dedups on
(date_us, event_id, market, pick, line): the first sighting of a line is
the record; re-pushes of the same line never overwrite it.

CLI:
  python3 experience.py settle   # fill results for unsettled pushes (ESPN finals)
  python3 experience.py stats    # calibration: model prob vs actual hit rate,
                                 # by kind / market / prob bucket; user bet P&L
"""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "experience")
PUSHES = os.path.join(BASE, "pushes.jsonl")
BETS = os.path.join(BASE, "bets.jsonl")


def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _append(path, rec):
    os.makedirs(BASE, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _key(r):
    return (r.get("date_us"), r.get("event_id"), r.get("market"), r.get("pick"), r.get("line"))


def log_push(rec):
    rec = dict(rec)
    rec.setdefault("ts", now_cst())
    rec.setdefault("result", None)
    if _key(rec) in {_key(r) for r in load(PUSHES)}:
        return False
    _append(PUSHES, rec)
    return True


def log_bet(rec):
    rec = dict(rec)
    rec.setdefault("ts", now_cst())
    rec.setdefault("result", None)
    _append(BETS, rec)
    return True


# ---------------- settling ----------------

def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _final_for_event(event_id):
    """ESPN summary -> {abbr: score, '_home': abbr, '_away': abbr, '_names': {...}} or None."""
    try:
        d = _get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}")
        comp = d["header"]["competitions"][0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            return None
        out = {"_names": {}}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            ab = t.get("abbreviation")
            if not ab:
                continue
            out[ab] = int(float(c.get("score") or 0))
            out["_names"][ab] = t.get("displayName") or ab
            out["_home" if c.get("homeAway") == "home" else "_away"] = ab
        return out if "_home" in out and "_away" in out else None
    except Exception:
        return None


def _pick_side(rec, fin):
    pick = str(rec.get("pick") or "")
    for side in ("home", "away"):
        ab = fin[f"_{side}"]
        name = fin["_names"].get(ab, "")
        if pick == ab or pick == name or (pick and name and pick.split()[-1] == name.split()[-1]):
            return side
    return None


def _judge(rec, fin):
    hs, as_ = fin[fin["_home"]], fin[fin["_away"]]
    total = hs + as_
    mkt = rec.get("market")
    if mkt == "total":
        line = rec.get("line")
        if line is None:
            return None
        if total == line:
            return "push"
        return "win" if (rec.get("pick") == "Over") == (total > line) else "loss"
    side = _pick_side(rec, fin)
    if side is None:
        return None
    mine, opp = (hs, as_) if side == "home" else (as_, hs)
    if mkt == "ml":
        return "win" if mine > opp else "loss"
    if mkt == "spread":
        line = rec.get("line")
        if line is None:
            return None
        adj = (mine - opp) + line
        return "win" if adj > 0 else ("push" if adj == 0 else "loss")
    return None


def settle():
    pushes = load(PUSHES)
    finals, changed = {}, 0
    for r in pushes:
        if r.get("result") or not r.get("event_id"):
            continue
        eid = str(r["event_id"])
        if eid not in finals:
            finals[eid] = _final_for_event(eid)
        fin = finals[eid]
        if not fin:
            continue
        res = _judge(r, fin)
        if res:
            r["result"] = res
            r["final"] = f"{fin['_away']} {fin[fin['_away']]}:{fin[fin['_home']]} {fin['_home']}"
            changed += 1
    if changed:
        with open(PUSHES, "w") as f:
            for r in pushes:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    open_ct = sum(1 for r in pushes if not r.get("result"))
    print(f"settle: {changed} settled, {open_ct} still open")
    return changed


# ---------------- stats ----------------

def _wl(rows):
    w = sum(1 for r in rows if r.get("result") == "win")
    l = sum(1 for r in rows if r.get("result") == "loss")
    return w, l


def stats():
    pushes, bets = load(PUSHES), load(BETS)
    formal = [r for r in pushes if r.get("delivered") and r.get("kind") in ("hit", "leak", "leak_plus", "trigger")]
    print("=== pushes (experience bank) ===")
    print(f"total records: {len(pushes)}")
    w, l = _wl(formal)
    if w + l:
        print(f"formal pushes delivered: {w}W-{l}L  hit {w/(w+l)*100:.1f}%  (n={w+l})")
    for kind in ("hit", "leak", "leak_plus", "trigger", "attach", "missed"):
        rows = [r for r in pushes if r.get("kind") == kind]
        w, l = _wl(rows)
        if w + l:
            print(f"  kind={kind}: {w}W-{l}L  hit {w/(w+l)*100:.1f}%  (n={w+l})")
    for mkt in ("ml", "spread", "total"):
        rows = [r for r in formal if r.get("market") == mkt]
        w, l = _wl(rows)
        if w + l:
            print(f"  market={mkt}: {w}W-{l}L  hit {w/(w+l)*100:.1f}%  (n={w+l})")
    print("calibration (model prob vs actual, formal delivered only):")
    for lo, hi in ((0.85, 0.90), (0.90, 0.95), (0.95, 1.01)):
        rows = [r for r in formal if r.get("prob") is not None and lo <= r["prob"] < hi]
        w, l = _wl(rows)
        if w + l:
            avg = sum(r["prob"] for r in rows) / len(rows)
            print(f"  prob {lo*100:.0f}-{min(hi, 1.0)*100:.0f}%: n={w+l}  avg model {avg*100:.1f}%  actual {w/(w+l)*100:.1f}%")
    print("=== user bets ===")
    if bets:
        w, l = _wl(bets)
        gross = sum(b.get("stake_gross") or 0 for b in bets)
        refunds = sum(b.get("refund") or 0 for b in bets)
        payout = sum(b.get("payout") or 0 for b in bets)
        net = gross - refunds
        print(f"bets: {len(bets)} ({w}W-{l}L)  gross ${gross:.2f}  refunds ${refunds:.2f}  "
              f"net staked ${net:.2f}  payout ${payout:.2f}  P&L ${payout-net:+.2f}")
    else:
        print("bets: none recorded")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "settle":
        settle()
    elif cmd == "stats":
        stats()
    else:
        print(__doc__)
