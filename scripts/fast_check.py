#!/usr/bin/env python3
"""fast_check.py - parallel ESPN scores + gamma books, tiered output.

Tiering (user, 2026-10-24):
- MAIN: any ML/spread/total line with model prob >= 95% (totals also need |proj-line| >= 10)
- Q4 HIT gate (user 2026-10-25): Q4 HITs only when <=5:00 remains; early-Q4 HITs held (leaks ungated)
- AUX: ladder inversion (any size >= 0.5c) with real two-sided quotes and
  easier-line model prob >= 90%; below 90% is filtered out in code.
Output (user, 2026-10-24): conclusion-first — a 结论 line names the single
top pick (best leak, else best hit); leaks print with a 【倒挂】 marker and
stay first; no model-vs-market gap analysis is printed.

Usage: python3 fast_check.py <US date YYYYMMDD> <event_id:slug:home_margin> [...]
home_margin: pregame expected full-game margin for home team (positive if home favored).
"""
import json, math, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

DATE_US = None  # set by main(); used as the experience-bank date key

# v1 (2026-10-25): new context modules are optional; the text path and
# push thresholds work without them, with the OT elapsed fix applied
# directly below regardless of imports.
try:
    from game_context import window_features, risk_tags, SnapshotHistory
    from market_context import (MarketHistory, market_window_features, parse_gamma_market,
                                cost_for_target, normalize_under_quote, token_for_total, fetch_book,
                                trade_status_from_market, tradable_for_new)
    from decision import decide_candidate
    from policy_config import (POLICY_VERSION, MODEL_VERSION, HIT_PROB, LEAK_PROB,
                               Q4_HIT_LATE_REMAINING_MIN, Q4_HIT_BLOWOUT_LEAD,
                               TOTALS_MIN_ELAPSED_MIN, TOTALS_MIN_MARGIN,
                               OT_WARN_CLOSE_LEAD, OT_WARN_OT_PROB, OT_WARN_OT_CUSHION,
                               MARKET_SCHEMA_VERSION, TRACKED_COST_TIMEOUT_SEC)
    _NEW_MODULES = True
except Exception:
    _NEW_MODULES = False
    trade_status_from_market = None
    tradable_for_new = lambda s: s in ("ok", "unknown")
    HIT_PROB, LEAK_PROB = 0.95, 0.90
    Q4_HIT_LATE_REMAINING_MIN, Q4_HIT_BLOWOUT_LEAD = 5.0, 30
    TOTALS_MIN_ELAPSED_MIN, TOTALS_MIN_MARGIN = 15, 10
    OT_WARN_CLOSE_LEAD, OT_WARN_OT_PROB, OT_WARN_OT_CUSHION = 8, 0.03, 20
    MARKET_SCHEMA_VERSION, TRACKED_COST_TIMEOUT_SEC = 2, 5.0


def _history_dir():
    d = os.environ.get("FC_HISTORY_DIR")
    if d:
        return d
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "experience", "snapshots")


def _target_usd():
    v = os.environ.get("FC_TARGET_USD") or os.environ.get("TG_TARGET_USD")
    try:
        return float(v) if v else None
    except Exception:
        return None


def _fee_per_share():
    v = os.environ.get("FC_FEE_PER_SHARE")
    try:
        return float(v) if v else None
    except Exception:
        return None

def _parse_rec(text, ctx, kind, delivered=True):
    """Turn one shown HIT/LEAK line into an experience-bank record (or None)."""
    m = re.search(r"ask (\d+(?:\.\d+)?)c", text)
    price = float(m.group(1)) if m else None
    pm = re.search(r"(?:prob|cover) (\d+(?:\.\d+)?)%", text)
    prob = float(pm.group(1)) / 100 if pm else None
    base = dict(ctx, price_c=price, prob=prob, delivered=delivered, result=None)
    mm = re.match(r"ML (\S+) ask", text)
    if mm:
        return {**base, "market": "ml", "pick": mm.group(1), "line": None, "kind": kind}
    mm = re.match(r"Spread (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return {**base, "market": "spread", "pick": mm.group(1), "line": float(mm.group(2)), "kind": kind}
    mm = re.match(r"Total (Over|Under) (\d+(?:\.\d+)?) ask", text)
    if mm:
        return {**base, "market": "total", "pick": mm.group(1), "line": float(mm.group(2)), "kind": kind}
    mm = re.match(r"LEAK Plus (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return {**base, "market": "spread", "pick": mm.group(1), "line": float(mm.group(2)), "kind": "leak_plus"}
    mm = re.match(r"LEAK (Over|Under) (\d+(?:\.\d+)?) ask", text)
    if mm:
        return {**base, "market": "total", "pick": mm.group(1), "line": float(mm.group(2)), "kind": "leak"}
    mm = re.match(r"LEAK Spread (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return {**base, "market": "spread", "pick": mm.group(1), "line": float(mm.group(2)), "kind": "leak"}
    return None

def get(u, timeout=12):
    r = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=timeout))

def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fmt_price(p):
    return f"{p*100:.0f}c(x{1/p:.2f})" if 0 < p < 1 else f"{p*100:.0f}c"

def analyze(gid, slug, hm, state):
    hs, as_, per, clk, hab, aab, desc = state
    # Only a live game is evaluated. Any other state (Scheduled, Delayed,
    # Postponed, Final, ...) returns without touching numbers: a pre-game
    # state carries period 0 (or None), and evaluating it as a full 48-minute
    # game fabricates hits. A None period alone is also non-live.
    # Halftime counts as live: real score, 24 min remaining, no fabrication.
    if desc not in ('In Progress', 'Halftime') or per is None:
        return f"{aab}@{hab}: {desc} {as_}:{hs}"
    def _load_tracked_raw():
        try:
            _tp = os.environ.get("FC_TRACKED_FILE")
            if _tp and os.path.exists(_tp):
                with open(_tp) as _tf:
                    _raw = json.load(_tf)
            elif os.environ.get("FC_TRACKED_JSON"):
                _raw = json.loads(os.environ.get("FC_TRACKED_JSON") or "[]")
            else:
                return []
            if isinstance(_raw, dict):
                _raw = _raw.get("tracked_signals") or _raw.get("signals") or []
            return [s for s in (_raw or []) if isinstance(s, dict)
                    and (not s.get("event_id") or str(s.get("event_id")) == str(gid))]
        except Exception:
            return []
    try:
        if clk is None or (isinstance(clk, str) and clk.strip() == ""):
            clock = None
        elif ':' in clk:
            mm, ss = clk.split(':'); clock = float(mm) + float(ss) / 60
        else:
            clock = float(clk) / 60  # ESPN shows bare seconds ("43.5") under a minute
    except Exception:
        clock = None
    # Clock-missing / unparseable / out-of-period is a data error,
    # never a fabricated 0.0 (which would look like period end / a
    # final). A real "0:00" parses to 0.0 and proceeds normally; only
    # a missing/unparseable value lands here. The structured file for
    # THIS scan is still written (status=data_error, scan_id intact)
    # so the sender can distinguish "this round is broken" from
    # "no fresh file at all", and tracked signals are reported as
    # clock_unknown REVIEW -- never INVALIDATED, CLOSED or settled.
    if clock is None or clock < 0 or (per <= 4 and clock > 12.0) or (per >= 5 and clock > 5.0):
        _clock_evals = []
        for _sig in _load_tracked_raw():
            _k2 = _sig.get("kind") or _sig.get("original_kind")
            _dec2 = None
            if _NEW_MODULES:
                try:
                    _dec2 = decide_candidate(
                        {"event_id": str(gid), "market": _sig.get("market"), "pick": _sig.get("pick"),
                         "line": _sig.get("line"), "kind": _k2, "raw_prob": None, "raw_price": None,
                         "data_status": "clock_unknown"},
                        {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                         "lead": hs - as_, "tot": hs + as_, "elapsed_min": None, "remaining_min": None,
                         "home_abbr": hab, "away_abbr": aab},
                        None, None, None)
                except Exception as _e:
                    _dec2 = {"state": "REVIEW", "reason_codes": ["clock_unknown"],
                             "reasons_text": [f"复查异常：{type(_e).__name__}"]}
            _clock_evals.append({"signal_id": _sig.get("signal_id"), "market": _sig.get("market"),
                                 "pick": _sig.get("pick"), "line": _sig.get("line"), "kind": _k2,
                                 "raw_prob": None, "raw_price": None, "bid": None, "ask": None,
                                 "data_status": "clock_unknown", "decision": _dec2,
                                 "evaluated_epoch": time.time()})
        _out_path = os.environ.get("FC_STRUCTURED_OUT")
        if _out_path:
            try:
                _struct = {"event_id": str(gid), "slug": slug, "status": "data_error",
                           "clock_status": "clock_unknown",
                           "game": {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                                    "home_abbr": hab, "away_abbr": aab, "lead": hs - as_, "tot": hs + as_},
                           "candidates_all": [], "tracked_evaluations": _clock_evals,
                           "policy_version": POLICY_VERSION if _NEW_MODULES else "legacy",
                           "model_version": MODEL_VERSION if _NEW_MODULES else "legacy",
                           "scan_id": os.environ.get("FC_SCAN_ID"),
                           "received_epoch": time.time()}
                _tmp = _out_path + ".tmp"
                with open(_tmp, "w") as _f:
                    json.dump(_struct, _f, ensure_ascii=False, default=str)
                os.replace(_tmp, _out_path)
            except Exception:
                pass
        return "DATA_ERROR clock_unknown: clock missing/unparseable/out of period"
    t = (4 - per) * 12 + clock if per <= 4 else clock
    # OT fix (2026-10-25 architecture, section 4.1): regulation 48 +
    # (OT-1)*5 + OT elapsed, never 48 - OT remaining. Regulation games
    # keep elapsed = 48 - t exactly as before.
    if per >= 5:
        elapsed = 48 + (per - 5) * 5 + (5 - clock)
    else:
        elapsed = 48 - t
    is_ot = per >= 5
    lead = hs - as_
    sd_m = 13 * math.sqrt(t / 48) if t >= 0.5 else 0.0
    wp_home = Phi((lead + hm * t / 48) / sd_m) if sd_m else 0.5
    tot = hs + as_
    # Note (user 2026-10-25): no total-based staleness guard - total has no
    # necessary relation to frozen data; pushes are probability-driven.
    # Totals Q3-end gate (user 2026-10-25, FIRST priority, overrides leak-first):
    # no totals prediction before Q3 ends (per < 4), for hits AND leaks.
    # Q3 just started has only minutes of second-half pace; totals need three
    # full quarters before they can be predicted.
    totals_ok = per >= 4 and elapsed >= TOTALS_MIN_ELAPSED_MIN and tot > 0
    hit_ok = not (per == 4 and t > Q4_HIT_LATE_REMAINING_MIN and abs(lead) < Q4_HIT_BLOWOUT_LEAD)  # Q4 HIT gate: hold early-Q4 HITs until last 5:00, unless 30+ blowout
    pace = (tot + 4.71 * 12) / (elapsed + 12) if elapsed > 0 else 4.71
    mu = tot + pace * t
    sd_t = min(2.0 * math.sqrt(pace * t), 21) if t > 0 else 1.0
    # OT probability (same normal model as below) + OT-scenario total.
    # Totals projections are regulation-only, so a close Q4 Under push
    # needs an explicit OT warning (user 2026-10-26, push + warning, no block).
    ot = 0.0
    if sd_m:
        _mean_m_home = lead + hm * t / 48
        ot = Phi((0.5 - _mean_m_home) / sd_m) - Phi((-0.5 - _mean_m_home) / sd_m)
    ot_scenario = mu + pace * 5.0 if per >= 4 else None

    def _ot_warning(line, pick):
        if pick != "Under" or ot_scenario is None or abs(lead) > OT_WARN_CLOSE_LEAD:
            return None
        _cush_ot = line - ot_scenario
        if ot < OT_WARN_OT_PROB and _cush_ot >= OT_WARN_OT_CUSHION:
            return None
        if _cush_ot < 0:
            return f"加时风险：若进加时按当前节奏约 {ot_scenario:.0f} 分，已超过 {line:g} 线"
        return f"加时风险：若进加时按当前节奏约 {ot_scenario:.0f} 分，离 {line:g} 只剩 {_cush_ot:.0f} 分"

    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")[0]
    _gamma_received_epoch = time.time()
    title = ev['title']
    totals, spreads, ml = {}, {}, None
    totals_id, spreads_id, ml_id = {}, {}, None
    # Trade status is recorded for EVERY market object, even when its
    # bid/ask is missing or dead: tracked evaluation must be able to
    # tell "closed" / "paused" apart from "this round's data missing".
    totals_status, spreads_status = {}, {}
    ml_status = "unknown"
    ml_present = False
    blocked_markets = []
    def _mstatus(m):
        if _NEW_MODULES and trade_status_from_market is not None:
            try:
                return trade_status_from_market(m)["status"]
            except Exception:
                return "unknown"
        try:
            if m.get("closed") is True:
                return "closed"
            if m.get("acceptingOrders", m.get("accepting_orders")) is False:
                return "paused"
            if m.get("active") is False:
                return "inactive"
            return "unknown"
        except Exception:
            return "unknown"
    team_names = []
    for m in ev['markets']:
        q = m.get('question', ''); b = m.get('bestBid'); a = m.get('bestAsk'); ln = m.get('line')
        _st = _mstatus(m)
        if q == title:
            try:
                team_names = json.loads(m.get('outcomes') or '[]')
            except Exception:
                team_names = []
            ml_present = True
            ml_status = _st
            if _st in ("closed", "paused", "inactive"):
                blocked_markets.append({"market": "ml", "status": _st})
            if b is not None and a is not None:
                ml = (b, a)
                ml_id = parse_gamma_market(m) if _NEW_MODULES else None
        # Record trade status even for markets whose quote is
        # missing/dead: otherwise a closed market with no price would
        # be misread as "this round's data missing" in review.
        if ln is not None and 'O/U' in q and 'vs.' in q and not any(x in q for x in ('1H', '1Q', '2Q', '3Q', '4Q')):
            totals_status[ln] = _st
            if _st in ("closed", "paused", "inactive"):
                blocked_markets.append({"market": "total", "line": ln, "status": _st})
        if ln is not None and q.startswith('Spread:'):
            _team0 = q.replace('Spread: ', '').rsplit(' (', 1)[0]
            spreads_status.setdefault(_team0, {})[ln] = _st
            if _st in ("closed", "paused", "inactive"):
                blocked_markets.append({"market": "spread", "pick": _team0, "line": ln, "status": _st})
        if ln is None or b is None or a is None or b <= 0.01 or a >= 0.999:
            continue
        if 'O/U' in q and 'vs.' in q and not any(x in q for x in ('1H', '1Q', '2Q', '3Q', '4Q')):
            totals[ln] = (b, a)
            totals_id[ln] = parse_gamma_market(m) if _NEW_MODULES else None
            totals_status[ln] = _st
        if q.startswith('Spread:'):
            team = q.replace('Spread: ', '').rsplit(' (', 1)[0]
            spreads.setdefault(team, []).append((ln, b, a))
            spreads_id.setdefault(team, {})[ln] = parse_gamma_market(m) if _NEW_MODULES else None
            spreads_status.setdefault(team, {})[ln] = _st

    hits, leaks = [], []
    # v1 structured candidates, built in parallel with the text lists
    # below, at full precision. Text output and thresholds are unchanged.
    cands = []

    def _add_cand(market, pick, line, kind, prob, buy_price, bid, ask, text, identity, ot_warning=None, leak_vs=None):
        cands.append({"market": market, "pick": pick, "line": line, "kind": kind,
                      "raw_prob": float(prob) if prob is not None else None,
                      "raw_price": float(buy_price) if buy_price is not None else None,
                      "bid": float(bid) if bid is not None else None,
                      "ask": float(ask) if ask is not None else None,
                      "text": text, "identity": identity, "ot_warning": ot_warning,
                      "leak_vs": leak_vs})
    if ml and sd_m and tradable_for_new(ml_status):
        if hit_ok and 1 - wp_home >= HIT_PROB:
            hits.append((1 - wp_home, f"ML {aab} ask {fmt_price(ml[1])} prob {(1-wp_home)*100:.0f}%"))
            _add_cand("ml", aab, None, "hit", 1 - wp_home, ml[1], ml[0], ml[1], f"ML {aab} ask {fmt_price(ml[1])} prob {(1-wp_home)*100:.0f}%", ml_id)
        if hit_ok and wp_home >= HIT_PROB:
            hits.append((wp_home, f"ML {hab} ask {fmt_price(1-ml[0])} prob {wp_home*100:.0f}%"))
            _add_cand("ml", hab, None, "hit", wp_home, 1 - ml[0], 1 - ml[1], 1 - ml[0], f"ML {hab} ask {fmt_price(1-ml[0])} prob {wp_home*100:.0f}%", ml_id)
    cover = {}
    for team, arr in spreads.items():
        is_away = bool(team_names) and team == team_names[0]
        mean_m = (-lead - hm * t / 48) if is_away else (lead + hm * t / 48)
        for ln, b, a in arr:
            if sd_m:
                p = Phi((mean_m + ln) / sd_m)
                cover[(team, ln)] = (p, a)
                if not tradable_for_new(spreads_status.get(team, {}).get(ln, "unknown")):
                    continue
                if hit_ok and p >= HIT_PROB:
                    hits.append((p, f"Spread {team} ({ln:+.1f}) ask {fmt_price(a)} cover {p*100:.0f}%"))
                    _add_cand("spread", team, ln, "hit", p, a, b, a, f"Spread {team} ({ln:+.1f}) ask {fmt_price(a)} cover {p*100:.0f}%", spreads_id.get(team, {}).get(ln))
                # Plus-line flip (user, 2026-10-24): the No side of a negative spread line is the
                # other team GETTING points (No on Clippers -3.5 == Lakers +3.5). It strictly
                # dominates that team's moneyline, so ask <= ML ask at prob >= 90% = top leak.
                if ln < 0 and ml and tradable_for_new(ml_status) and len(team_names) == 2 and b and b >= 0.02:
                    other = team_names[1] if team == team_names[0] else team_names[0]
                    p_no = 1 - p
                    ask_no = 1 - b
                    ml_a = ml[1] if other == team_names[0] else 1 - ml[0]
                    if p_no >= LEAK_PROB and ask_no <= ml_a and ask_no < 0.999:
                        leaks.append(f"LEAK Plus {other} ({-ln:+.1f}) ask {fmt_price(ask_no)} vs ML {fmt_price(ml_a)}: easier than ML, cheaper-or-equal, prob {p_no*100:.0f}%")
                        _add_cand("spread", other, -ln, "leak_plus", p_no, ask_no, 1 - a, ask_no, f"LEAK Plus {other} ({-ln:+.1f}) ask {fmt_price(ask_no)} vs ML {fmt_price(ml_a)}: easier than ML, cheaper-or-equal, prob {p_no*100:.0f}%", spreads_id.get(team, {}).get(ln), leak_vs={"original_team": team, "original_line": ln, "ml_ask": ml_a, "side": "spread_no"})
    for ln, (b, a) in totals.items():
        if not tradable_for_new(totals_status.get(ln, "unknown")):
            continue
        p_over = Phi((mu - ln) / sd_t)
        if hit_ok and totals_ok and p_over >= HIT_PROB and mu - ln >= TOTALS_MIN_MARGIN:
            hits.append((p_over, f"Total Over {ln} ask {fmt_price(a)} prob {p_over*100:.0f}% (now {tot:.0f}, need {ln-tot:.0f} more, proj {mu:.0f})"))
            _add_cand("total", "Over", ln, "hit", p_over, a, b, a, f"Total Over {ln} ask {fmt_price(a)} prob {p_over*100:.0f}% (now {tot:.0f}, need {ln-tot:.0f} more, proj {mu:.0f})", totals_id.get(ln))
        if hit_ok and totals_ok and 1 - p_over >= HIT_PROB and ln - mu >= TOTALS_MIN_MARGIN:
            _ub, _ua = normalize_under_quote(b, a) if _NEW_MODULES else (1 - a, 1 - b)
            hits.append((1 - p_over, f"Total Under {ln} ask {fmt_price(_ua)} prob {(1-p_over)*100:.0f}% (now {tot:.0f}, cushion {ln-tot:.0f}, proj {mu:.0f})"))
            _add_cand("total", "Under", ln, "hit", 1 - p_over, _ua, _ub, _ua, f"Total Under {ln} ask {fmt_price(_ua)} prob {(1-p_over)*100:.0f}% (now {tot:.0f}, cushion {ln-tot:.0f}, proj {mu:.0f})", totals_id.get(ln), ot_warning=_ot_warning(ln, "Under"))
    ls = sorted(totals)
    for i in range(len(ls) - 1):
        l1, l2 = ls[i], ls[i + 1]
        if not (tradable_for_new(totals_status.get(l1, "unknown")) and tradable_for_new(totals_status.get(l2, "unknown"))):
            continue
        ua1, ua2 = 1 - totals[l1][0], 1 - totals[l2][0]
        oa1, oa2 = totals[l1][1], totals[l2][1]
        if ua2 < ua1 - 0.005:
            p = 1 - Phi((mu - l2) / sd_t)
            if totals_ok and p >= LEAK_PROB:
                leaks.append(f"LEAK Under {l2} ask {fmt_price(ua2)} vs Under {l1} {fmt_price(ua1)}: easier+cheaper by {100*(ua1-ua2):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, cushion {l2-tot:.0f}, proj {mu:.0f})")
                _ub2, _ua2 = normalize_under_quote(totals[l2][0], totals[l2][1]) if _NEW_MODULES else (1 - totals[l2][1], ua2)
                _add_cand("total", "Under", l2, "leak", p, ua2, _ub2, _ua2, f"LEAK Under {l2} ask {fmt_price(ua2)} vs Under {l1} {fmt_price(ua1)}: easier+cheaper by {100*(ua1-ua2):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, cushion {l2-tot:.0f}, proj {mu:.0f})", totals_id.get(l2), ot_warning=_ot_warning(l2, "Under"), leak_vs={"line": l1, "ask": ua1, "side": "Under"})
        if oa2 > oa1 + 0.005:
            p = Phi((mu - l1) / sd_t)
            if totals_ok and p >= LEAK_PROB:
                leaks.append(f"LEAK Over {l1} ask {fmt_price(oa1)} vs Over {l2} {fmt_price(oa2)}: easier+cheaper by {100*(oa2-oa1):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, need {l1-tot:.0f} more, proj {mu:.0f})")
                _add_cand("total", "Over", l1, "leak", p, oa1, totals[l1][0], oa1, f"LEAK Over {l1} ask {fmt_price(oa1)} vs Over {l2} {fmt_price(oa2)}: easier+cheaper by {100*(oa2-oa1):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, need {l1-tot:.0f} more, proj {mu:.0f})", totals_id.get(l1), leak_vs={"line": l2, "ask": oa2, "side": "Over"})
    for team, arr in spreads.items():
        arr2 = sorted(arr)
        for i in range(len(arr2) - 1):
            ln1, b1, a1 = arr2[i]
            ln2, b2, a2 = arr2[i + 1]
            if not (tradable_for_new(spreads_status.get(team, {}).get(ln1, "unknown")) and tradable_for_new(spreads_status.get(team, {}).get(ln2, "unknown"))):
                continue
            if a2 < a1 - 0.005:
                p = cover.get((team, ln2), (0, 0))[0]
                if p >= 0.90:
                    leaks.append(f"LEAK Spread {team} ({ln2:+.1f}) ask {fmt_price(a2)} vs ({ln1:+.1f}) {fmt_price(a1)}: easier+cheaper by {100*(a1-a2):.0f}c, cover {p*100:.0f}%")
                    _add_cand("spread", team, ln2, "leak", p, a2, b2, a2, f"LEAK Spread {team} ({ln2:+.1f}) ask {fmt_price(a2)} vs ({ln1:+.1f}) {fmt_price(a1)}: easier+cheaper by {100*(a1-a2):.0f}c, cover {p*100:.0f}%", spreads_id.get(team, {}).get(ln2))

    # OT probability (user, 2026-10-24): P(final margin lands on 0) under the same normal model.
    ot = 0.0
    if sd_m:
        mean_m_home = lead + hm * t / 48
        ot = Phi((0.5 - mean_m_home) / sd_m) - Phi((-0.5 - mean_m_home) / sd_m)
    out = [f"{aab} {as_} @ {hab} {hs} | Q{per} {clk} | WP {aab} {(1-wp_home)*100:.0f}% / {hab} {wp_home*100:.0f}% | proj total {mu:.0f} | OT {ot*100:.0f}%"]
    # Priority (user, 2026-10-24 12:10 + 12:15): qualified inversions first,
    # then ML/totals hits, spreads always last (late-game margin is the least stable).
    leaks_sorted = [l for l in leaks if not l.startswith("LEAK Spread")] + [l for l in leaks if l.startswith("LEAK Spread")]
    main_hits = sorted([h for h in hits if not h[1].startswith("Spread")], reverse=True)
    spread_hits = sorted([h for h in hits if h[1].startswith("Spread")], reverse=True)
    # Per-type caps (fix 2026-10-24): a flat [:4] let a stack of totals rows crowd out
    # the moneyline hit entirely. ML always shows, totals capped at 2, spreads at 1.
    ml_hits = [h for h in main_hits if h[1].startswith("ML")]
    tot_hits = [h for h in main_hits if not h[1].startswith("ML")]
    shown = sorted(ml_hits[:2] + tot_hits[:2], reverse=True) + spread_hits[:1]
    # Conclusion-first (user, 2026-10-24): one line naming the single top pick
    # (best leak, else best hit); leaks print with a prominent 【倒挂】 marker.
    top = leaks_sorted[0] if leaks_sorted else (shown[0][1] if shown else None)
    if top:
        out.append(f"结论：{top}")
    for lk in leaks_sorted[:3]:
        out.append(f"  【倒挂】{lk}")
    for _, h in shown:
        out.append(f"  [HIT] {h}")
    if not hits and not leaks:
        out.append("  no hit / no leak")
    # F1 tracked re-evaluation (2026-10-26 fixes): previously sent
    # candidates are re-evaluated from THIS round's market data, even
    # when they no longer qualify as new pushes. A tracked signal is
    # identified by its fixed contract (market/pick/line + condition),
    # never by "it is still in the main line list".
    tracked_evaluations = []
    def _eval_tracked(sig):
        try:
            _mkt, _pick, _ln = sig.get("market"), sig.get("pick"), sig.get("line")
            # Kind must be read from the field register_tracked actually
            # writes. 69fbb1d records only carry original_kind; reading
            # kind alone yielded None and silently evaluated leaks
            # under hit rules (and hit thresholds in risk gating).
            _kind = sig.get("kind") or sig.get("original_kind")
            _thresh = sig.get("threshold_at_send")
            if _thresh is None:
                _thresh = LEAK_PROB if _kind in ("leak", "leak_plus") else HIT_PROB
            _base_false = False
            _inversion_broken = False
            _prob = None; _price = None; _bid = None; _ask = None; _data_status = "ok"
            _vs = sig.get("leak_vs") or {}
            if _kind is None:
                _dec0 = None
                if _NEW_MODULES:
                    try:
                        _dec0 = decide_candidate(
                            {"event_id": str(gid), "market": _mkt, "pick": _pick, "line": _ln,
                             "kind": None, "raw_prob": None, "raw_price": None,
                             "data_status": "unknown_kind"},
                            {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                             "lead": lead, "tot": tot, "elapsed_min": elapsed, "remaining_min": t,
                             "home_abbr": hab, "away_abbr": aab},
                            process, None, None)
                    except Exception as _e:
                        _dec0 = {"state": "REVIEW", "reason_codes": ["tracked_kind_unknown"],
                                 "reasons_text": [f"复查异常：{type(_e).__name__}"]}
                return {"signal_id": sig.get("signal_id"), "market": _mkt, "pick": _pick, "line": _ln,
                        "kind": None, "threshold_at_send": float(_thresh) if _thresh is not None else None,
                        "raw_prob": None, "raw_price": None, "bid": None, "ask": None,
                        "data_status": "unknown_kind", "base_condition_false": False,
                        "inversion_broken": False, "decision": _dec0,
                        "evaluated_epoch": time.time()}
            # Trade status gate for review: a closed market is
            # CLOSED (not settled, not a loss), a paused/inactive one
            # is REVIEW/paused, and both are distinct from missing.
            _trade = None; _trade_present = False
            if _mkt == "ml":
                _trade_present = ml_present; _trade = ml_status
            elif _mkt == "total":
                try:
                    _tk = float(_ln) if _ln is not None else None
                except Exception:
                    _tk = _ln
                _trade_present = _tk in totals_status; _trade = totals_status.get(_tk)
            elif _mkt == "spread":
                if _kind == "leak_plus" and _vs.get("original_team") is not None:
                    _trade_present = _vs.get("original_line") in spreads_status.get(_vs.get("original_team"), {})
                    _trade = spreads_status.get(_vs.get("original_team"), {}).get(_vs.get("original_line"))
                else:
                    _trade_present = _ln in spreads_status.get(_pick, {})
                    _trade = spreads_status.get(_pick, {}).get(_ln)
            if _trade_present and _trade == "closed":
                _dec_c = None
                if _NEW_MODULES:
                    try:
                        _dec_c = decide_candidate(
                            {"event_id": str(gid), "market": _mkt, "pick": _pick, "line": _ln,
                             "kind": _kind, "raw_prob": None, "raw_price": None},
                            {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                             "lead": lead, "tot": tot, "elapsed_min": elapsed, "remaining_min": t,
                             "home_abbr": hab, "away_abbr": aab},
                            process, None, None, market_closed=True)
                    except Exception as _e:
                        _dec_c = {"state": "CLOSED", "reason_codes": ["market_closed"],
                                  "reasons_text": [f"复查异常：{type(_e).__name__}"]}
                return {"signal_id": sig.get("signal_id"), "market": _mkt, "pick": _pick, "line": _ln,
                        "kind": _kind, "threshold_at_send": float(_thresh) if _thresh is not None else None,
                        "raw_prob": None, "raw_price": None, "bid": None, "ask": None,
                        "data_status": "closed", "decision": _dec_c,
                        "evaluated_epoch": time.time()}
            if _trade_present and _trade in ("paused", "inactive"):
                _dec_p = None
                if _NEW_MODULES:
                    try:
                        _dec_p = decide_candidate(
                            {"event_id": str(gid), "market": _mkt, "pick": _pick, "line": _ln,
                             "kind": _kind, "raw_prob": None, "raw_price": None, "data_status": "paused"},
                            {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                             "lead": lead, "tot": tot, "elapsed_min": elapsed, "remaining_min": t,
                             "home_abbr": hab, "away_abbr": aab},
                            process, None, None)
                    except Exception as _e:
                        _dec_p = {"state": "REVIEW", "reason_codes": ["market_paused"],
                                  "reasons_text": [f"复查异常：{type(_e).__name__}"]}
                return {"signal_id": sig.get("signal_id"), "market": _mkt, "pick": _pick, "line": _ln,
                        "kind": _kind, "threshold_at_send": float(_thresh) if _thresh is not None else None,
                        "raw_prob": None, "raw_price": None, "bid": None, "ask": None,
                        "data_status": "paused", "decision": _dec_p,
                        "evaluated_epoch": time.time()}
            if _mkt == "ml" and ml and sd_m:
                _is_home = (_pick == hab) or (team_names and len(team_names) == 2 and _pick == team_names[1])
                _is_away = (_pick == aab) or (team_names and len(team_names) == 2 and _pick == team_names[0])
                if _is_home:
                    _prob, _price, _bid, _ask = wp_home, 1 - ml[0], 1 - ml[1], 1 - ml[0]
                elif _is_away:
                    _prob, _price, _bid, _ask = 1 - wp_home, ml[1], ml[0], ml[1]
                else:
                    _data_status = "missing"
            elif _mkt == "spread":
                if _kind == "leak_plus" and _vs.get("original_team") and _vs.get("original_line") is not None:
                    if ml_present and ml_status in ("closed", "paused", "inactive"):
                        _data_status = "comparison_unavailable"
                    _ot_team, _ot_ln = _vs["original_team"], _vs["original_line"]
                    _found = None
                    for _ln2, _b2, _a2 in spreads.get(_ot_team, []):
                        if float(_ln2) == float(_ot_ln):
                            _found = (_b2, _a2); break
                    if _data_status == "comparison_unavailable":
                        pass
                    elif _found is None or not ml:
                        _data_status = "missing"
                    else:
                        _b2, _a2 = _found
                        _is_away2 = bool(team_names) and _ot_team == team_names[0]
                        _mean2 = (-lead - hm * t / 48) if _is_away2 else (lead + hm * t / 48)
                        _p2 = Phi((_mean2 + float(_ot_ln)) / sd_m) if sd_m else 0.5
                        _prob, _price, _bid, _ask = 1 - _p2, 1 - _b2, 1 - _a2, 1 - _b2
                        _ml_a = ml[1] if _pick in (aab, team_names[0] if team_names else None) else 1 - ml[0]
                        if _prob < _thresh:
                            _base_false = True
                        elif _price > _ml_a:
                            _inversion_broken = True
                            _base_false = True
                else:
                    _found = None
                    for _ln2, _b2, _a2 in spreads.get(_pick, []):
                        if _ln2 is not None and float(_ln2) == float(_ln):
                            _found = (_b2, _a2); break
                    if _found is None:
                        _data_status = "missing"
                    else:
                        _b2, _a2 = _found
                        _is_away3 = bool(team_names) and _pick == team_names[0]
                        _mean3 = (-lead - hm * t / 48) if _is_away3 else (lead + hm * t / 48)
                        _prob, _price, _bid, _ask = (Phi((_mean3 + float(_ln)) / sd_m) if sd_m else 0.5), _a2, _b2, _a2
            elif _mkt == "total":
                _ta = totals.get(float(_ln)) if _ln is not None else None
                if _ta is None:
                    _data_status = "missing"
                else:
                    _b3, _a3 = _ta
                    _p_over = Phi((mu - float(_ln)) / sd_t) if sd_t else 0.5
                    if _pick == "Over":
                        _prob, _price, _bid, _ask = _p_over, _a3, _b3, _a3
                        # Margin is a HIT entry rule only. A LEAK was
                        # never required to be >=10 from the projection;
                        # applying it in review invents "just pushed,
                        # already invalid" alerts on unchanged snapshots.
                        # Old 69fbb1d leak records that stored
                        # min_margin=10 are therefore ignored here:
                        # the kind, not the stored value, decides.
                        if _kind == "hit" and mu - float(_ln) < TOTALS_MIN_MARGIN:
                            _base_false = True
                    elif _pick == "Under":
                        _ub3, _ua3 = normalize_under_quote(_b3, _a3) if _NEW_MODULES else (1 - _a3, 1 - _b3)
                        _prob, _price, _bid, _ask = 1 - _p_over, _ua3, _ub3, _ua3
                        if _kind == "hit" and float(_ln) - mu < TOTALS_MIN_MARGIN:
                            _base_false = True
                    else:
                        _data_status = "missing"
                    if _kind in ("leak", "leak_plus") and _data_status == "ok" and _vs.get("line") is not None:
                        _cmp = totals.get(float(_vs["line"]))
                        _cmp_status = totals_status.get(float(_vs["line"]))
                        if _cmp is None:
                            _data_status = "missing"
                        elif _cmp_status in ("closed", "paused", "inactive"):
                            _data_status = "comparison_unavailable"
                        else:
                            _cmp_ask = (1 - _cmp[0]) if _pick == "Under" else _cmp[1]
                            if not (_price < _cmp_ask - 0.005):
                                _inversion_broken = True
                                _base_false = True
            else:
                _data_status = "missing"
            _dec = None
            if _NEW_MODULES:
                try:
                    _dec = decide_candidate(
                        {"event_id": str(gid), "market": _mkt, "pick": _pick, "line": _ln,
                         "kind": _kind, "raw_prob": _prob, "raw_price": _price,
                         "tracked_threshold": float(_thresh), "base_condition_false": _base_false,
                         "inversion_broken": _inversion_broken,
                         "data_status": _data_status if _data_status in ("missing", "comparison_unavailable") else None},
                        {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                         "lead": lead, "tot": tot, "elapsed_min": elapsed, "remaining_min": t,
                         "home_abbr": hab, "away_abbr": aab},
                        process, None,
                        {"status": "unknown", "reason": "no_book_or_no_target", "avg_price": None,
                         "filled_usd": None, "filled_shares": None, "fill_complete": None,
                         "cost_per_share_incl_fee": None})
                except Exception as _e:
                    _dec = {"state": "REVIEW", "reason_codes": ["context_error"],
                            "reasons_text": [f"复查异常：{type(_e).__name__}"], "missing": [],
                            "raw_probability": _prob, "calibrated_probability": None,
                            "version": "fallback", "policy_version": POLICY_VERSION, "model_version": MODEL_VERSION}
            return {"signal_id": sig.get("signal_id"), "market": _mkt, "pick": _pick, "line": _ln,
                    "kind": _kind, "threshold_at_send": float(_thresh), "raw_prob": _prob,
                    "raw_price": _price, "bid": _bid, "ask": _ask, "data_status": _data_status,
                    "base_condition_false": _base_false, "inversion_broken": _inversion_broken,
                    "decision": _dec,
                    "evaluated_epoch": time.time()}
        except Exception as _e:
            return {"signal_id": sig.get("signal_id"), "market": sig.get("market"), "pick": sig.get("pick"),
                    "line": sig.get("line"), "kind": sig.get("kind"), "data_status": "error",
                    "decision": {"state": "REVIEW", "reason_codes": ["context_error"],
                                 "reasons_text": [f"复查异常：{type(_e).__name__}"]},
                    "evaluated_epoch": time.time()}
    # v1 context (bypass): process windows, market windows, decisions.
    # Probabilities are never changed here; decisions only annotate.
    process = None
    decisions = []
    if _NEW_MODULES:
        try:
            _hdir = _history_dir()
            _gh = SnapshotHistory(_hdir, gid)
            _prev_game = _gh.load()
            _cur_snap = {"elapsed_min": elapsed, "lead": lead, "tot": tot,
                         "home_score": hs, "away_score": as_, "period": per, "clock": clk}
            _wins = window_features(_prev_game, _cur_snap)
            _tags = risk_tags({**_cur_snap, "remaining_min": t, "is_ot": is_ot}, _wins)
            process = {"windows": _wins, "risk_tags": _tags}
            _gh.append(_cur_snap)
            _target = _target_usd()
            _fee = _fee_per_share()
            _cost_deadline = time.time() + TRACKED_COST_TIMEOUT_SEC
            _shown_pre = set(leaks_sorted[:3]) | {h for _, h in shown}
            for _c in cands:
                # Market history is per game AND per candidate: two games
                # with the same totals line must never share a history file.
                _mkey = f"{gid}|{_c['market']}|{_c['pick']}|{_c['line']}"
                _mwin = None
                _mismatch = False
                _cost = {"status": "unknown", "reason": "no_book_or_no_target", "avg_price": None,
                         "filled_usd": None, "filled_shares": None, "fill_complete": None,
                         "cost_per_share_incl_fee": None}
                try:
                    _mh = MarketHistory(_hdir, _mkey)
                    _prev_m_all = _mh.load()
                    _ident = _c.get("identity") or {}
                    _cond_now = _ident.get("condition_id")
                    # F2: a contract change must not silently reuse the old
                    # contract's window, and a pre-fix Under snapshot (wrong
                    # bid) must not pollute a post-fix window.
                    _mismatch = bool(_cond_now and any(
                        s.get("condition_id") and s.get("condition_id") != _cond_now for s in _prev_m_all))
                    _prev_m = [s for s in _prev_m_all
                               if (not s.get("condition_id") or s.get("condition_id") == _cond_now)]
                    if _c["market"] == "total" and _c["pick"] == "Under":
                        _prev_m = [s for s in _prev_m if s.get("schema_version", 0) >= MARKET_SCHEMA_VERSION]
                    _cur_msnap = {"received_epoch": _gamma_received_epoch, "bid": _c["bid"], "ask": _c["ask"],
                                  "price": _c["raw_price"], "last": None, "schema_version": MARKET_SCHEMA_VERSION,
                                  "home_score": hs, "away_score": as_, "condition_id": _cond_now}
                    _mwin = market_window_features(_prev_m, _cur_msnap) if _prev_m else None
                    if _mwin is None:
                        _mwin = {30: {"status": "unknown", "reason": "no_history", "coverage_status": "unknown"},
                                 120: {"status": "unknown", "reason": "no_history", "coverage_status": "unknown"}}
                    _mh.append(_cur_msnap)
                    # F5: real book only for shown totals candidates, one
                    # request per candidate, inside a total time budget.
                    # ML/spread token mapping is NOT verified, so their
                    # cost stays unknown rather than guessed.
                    if _target is not None and _c["text"] in _shown_pre:
                        if _c["market"] == "total":
                            _tok = token_for_total(_ident, _c["pick"]) if _NEW_MODULES else None
                            if _tok is None:
                                _cost = {"status": "unknown", "book_status": "unknown", "reason": "identity_unknown",
                                         "avg_price": None, "filled_usd": None, "filled_shares": None,
                                         "fill_complete": None, "cost_per_share_incl_fee": None,
                                         "source": "polymarket_clob", "source_note": "Polymarket book only; Gate execution cost not verified"}
                            elif time.time() >= _cost_deadline:
                                _cost = {"status": "unknown", "book_status": "unknown", "reason": "cost_budget_exhausted",
                                         "avg_price": None, "filled_usd": None, "filled_shares": None,
                                         "fill_complete": None, "cost_per_share_incl_fee": None,
                                         "source": "polymarket_clob", "source_note": "Polymarket book only; Gate execution cost not verified"}
                            else:
                                _asks, _bids = fetch_book(_tok, timeout=max(1.0, min(10.0, _cost_deadline - time.time())))
                                if _asks is None:
                                    _cost = {"status": "unknown", "book_status": "unknown", "reason": "book_fetch_failed",
                                             "avg_price": None, "filled_usd": None, "filled_shares": None,
                                             "fill_complete": None, "cost_per_share_incl_fee": None,
                                             "source": "polymarket_clob", "source_note": "Polymarket book only; Gate execution cost not verified"}
                                else:
                                    _cost = cost_for_target(_asks, _target, fee_per_share=_fee)
                                    _cost["source"] = "polymarket_clob"
                                    _cost["source_note"] = "Polymarket book only; Gate execution cost not verified"
                                    _cost["received_epoch"] = time.time()
                                    _cost["token_id"] = str(_tok)
                        else:
                            _cost = {"status": "unknown", "book_status": "unknown", "reason": "cost_not_wired_for_market",
                                     "avg_price": None, "filled_usd": None, "filled_shares": None,
                                     "fill_complete": None, "cost_per_share_incl_fee": None,
                                     "source": "polymarket_clob", "source_note": "Polymarket book only; Gate execution cost not verified"}
                except Exception:
                    pass
                _c["market_windows"] = _mwin
                _c["cost"] = _cost
                try:
                    _dec = decide_candidate(
                        {"event_id": str(gid), "market": _c["market"], "pick": _c["pick"],
                         "line": _c["line"], "kind": _c["kind"], "raw_prob": _c["raw_prob"],
                         "raw_price": _c["raw_price"], "identity_mismatch": _mismatch,
                         "base_condition_false": False},
                        {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                         "lead": lead, "tot": tot, "elapsed_min": elapsed, "remaining_min": t,
                         "home_abbr": hab, "away_abbr": aab},
                        process,
                        {"windows": _mwin, "received_age_sec": max(0.0, time.time() - _gamma_received_epoch)} if _mwin else None,
                        _cost)
                except Exception as _e:
                    _dec = {"state": "REVIEW", "reason_codes": ["context_error"],
                            "reasons_text": [f"决策异常：{type(_e).__name__}"], "missing": [],
                            "raw_probability": _c["raw_prob"], "calibrated_probability": None,
                            "version": "fallback", "policy_version": POLICY_VERSION, "model_version": MODEL_VERSION}
                _c["decision"] = _dec
                decisions.append(_dec)
        except Exception:
            process = None
            decisions = []
    # F1: re-evaluate tracked signals from this round's data, even
    # when they are no longer new-push candidates. Missing from the
    # book this round is a REVIEW (data_status=missing), not a loss.
    if _NEW_MODULES:
        try:
            for _sig in _load_tracked_raw():
                tracked_evaluations.append(_eval_tracked(_sig))
        except Exception:
            pass
    # Shown flag: a candidate is shown iff its text is in the displayed
    # leak (<=3) or hit lists. Unshown candidates are still logged below.
    _shown_texts = set(leaks_sorted[:3]) | {h for _, h in shown}
    for _c in cands:
        _c["shown"] = _c["text"] in _shown_texts
    # Structured sidecar for the sender (bypass: text remains the push path).
    _out_path = os.environ.get("FC_STRUCTURED_OUT")
    if _out_path:
        try:
            _struct = {
                "event_id": str(gid), "slug": slug, "status": "live",
                "game": {"home_score": hs, "away_score": as_, "period": per, "clock": clk,
                         "home_abbr": hab, "away_abbr": aab, "lead": lead, "tot": tot,
                         "elapsed_min": elapsed, "remaining_min": t, "is_ot": is_ot,
                         "wp_home": wp_home, "proj_total": mu, "ot_prob": ot, "ot_scenario_total": ot_scenario,
                         "totals_ok": totals_ok, "hit_ok": hit_ok},
                "candidates_all": cands, "process": process, "decisions": decisions,
                "tracked_evaluations": tracked_evaluations,
                "blocked_markets": blocked_markets,
                "policy_version": POLICY_VERSION if _NEW_MODULES else "legacy",
                "model_version": MODEL_VERSION if _NEW_MODULES else "legacy",
                "scan_id": os.environ.get("FC_SCAN_ID"),
                "gamma_received_epoch": _gamma_received_epoch,
                "received_epoch": time.time(),
            }
            _tmp = _out_path + ".tmp"
            with open(_tmp, "w") as _f:
                json.dump(_struct, _f, ensure_ascii=False, default=str)
            os.replace(_tmp, _out_path)
        except Exception:
            pass
    # Experience bank v1: log EVERY candidate as detected (full precision,
    # shown flag), never as delivered. Sender marks SENT after TG ok.
    # Logging must never break a scan.
    try:
        import experience as _exp
        _ctx = {"date_us": DATE_US, "event_id": str(gid), "game": f"{aab}@{hab}",
                "score": f"{aab} {as_}:{hs} {hab}", "clock": f"Q{per} {clk}"}
        for _c in cands:
            _dec = _c.get("decision") or {}
            _exp.log_detected({**_ctx,
                "market": _c["market"], "pick": _c["pick"], "line": _c["line"], "kind": _c["kind"],
                "price_c": round(_c["raw_price"] * 100, 1) if _c["raw_price"] is not None else None,
                "raw_price": _c["raw_price"], "prob": _c["raw_prob"], "raw_prob": _c["raw_prob"],
                "shown": bool(_c.get("shown")), "decision_state": _dec.get("state"),
                "reason_codes": _dec.get("reason_codes"), "result": None})
    except Exception:
        pass
    return "\n".join(out)

def main():
    global DATE_US
    date = sys.argv[1]
    DATE_US = date
    specs = []
    for s in sys.argv[2:]:
        gid, slug, hm = s.split(':')
        specs.append((gid, slug, float(hm)))
    t0 = time.time()
    sb = get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}")
    states = {}
    for e in sb['events']:
        c = e['competitions'][0]; st = c['status']
        comp = {x['homeAway']: x for x in c['competitors']}
        # Preserve the raw clock: a missing/null/empty displayClock
        # must reach analyze() as None so it becomes DATA_ERROR, never
        # a fabricated '0:00' (which would look like period end).
        _raw_clk = st.get('displayClock')
        if _raw_clk is None or (isinstance(_raw_clk, str) and _raw_clk.strip() == ""):
            _raw_clk = None
        elif not isinstance(_raw_clk, str):
            _raw_clk = str(_raw_clk)
        states[e['id']] = (int(comp['home'].get('score') or 0), int(comp['away'].get('score') or 0),
                           st.get('period'), _raw_clk,
                           comp['home']['team']['abbreviation'], comp['away']['team']['abbreviation'],
                           st['type']['description'])
    with ThreadPoolExecutor(max_workers=len(specs)) as ex:
        futs = {ex.submit(analyze, gid, slug, hm, states[gid]): gid for gid, slug, hm in specs if gid in states}
        for f in futs:
            try:
                print(f.result())
            except Exception as ex_:
                print(f"{futs[f]}: fetch failed {ex_}")
    print(f"[fast_check total {time.time()-t0:.1f}s]")

if __name__ == '__main__':
    main()
