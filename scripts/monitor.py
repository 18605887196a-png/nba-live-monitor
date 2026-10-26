#!/usr/bin/env python3
"""NBA live moneyline monitor: ESPN score/clock + Polymarket gamma prices.

Polls every --poll seconds, evaluates an in-game win-probability model
against the Polymarket moneyline book, and exits on a trigger, on the
end-of-Q3 snapshot (mode q3), or at the final buzzer (mode final).
In mode final it also emits two fixed analysis nodes without exiting:
Q3_NODE when Q4 starts and Q4_6MIN_NODE when Q4 reaches 6:00 remaining;
each fires once (a late join in Q4 at <=6:00 fires only the Q4 node).
Read-only. Places no orders.

Model (per team): WP = Phi((lead + M * t/48) / (13 * sqrt(t/48)))
  lead  = team score minus opponent score
  M     = team's pregame expected final margin (favorite positive)
  t     = minutes remaining in regulation (OT periods count as 5)
"""
import argparse, json, math, sys, time, urllib.request

# v1 (2026-10-25): share clock/WP with fast_check via game_context.
# Fallback to local implementations if the module is missing, so the
# monitor never breaks on import alone.
try:
    from game_context import parse_clock_seconds as _parse_clock_seconds, game_time as _game_time, wp_home as _wp_home
    _GAME_CTX = True
except Exception:
    _GAME_CTX = False

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
    # "5:22" or "0.0" style displayClock -> minutes remaining in period.
    # Delegates to game_context when available (single clock parser).
    if _GAME_CTX:
        return _parse_clock_seconds(clock) / 60.0
    try:
        if ":" in clock:
            m, s = clock.split(":")
            return int(m) + int(float(s)) / 60.0
        return float(clock) / 60.0
    except Exception:
        return 0.0

def check_nodes(fired, period, t, live, completed):
    """Fixed analysis nodes (user, 2026-10-24): Q3 end + Q4 6:00 left.

    Pure helper (unit-tested): fired is a set of node names already emitted.
    Returns (node_or_None, new_fired_set). Only period == 4 counts; OT
    (period >= 5) never fires a node, and a game that already ended or has
    t <= 0.5 never fires one either. A late join in Q4 at <= 6:00 fires
    only the Q4 node (the Q3 node is superseded).
    """
    fired = set(fired)
    if not live or completed or t <= 0.5 or period != 4:
        return None, fired
    if t <= 6.0:
        if "q4_6min" not in fired:
            fired.update(("q4_6min", "q3"))
            return "q4_6min", fired
        return None, fired
    if "q3" not in fired:
        fired.add("q3")
        return "q3", fired
    return None, fired

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
    ap.add_argument("--poll", type=int, default=20)
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
        if kind in ("TRIGGER", "Q3_SNAPSHOT", "Q4_CHECKPOINT", "Q3_NODE", "Q4_6MIN_NODE", "FINAL", "TEST"):
            with open(args.out, "w") as f:
                f.write(line + "\n")

    streak = {0: 0, 1: 0}   # per outcome index: consecutive polls meeting trigger
    polls = 0
    last_state = None
    stale = 0
    nodes_fired = set()  # fixed analysis nodes already emitted (Q3 end, Q4 6:00)

    while True:
        polls += 1
        try:
            st, comp0 = espn_event(args.event_id)
            # .get(key, default) does NOT protect against an explicit null:
            # period None used to raise TypeError on every poll below, and the
            # loop's except turned it into an endless error-POLL retry.
            period = st.get("period") or 0
            clock = st.get("displayClock") or "0.0"
            completed = bool(st.get("type", {}).get("completed"))
            # Only a live game may trigger. Pre-game states carry period 0,
            # and scoring them as a full 48-minute game fabricates triggers.
            live = st.get("type", {}).get("description") == "In Progress"
            comp = comp0.get("competitors", [])
            teams = {}
            order = []
            for c in comp:
                ha = c.get("homeAway")
                teams[ha] = {"name": c.get("team", {}).get("displayName"), "score": int(c.get("score") or 0),
                             "quarters": [l.get("value") for l in c.get("linescores", [])]}
                order.append(ha)
            home, away = teams.get("home", {}), teams.get("away", {})
            # minutes remaining / elapsed, via the shared game_time():
            # OT elapsed is 48 + (OT-1)*5 + OT elapsed, never 48 - OT rem.
            if _GAME_CTX:
                elapsed_total, t, _is_ot = _game_time(period, str(clock))
            else:
                clk = parse_clock(str(clock))
                if period >= 5:
                    t = clk
                    elapsed_total = 48.0 + (period - 5) * 5.0 + (5.0 - clk)
                elif period >= 1:
                    t = clk + 12.0 * (4 - period)
                    elapsed_total = 48.0 - t
                else:
                    t = 48.0
                    elapsed_total = 0.0

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
            # win prob per home/away, shared model (game_context.wp_home).
            lead_home = home.get("score", 0) - away.get("score", 0)
            if _GAME_CTX:
                wp_home = _wp_home(lead_home, args.home_margin, t)
            elif t >= 0.5:
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
            if live and wp_home is not None and len(outcomes) == 2:
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
            # OT pace: total elapsed (incl. OT) * remaining OT time.
            # Previously OT pace was None (period<=4 guard) and elapsed
            # was stuck at 48; both are fixed by elapsed_total above.
            row["pace_proj_total"] = round(row["total_pts"] + (row["total_pts"] / elapsed_total) * t, 1) if elapsed_total > 0 else None
            emit("POLL", row)

            if trig:
                emit("TRIGGER", {"trigger": trig, "period": period, "clock": clock,
                                 "score": f"{away.get('name')} {away.get('score')} : {home.get('score')} {home.get('name')}"})
                # Experience bank v1: TRIGGER is data-log only, so it is
                # logged as DETECTED, never delivered=True. Sender marks
                # SENT only after a real TG send. First wins, as before.
                try:
                    import datetime as _dt
                    import experience as _exp
                    _cst = _dt.timezone(_dt.timedelta(hours=8))
                    _now = _dt.datetime.now(_cst)
                    _dus = (_now - _dt.timedelta(days=1)).strftime("%Y%m%d") if _now.hour < 15 else _now.strftime("%Y%m%d")
                    _exp.log_detected({
                        "ts": _now.strftime("%Y-%m-%d %H:%M:%S"), "date_us": _dus,
                        "event_id": str(args.event_id),
                        "game": f"{away.get('name')} @ {home.get('name')}",
                        "market": "ml", "pick": trig["side"], "line": None,
                        "price_c": round(trig["ask"] * 100, 1), "raw_price": trig["ask"],
                        "prob": trig["wp"], "raw_prob": trig["wp"],
                        "kind": "trigger",
                        "score": f"{away.get('score')}:{home.get('score')}",
                        "clock": f"Q{period} {clock}", "result": None})
                except Exception:
                    pass
                # trigger is data-log only; only mode=trigger exits on it (2026-10-24 fix:
                # q3 mode used to exit on trigger and never emit Q3_SNAPSHOT)
                if args.mode == "trigger":
                    return 0
            # Fixed analysis nodes (mode final only; q3/trigger modes keep their
            # own exits). Nodes never exit the loop: the monitor keeps running
            # so mid/late-game high-prob events still push one message each.
            if args.mode == "final":
                node, nodes_fired = check_nodes(nodes_fired, period, t, live, completed)
                if node == "q3":
                    emit("Q3_NODE", row)
                elif node == "q4_6min":
                    emit("Q4_6MIN_NODE", row)
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
