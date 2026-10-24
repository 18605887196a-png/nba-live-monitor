#!/usr/bin/env python3
"""fast_check.py - parallel ESPN scores + gamma books, tiered output.

Tiering (user, 2026-10-24):
- MAIN: any ML/spread/total line with model prob >= 90% (totals also need |proj-line| >= 10)
- AUX: ladder inversion (any size >= 0.5c) with real two-sided quotes and
  easier-line model prob >= 85%; below 85% is filtered out in code.

Usage: python3 fast_check.py <US date YYYYMMDD> <event_id:slug:home_margin> [...]
home_margin: pregame expected full-game margin for home team (positive if home favored).
"""
import json, math, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

def get(u, timeout=12):
    r = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=timeout))

def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def fmt_price(p):
    return f"{p*100:.0f}c(x{1/p:.2f})" if 0 < p < 1 else f"{p*100:.0f}c"

def analyze(gid, slug, hm, state):
    hs, as_, per, clk, hab, aab, desc = state
    if desc in ('Final', 'Scheduled', 'Postponed', 'Canceled'):
        return f"{aab}@{hab}: {desc} {as_}:{hs}"
    try:
        mm, ss = clk.split(':'); clock = float(mm) + float(ss) / 60
    except Exception:
        clock = 0.0
    t = (4 - per) * 12 + clock if per <= 4 else clock
    elapsed = 48 - t
    lead = hs - as_
    sd_m = 13 * math.sqrt(t / 48) if t >= 0.5 else 0.0
    wp_home = Phi((lead + hm * t / 48) / sd_m) if sd_m else 0.5
    tot = hs + as_
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
    for ln, (b, a) in totals.items():
        p_over = Phi((mu - ln) / sd_t)
        if p_over >= 0.90 and mu - ln >= 10:
            hits.append((p_over, f"Total Over {ln} ask {fmt_price(a)} prob {p_over*100:.0f}% (proj {mu:.0f})"))
        if 1 - p_over >= 0.90 and ln - mu >= 10:
            hits.append((1 - p_over, f"Total Under {ln} ask {fmt_price(1-b)} prob {(1-p_over)*100:.0f}% (proj {mu:.0f})"))
    ls = sorted(totals)
    for i in range(len(ls) - 1):
        l1, l2 = ls[i], ls[i + 1]
        ua1, ua2 = 1 - totals[l1][0], 1 - totals[l2][0]
        oa1, oa2 = totals[l1][1], totals[l2][1]
        if ua2 < ua1 - 0.005:
            p = 1 - Phi((mu - l2) / sd_t)
            if p >= 0.85:
                leaks.append(f"LEAK Under {l2} ask {fmt_price(ua2)} vs Under {l1} {fmt_price(ua1)}: easier+cheaper by {100*(ua1-ua2):.0f}c, prob {p*100:.0f}%")
        if oa2 > oa1 + 0.005:
            p = Phi((mu - l1) / sd_t)
            if p >= 0.85:
                leaks.append(f"LEAK Over {l1} ask {fmt_price(oa1)} vs Over {l2} {fmt_price(oa2)}: easier+cheaper by {100*(oa2-oa1):.0f}c, prob {p*100:.0f}%")
    for team, arr in spreads.items():
        arr2 = sorted(arr)
        for i in range(len(arr2) - 1):
            ln1, b1, a1 = arr2[i]
            ln2, b2, a2 = arr2[i + 1]
            if a2 < a1 - 0.005:
                p = cover.get((team, ln2), (0, 0))[0]
                if p >= 0.85:
                    leaks.append(f"LEAK Spread {team} ({ln2:+.1f}) ask {fmt_price(a2)} vs ({ln1:+.1f}) {fmt_price(a1)}: easier+cheaper by {100*(a1-a2):.0f}c, cover {p*100:.0f}%")

    out = [f"{aab} {as_} @ {hab} {hs} | Q{per} {clk} | WP {aab} {(1-wp_home)*100:.0f}% / {hab} {wp_home*100:.0f}% | proj total {mu:.0f}"]
    hits.sort(reverse=True)
    for _, h in hits[:4]:
        out.append(f"  [HIT] {h}")
    for lk in leaks[:3]:
        out.append(f"  [{lk}]")
    if not hits and not leaks:
        out.append("  no hit / no leak")
    return "\n".join(out)

def main():
    date = sys.argv[1]
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
