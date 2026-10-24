#!/usr/bin/env python3
"""fast_check.py — 一次并行拉齐 ESPN 比分 + gamma 三盘 + 梯子倒挂扫描。

用法: python3 fast_check.py <美国日期YYYYMMDD> <event_id:slug:home_margin> [...]
输出每场一行摘要 + 达标/倒挂明细。设计目标: 全部场次并行, 总耗时 <5s。
home_margin: 主队赛前预期全场分差(主队热门为正, 客队热门为负)。
"""
import json, math, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

def get(u, timeout=12):
    r = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=timeout))

def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def analyze(gid, slug, hm, state):
    hs, as_, per, clk, hab, aab, desc = state
    if desc != 'In Progress':
        return f"{aab}@{hab}: {desc} {as_}:{hs}"
    try:
        mm, ss = clk.split(':'); clock = float(mm) + float(ss) / 60
    except Exception:
        clock = 0.0
    t = (4 - per) * 12 + clock if per <= 4 else clock
    lead = hs - as_
    wp = Phi((lead + hm * t / 48) / (13 * math.sqrt(t / 48))) if t >= 0.5 else 0.5
    ev = get(f"https://gamma-api.polymarket.com/events?slug={slug}")[0]
    title = ev['title']
    totals, spreads, ml = {}, {}, None
    for m in ev['markets']:
        q = m.get('question', ''); b = m.get('bestBid'); a = m.get('bestAsk'); ln = m.get('line')
        if q == title and b is not None and a is not None:
            ml = (b, a)  # outcomes[0]=客队(标题首队), ask=a; 主队 ask=1-b
        if ln is None or b is None or a is None or b <= 0.01 or a >= 0.999:
            continue
        if 'O/U' in q and 'vs.' in q and not any(x in q for x in ('1H', '1Q', '2Q', '3Q', '4Q')):
            totals[ln] = (b, a)
        if q.startswith('Spread:'):
            team = q.replace('Spread: ', '').rsplit(' (', 1)[0]
            spreads.setdefault(team, []).append((ln, a))
    out = [f"{aab} {as_} @ {hab} {hs} | Q{per} {clk} | 胜率 {aab} {(1-wp)*100:.0f}% / {hab} {wp*100:.0f}%"]
    if ml:
        out.append(f"  胜负卖价: {aab} {ml[1]*100:.0f}c(倍率{1/ml[1]:.2f}) | {hab} {(1-ml[0])*100:.0f}c(倍率{1/(1-ml[0]):.2f})")
    ls = sorted(totals)
    for i in range(len(ls) - 1):
        l1, l2 = ls[i], ls[i + 1]
        ua1, ua2 = 1 - totals[l1][0], 1 - totals[l2][0]
        oa1, oa2 = totals[l1][1], totals[l2][1]
        if ua1 >= 0.5 and ua2 < ua1 - 0.005:
            out.append(f"  !! 小分倒挂: Under{l2} {ua2*100:.0f}c(倍率{1/ua2:.2f}) 比 Under{l1} {ua1*100:.0f}c 便宜{100*(ua1-ua2):.0f}c 还更好中")
        if oa2 >= 0.5 and oa2 > oa1 + 0.005:
            out.append(f"  !! 大分倒挂: Over{l1} {oa1*100:.0f}c(倍率{1/oa1:.2f}) 比 Over{l2} {oa2*100:.0f}c 更易中还便宜{100*(oa2-oa1):.0f}c")
    for team, arr in spreads.items():
        arr.sort()
        for i in range(len(arr) - 1):
            if arr[i + 1][1] < arr[i][1] - 0.005 and arr[i][1] >= 0.3:
                out.append(f"  !! 让分倒挂: {team} {arr[i+1][0]} 现价 {arr[i+1][1]*100:.0f}c(倍率{1/arr[i+1][1]:.2f}) 比 {arr[i][0]} 现价 {arr[i][1]*100:.0f}c 更易中还便宜{100*(arr[i][1]-arr[i+1][1]):.0f}c")
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
                print(f"{futs[f]}: 拉数失败 {ex_}")
    print(f"[fast_check 总耗时 {time.time()-t0:.1f}s]")

if __name__ == '__main__':
    main()
