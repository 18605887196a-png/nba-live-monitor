#!/usr/bin/env python3
"""fast_check.py - parallel ESPN scores + gamma books, tiered output.

Tiering (user, 2026-10-24):
- MAIN: any ML/spread/total line with model prob >= 90% (totals also need |proj-line| >= 10)
- AUX: ladder inversion (any size >= 0.5c) with real two-sided quotes and
  easier-line model prob >= 85%; below 85% is filtered out in code.
Output (user, 2026-10-24): conclusion-first — a 结论 line names the single
top pick (best leak, else best hit); leaks print with a 【倒挂】 marker and
stay first; no model-vs-market gap analysis is printed.

Usage: python3 fast_check.py <US date YYYYMMDD> <event_id:slug:home_margin> [...]
home_margin: pregame expected full-game margin for home team (positive if home favored).
"""
import json, math, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

DATE_US = None  # set by main(); used as the experience-bank date key

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
    try:
        if ':' in clk:
            mm, ss = clk.split(':'); clock = float(mm) + float(ss) / 60
        else:
            clock = float(clk) / 60  # ESPN shows bare seconds ("43.5") under a minute
    except Exception:
        clock = 0.0
    t = (4 - per) * 12 + clock if per <= 4 else clock
    elapsed = 48 - t
    lead = hs - as_
    sd_m = 13 * math.sqrt(t / 48) if t >= 0.5 else 0.0
    wp_home = Phi((lead + hm * t / 48) / sd_m) if sd_m else 0.5
    tot = hs + as_
    totals_ok = elapsed >= 15 and tot > 0
    pace = (tot + 4.71 * 12) / (elapsed + 12) if elapsed > 0 else 4.71
    mu = tot + pace * t
    sd_t = min(2.0 * math.sqrt(pace * t), 21) if t > 0 else 1.0

    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")[0]
    title = ev['title']
    totals, spreads, ml = {}, {}, None
    team_names = []
    for m in ev['markets']:
        q = m.get('question', ''); b = m.get('bestBid'); a = m.get('bestAsk'); ln = m.get('line')
        if q == title:
            try:
                team_names = json.loads(m.get('outcomes') or '[]')
            except Exception:
                team_names = []
            if b is not None and a is not None:
                ml = (b, a)
        if ln is None or b is None or a is None or b <= 0.01 or a >= 0.999:
            continue
        if 'O/U' in q and 'vs.' in q and not any(x in q for x in ('1H', '1Q', '2Q', '3Q', '4Q')):
            totals[ln] = (b, a)
        if q.startswith('Spread:'):
            team = q.replace('Spread: ', '').rsplit(' (', 1)[0]
            spreads.setdefault(team, []).append((ln, b, a))

    hits, leaks = [], []
    if ml and sd_m:
        if 1 - wp_home >= 0.90:
            hits.append((1 - wp_home, f"ML {aab} ask {fmt_price(ml[1])} prob {(1-wp_home)*100:.0f}%"))
        if wp_home >= 0.90:
            hits.append((wp_home, f"ML {hab} ask {fmt_price(1-ml[0])} prob {wp_home*100:.0f}%"))
    cover = {}
    for team, arr in spreads.items():
        is_away = bool(team_names) and team == team_names[0]
        mean_m = (-lead - hm * t / 48) if is_away else (lead + hm * t / 48)
        for ln, b, a in arr:
            if sd_m:
                p = Phi((mean_m + ln) / sd_m)
                cover[(team, ln)] = (p, a)
                if p >= 0.90:
                    hits.append((p, f"Spread {team} ({ln:+.1f}) ask {fmt_price(a)} cover {p*100:.0f}%"))
                # Plus-line flip (user, 2026-10-24): the No side of a negative spread line is the
                # other team GETTING points (No on Clippers -3.5 == Lakers +3.5). It strictly
                # dominates that team's moneyline, so ask <= ML ask at prob >= 85% = top leak.
                if ln < 0 and ml and len(team_names) == 2 and b and b >= 0.02:
                    other = team_names[1] if team == team_names[0] else team_names[0]
                    p_no = 1 - p
                    ask_no = 1 - b
                    ml_a = ml[1] if other == team_names[0] else 1 - ml[0]
                    if p_no >= 0.85 and ask_no <= ml_a and ask_no < 0.999:
                        leaks.append(f"LEAK Plus {other} ({-ln:+.1f}) ask {fmt_price(ask_no)} vs ML {fmt_price(ml_a)}: easier than ML, cheaper-or-equal, prob {p_no*100:.0f}%")
    for ln, (b, a) in totals.items():
        p_over = Phi((mu - ln) / sd_t)
        if totals_ok and p_over >= 0.90 and mu - ln >= 10:
            hits.append((p_over, f"Total Over {ln} ask {fmt_price(a)} prob {p_over*100:.0f}% (now {tot:.0f}, need {ln-tot:.0f} more, proj {mu:.0f})"))
        if totals_ok and 1 - p_over >= 0.90 and ln - mu >= 10:
            hits.append((1 - p_over, f"Total Under {ln} ask {fmt_price(1-b)} prob {(1-p_over)*100:.0f}% (now {tot:.0f}, cushion {ln-tot:.0f}, proj {mu:.0f})"))
    ls = sorted(totals)
    for i in range(len(ls) - 1):
        l1, l2 = ls[i], ls[i + 1]
        ua1, ua2 = 1 - totals[l1][0], 1 - totals[l2][0]
        oa1, oa2 = totals[l1][1], totals[l2][1]
        if ua2 < ua1 - 0.005:
            p = 1 - Phi((mu - l2) / sd_t)
            if totals_ok and p >= 0.85:
                leaks.append(f"LEAK Under {l2} ask {fmt_price(ua2)} vs Under {l1} {fmt_price(ua1)}: easier+cheaper by {100*(ua1-ua2):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, cushion {l2-tot:.0f}, proj {mu:.0f})")
        if oa2 > oa1 + 0.005:
            p = Phi((mu - l1) / sd_t)
            if totals_ok and p >= 0.85:
                leaks.append(f"LEAK Over {l1} ask {fmt_price(oa1)} vs Over {l2} {fmt_price(oa2)}: easier+cheaper by {100*(oa2-oa1):.0f}c, prob {p*100:.0f}% (now {tot:.0f}, need {l1-tot:.0f} more, proj {mu:.0f})")
    for team, arr in spreads.items():
        arr2 = sorted(arr)
        for i in range(len(arr2) - 1):
            ln1, b1, a1 = arr2[i]
            ln2, b2, a2 = arr2[i + 1]
            if a2 < a1 - 0.005:
                p = cover.get((team, ln2), (0, 0))[0]
                if p >= 0.85:
                    leaks.append(f"LEAK Spread {team} ({ln2:+.1f}) ask {fmt_price(a2)} vs ({ln1:+.1f}) {fmt_price(a1)}: easier+cheaper by {100*(a1-a2):.0f}c, cover {p*100:.0f}%")

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
    # Experience bank: log every line we are about to show; first sighting of a
    # line wins, re-scans never overwrite it. Logging must never break a scan.
    try:
        import experience as _exp
        _ctx = {"date_us": DATE_US, "event_id": str(gid), "game": f"{aab}@{hab}",
                "score": f"{aab} {as_}:{hs} {hab}", "clock": f"Q{per} {clk}"}
        # Early-game gate (user 2026-10-25): Q1/Q2 regular hits are logged-only
        # (delivered=False) when the sender sets FC_EARLY_LOG_ONLY; leaks are
        # always sent (fleeting arbitrage) so they stay delivered=True.
        _early_gated = os.environ.get("FC_EARLY_LOG_ONLY") == "1" and per <= 2
        for _t in leaks_sorted[:3]:
            _r = _parse_rec(_t, _ctx, "leak", delivered=True)
            if _r:
                _exp.log_push(_r)
        for _, _t in shown:
            _r = _parse_rec(_t, _ctx, "hit", delivered=not _early_gated)
            if _r:
                _exp.log_push(_r)
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
        states[e['id']] = (int(comp['home'].get('score') or 0), int(comp['away'].get('score') or 0),
                           st.get('period'), st.get('displayClock') or '0:00',
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
