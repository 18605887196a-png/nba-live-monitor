#!/usr/bin/env python3
"""NBA live moneyline monitor: ESPN score/clock + Polymarket gamma prices.

Polls every --poll seconds, evaluates an in-game win-probability model
against the Polymarket moneyline book, and exits on a trigger, on the
end-of-Q3 snapshot (mode q3), or at the final buzzer (mode final).
Read-only. Places no orders.

Model (per team): WP = Phi((lead + M * t/48) / (13 * sqrt(t/48)))
  lead  = team score minus opponent score
  M     = team's pregame expected final margin (favorite positive)
  t     = minutes remaining in regulation (OT periods count as 5)
"""
import argparse, json, math, sys, time, urllib.request

def espn_event(event_id):
    # scoreboard first; falls back to summary endpoint when scoreboard
    # throws its usual post-tipoff IncompleteRead streak.
    last = None
    urls = [
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}",
    ]
    for url in urls:
        try:
            data = fetch(url)
            if "header" in data:  # summary shape
                comp = data["header"]["competitions"][0]
                ev_status, ev_comp = comp.get("status", {}), comp
            else:  # scoreboard shape
                ev = next((e for e in data.get("events", []) if str(e.get("id")) == str(event_id)), None)
                if ev is None:
                    last = Exception("event not found on scoreboard")
                    continue
                ev_status, ev_comp = ev.get("status", {}), ev.get("competitions", [{}])[0]
            return ev_status, ev_comp
        except Exception as e:
            last = e
    raise last

def fetch(url, tries=3, interval=2.0, timeout=15):
    # ESPN scoreboard throws IncompleteRead right after tipoff; retry before giving up.
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(interval)
    raise last

def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def parse_clock(clock):
    # "5:22" or "0.0" style displayClock
    try:
        if ":" in clock:
            m, s = clock.split(":")
            return int(m) + int(float(s)) / 60.0
        return float(clock) / 60.0
    except Exception:
        return 0.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--home-margin", type=float, required=True,
                    help="pregame expected final margin for the HOME team, e.g. 2.5 if home favored by 2.5")
    ap.add_argument("--ml-question", required=True)
    ap.add_argument("--spread-question", default="")
    ap.add_argument("--total-question", default="")
    ap.add_argument("--threshold", type=float, default=0.06)
    ap.add_argument("--threshold-pre", type=float, default=None, help="trigger threshold before Q4 (default: --threshold)")
    ap.add_argument("--threshold-q4", type=float, default=None, help="trigger threshold in Q4 and OT (default: --threshold)")
    ap.add_argument("--checkpoint", type=float, default=0.0, help="if >0, exit with Q4_CHECKPOINT when period>=4 and minutes remaining <= this value")
    ap.add_argument("--mode", choices=["q3", "final", "trigger"], default="final",
                    help="q3: exit with snapshot when Q4 starts. final: run to buzzer. trigger: exit on first trigger or buzzer.")
    ap.add_argument("--poll", type=int, default=12)
    ap.add_argument("--max-polls", type=int, default=0, help="0 = unlimited (test with 1)")
    ap.add_argument("--out", default="/tmp/nba_monitor_result.txt")
    ap.add_argument("--log", default="/tmp/nba_monitor.log")
    args = ap.parse_args()
    if args.threshold_pre is None: args.threshold_pre = args.threshold
    if args.threshold_q4 is None: args.threshold_q4 = args.threshold

    logf = open(args.log, "a", buffering=1)

    def emit(kind, data):
        line = json.dumps({"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **data}, ensure_ascii=False)
        print(line, flush=True)
        logf.write(line + "\n")
        if kind in ("TRIGGER", "Q3_SNAPSHOT", "Q4_CHECKPOINT", "FINAL", "TEST"):
            with open(args.out, "w") as f:
                f.write(line + "\n")

    streak = {0: 0, 1: 0}   # per outcome index: consecutive polls meeting trigger
    polls = 0
    last_state = None
    stale = 0

    while True:
        polls += 1
        try:
            st, comp0 = espn_event(args.event_id)
            period = st.get("period", 0)
            clock = st.get("displayClock", "0.0")
            completed = bool(st.get("type", {}).get("completed"))
            comp = comp0.get("competitors", [])
            teams = {}
            order = []
            for c in comp:
                ha = c.get("homeAway")
                teams[ha] = {"name": c.get("team", {}).get("displayName"), "score": int(c.get("score") or 0),
                             "quarters": [l.get("value") for l in c.get("linescores", [])]}
                order.append(ha)
            home, away = teams.get("home", {}), teams.get("away", {})
            # minutes remaining in regulation
            clk = parse_clock(str(clock))
            if period >= 5:
                t = clk
            elif period >= 1:
                t = clk + 12.0 * (4 - period)
            else:
                t = 48.0

            gm = fetch(f"https://gamma-api.polymarket.com/events?slug={args.slug}")
            gev = gm[0] if isinstance(gm, list) and gm else {}
            markets = {m.get("question"): m for m in gev.get("markets", [])}
            ml = markets.get(args.ml_question, {})
            outcomes = json.loads(ml.get("outcomes", "[]")) if isinstance(ml.get("outcomes"), str) else (ml.get("outcomes") or [])
            prices = json.loads(ml.get("outcomePrices", "[]")) if isinstance(ml.get("outcomePrices"), str) else (ml.get("outcomePrices") or [])
            bid0, ask0 = ml.get("bestBid"), ml.get("bestAsk")
            # map gamma outcome index -> team by name match
            def side(i):
                bid = bid0 if i == 0 else (None if ask0 is None else 1 - ask0)
                ask = ask0 if i == 0 else (None if bid0 is None else 1 - bid0)
                return bid, ask
            # win prob per home/away
            lead_home = home.get("score", 0) - away.get("score", 0)
            if t >= 0.5:
                denom = 13.0 * math.sqrt(t / 48.0)
                wp_home = phi((lead_home + args.home_margin * (t / 48.0)) / denom)
            else:
                wp_home = None
            wp = {"home": wp_home, "away": (None if wp_home is None else 1 - wp_home)}

            state_key = (period, clock, home.get("score"), away.get("score"))
            stale = stale + 1 if state_key == last_state else 0
            last_state = state_key

            # find each team's outcome index
            idx = {}
            for i, name in enumerate(outcomes):
                for ha in ("home", "away"):
                    if teams.get(ha, {}).get("name") and teams[ha]["name"].split()[-1] == name.split()[-1]:
                        idx[ha] = i
            row = {"period": period, "clock": clock, "home": home, "away": away, "t": round(t, 2),
                   "wp_home": None if wp_home is None else round(wp_home, 3),
                   "ml_prices": prices, "stale_polls": stale}
            trig = None
            cur_thr = args.threshold_q4 if period >= 4 else args.threshold_pre
            if wp_home is not None and len(outcomes) == 2:
                for ha in ("home", "away"):
                    i = idx.get(ha)
                    if i is None: continue
                    bid, ask = side(i)
                    if bid is None or ask is None: continue
                    edge = wp[ha] - ask
                    ok = edge >= cur_thr and (ask - bid) <= 0.10
                    streak[i] = streak[i] + 1 if ok else 0
                    if streak[i] >= 3:
                        trig = {"side": teams[ha]["name"], "ha": ha, "ask": ask, "bid": bid,
                                "wp": round(wp[ha], 3), "edge_pp": round(edge * 100, 1)}
                        row["streak"] = dict(streak)
            def mkt_quotes(q):
                # bestBid/bestAsk per outcome index (index 1 mirrored as 1-ask / 1-bid);
                # outcomePrices is lagging last-trade, never use it for order reference.
                m = markets.get(q, {}) or {}
                bb, ba = m.get("bestBid"), m.get("bestAsk")
                if bb is None or ba is None:
                    return None
                bb, ba = float(bb), float(ba)
                return [{"bid": round(bb, 3), "ask": round(ba, 3)},
                        {"bid": round(1 - ba, 3), "ask": round(1 - bb, 3)}]
            row["spread_quotes"] = mkt_quotes(args.spread_question) if args.spread_question else None
            row["total_quotes"] = mkt_quotes(args.total_question) if args.total_question else None
            row["spread_mkt"] = (markets.get(args.spread_question, {}) or {}).get("outcomePrices") if args.spread_question else None
            row["total_mkt"] = (markets.get(args.total_question, {}) or {}).get("outcomePrices") if args.total_question else None
            row["total_pts"] = home.get("score", 0) + away.get("score", 0)
            elapsed = 48.0 - t if period <= 4 else 48.0
            row["pace_proj_total"] = round(row["total_pts"] + (row["total_pts"] / elapsed) * t, 1) if elapsed > 0 and period <= 4 else None
            emit("POLL", row)

            if trig:
                emit("TRIGGER", {"trigger": trig, "period": period, "clock": clock,
                                 "score": f"{away.get('name')} {away.get('score')} : {home.get('score')} {home.get('name')}"})
                # Experience bank: one record per (event, side) trigger, first wins.
                # Self-contained on purpose (monitor.py runs detached via setsid);
                # logging must never break the monitor loop.
                try:
                    import datetime as _dt, os as _os
                    _cst = _dt.timezone(_dt.timedelta(hours=8))
                    _now = _dt.datetime.now(_cst)
                    _dus = (_now - _dt.timedelta(days=1)).strftime("%Y%m%d") if _now.hour < 15 else _now.strftime("%Y%m%d")
                    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "experience", "pushes.jsonl")
                    _os.makedirs(_os.path.dirname(_p), exist_ok=True)
                    _rec = {"ts": _now.strftime("%Y-%m-%d %H:%M:%S"), "date_us": _dus,
                            "event_id": str(args.event_id),
                            "game": f"{away.get('name')} @ {home.get('name')}",
                            "market": "ml", "pick": trig["side"], "line": None,
                            "price_c": round(trig["ask"] * 100, 1), "prob": trig["wp"],
                            "kind": "trigger", "delivered": True,
                            "score": f"{away.get('score')}:{home.get('score')}",
                            "clock": f"Q{period} {clock}", "result": None}
                    _dup = False
                    if _os.path.exists(_p):
                        with open(_p) as _f:
                            for _l in _f:
                                try:
                                    _r = json.loads(_l)
                                except Exception:
                                    continue
                                if (_r.get("event_id"), _r.get("market"), _r.get("pick")) == (_rec["event_id"], "ml", _rec["pick"]):
                                    _dup = True
                                    break
                    if not _dup:
                        with open(_p, "a") as _f:
                            _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                # trigger is data-log only; only mode=trigger exits on it (2026-10-24 fix:
                # q3 mode used to exit on trigger and never emit Q3_SNAPSHOT)
                if args.mode == "trigger":
                    return 0
            if args.mode == "q3" and period >= 4:
                emit("Q3_SNAPSHOT", row)
                return 0
            if args.checkpoint and period >= 4 and t <= args.checkpoint:
                emit("Q4_CHECKPOINT", row)
                return 0
            if completed:
                emit("FINAL", {"score": f"{away.get('name')} {away.get('score')} : {home.get('score')} {home.get('name')}",
                               "quarters_away": away.get("quarters"), "quarters_home": home.get("quarters"),
                               "periods": period})
                return 0
            if args.max_polls and polls >= args.max_polls:
                emit("TEST", row)
                return 0
        except Exception as e:
            emit("POLL", {"error": str(e)[:200]})
        time.sleep(args.poll)

if __name__ == "__main__":
    sys.exit(main())
