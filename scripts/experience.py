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
import contextlib, fcntl, json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "experience")
PUSHES = os.path.join(BASE, "pushes.jsonl")
BETS = os.path.join(BASE, "bets.jsonl")
RISKS = os.path.join(BASE, "risk_updates.jsonl")


@contextlib.contextmanager
def _file_lock(path):
    """Cross-process exclusive lock for one JSONL file (C).

    Every writer of the same file (read-modify-write AND append) must
    hold this lock for its whole operation: an append that lands
    between another writer's read and rewrite would otherwise be
    silently lost, and both writers would still report success.
    flock is released automatically if the holder dies, so a stale
    lock cannot block later writers.
    """
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _write_all(path, records):
    """Write a whole JSONL file atomically (temp file + os.replace).
    Callers must hold _file_lock(path); no reader ever sees a
    half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


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


def _append_unlocked(path, rec):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def _append(path, rec):
    with _file_lock(path):
        _append_unlocked(path, rec)


def _key(r):
    return (r.get("date_us"), r.get("event_id"), r.get("market"), r.get("pick"), r.get("line"))


def _key_detected(r):
    """v1 detected dedup: kind is part of the key, so a hit and a leak
    for the same line are two distinct candidates and both are kept.
    Legacy log_push keeps the old key (without kind) for backfills."""
    return (r.get("date_us"), r.get("event_id"), r.get("market"), r.get("pick"), r.get("line"), r.get("kind"))


def log_push(rec):
    """Legacy writer, kept for backfills only.

    Live code must use log_detected() + mark_sent() instead: detected
    is never automatically delivered=True (2026-10-25 architecture,
    section 7). log_push preserves its old behaviour for historical
    callers that pass delivered explicitly.
    """
    rec = dict(rec)
    rec.setdefault("ts", now_cst())
    rec.setdefault("result", None)
    with _file_lock(PUSHES):
        if _key(rec) in {_key(r) for r in load(PUSHES)}:
            return False
        _append_unlocked(PUSHES, rec)
    return True


def log_detected(rec):
    """Record a candidate at detection time, full precision, unshown
    candidates included. lifecycle=detected, delivered=False, sent_at
    is None until mark_sent() is called after a real TG send. First
    sighting of a (date, event, market, pick, line) wins; later polls
    never overwrite its detected price/probability."""
    rec = dict(rec)
    rec.setdefault("ts", now_cst())
    rec.setdefault("detected_at", rec["ts"])
    rec.setdefault("result", None)
    rec["lifecycle"] = "detected"
    # delivered is only set True by mark_sent(); never here.
    rec.setdefault("delivered", False)
    rec.setdefault("sent_at", None)
    with _file_lock(PUSHES):
        if _key_detected(rec) in {_key_detected(r) for r in load(PUSHES)}:
            return False
        _append_unlocked(PUSHES, rec)
    return True


def mark_sent(key, sent_at=None, sent_price_c=None, sent_prob=None):
    """Mark a detected record as actually sent, after a TG send ok.

    key: (date_us, event_id, market, pick, line) or with kind appended.
    Only called inside a sender's `if ok:` block; a failed send never
    calls this. Does not overwrite detected price/probability;
    send-time values go in sent_* fields. Returns True when updated.
    """
    with _file_lock(PUSHES):
        pushes = load(PUSHES)
        changed = False
        kt = tuple(key)
        for r in pushes:
            match = _key_detected(r) == kt if len(kt) == 6 else _key(r) == kt
            if match:
                r["lifecycle"] = "sent"
                r["delivered"] = True
                r["sent_at"] = sent_at or now_cst()
                if sent_price_c is not None:
                    r["sent_price_c"] = sent_price_c
                if sent_prob is not None:
                    r["sent_prob"] = sent_prob
                changed = True
                break
        if changed:
            _write_all(PUSHES, pushes)
    return changed


def log_risk(rec):
    """Append a risk update (REVIEW/INVALIDATED) to risk_updates.jsonl.
    Risk updates are not pushes and never enter pushes.jsonl calibration."""
    rec = dict(rec)
    rec.setdefault("ts", now_cst())
    _append(RISKS, rec)
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
    # Phase 1 (no lock): collect unsettled events and fetch finals.
    # Holding the file lock across network calls would stall every
    # other game's sender for the whole fetch.
    event_ids = {str(r["event_id"]) for r in load(PUSHES)
                 if not r.get("result") and r.get("event_id")}
    finals = {eid: _final_for_event(eid) for eid in event_ids}
    # Phase 2 (one lock): re-load (so concurrent mark_sent /
    # log_detected during the fetch are preserved) and apply.
    with _file_lock(PUSHES):
        pushes = load(PUSHES)
        changed = 0
        for r in pushes:
            if r.get("result") or not r.get("event_id"):
                continue
            fin = finals.get(str(r["event_id"]))
            if not fin:
                continue
            res = _judge(r, fin)
            if res:
                r["result"] = res
                r["final"] = f"{fin['_away']} {fin[fin['_away']]}:{fin[fin['_home']]} {fin['_home']}"
                changed += 1
        if changed:
            _write_all(PUSHES, pushes)
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
    # v1 (2026-10-25): formal = actually sent only. Legacy delivered
    # (pre-v1 fast_check/monitor logs) mixed detected with sent, so it
    # is shown separately and must not be quoted as sent calibration.
    formal = [r for r in pushes if r.get("lifecycle") == "sent" and r.get("kind") in ("hit", "leak", "leak_plus", "trigger")]
    legacy = [r for r in pushes if r.get("delivered") and r.get("kind") in ("hit", "leak", "leak_plus", "trigger")]
    detected = [r for r in pushes if r.get("lifecycle") == "detected"]
    print("=== pushes (experience bank) ===")
    print(f"total records: {len(pushes)}  detected-not-sent: {len(detected)}  sent (v1): {len(formal)}  legacy delivered (pre-v1, detected+sent mixed): {len(legacy)}")
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
        # P&L only over money-complete bets; incomplete history rows (unknown
        # price/stake) count in W-L but never in money, and are never estimated.
        complete = [b for b in bets if b.get("payout") is not None and b.get("stake_gross") is not None]
        gross = sum(b.get("stake_gross") or 0 for b in complete)
        refunds = sum(b.get("refund") or 0 for b in complete)
        payout = sum(b.get("payout") or 0 for b in complete)
        net = gross - refunds
        print(f"bets: {len(bets)} ({w}W-{l}L), {len(complete)} with complete money: "
              f"gross ${gross:.2f}  refunds ${refunds:.2f}  "
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
