#!/usr/bin/env python3
"""sender.py - TG-direct push sender (trial, 2026-10-25).

Polls fast_check.py for one game and sends a Telegram message ONLY when a
line newly qualifies (transition). No agent in the loop, so there is no
relay agent to die: the script itself is the sender.

Rules (match the user's push format):
- Trigger = newly qualified MAIN item (leak / ML / total). Spread hits
  never trigger a send alone (attach-only); they ride along only in the
  cycle they newly qualify together with a main trigger.
- Re-crossing counts as new: an item that unqualified then qualifies
  again is sent again.
- Message: state line, conclusion (top newly qualified item, leaks first
  with a 【倒挂】 marker), other newly qualified mains, spread attaches.
- First line carries 共多少分 + 预测全场多少分; totals lines carry only 离盘口差多少 (conclusion adds 加时概率);
 达标盘 carry a Gate 核价线 (price+5c, capped at 100c).
- State file persists the qualified set so a restart does not resend.
- A failed TG send does NOT advance the state, so the next cycle retries.
- Exits on Final; waits through Scheduled/Delayed (pregame).
- Fixed nodes (minimum guarantee): at Q3 end (first cycle with period 4)
  and at Q4 6:00 (first cycle with period 4 and clock <= 6:00), the sender
  ALWAYS sends a node message, even if no line qualifies (then a one-line
  '暂无够格线' status), so every game gets its node broadcasts. Node flags
  persist in the state file; a failed node send retries next cycle.
- Priority (user 2026-10-26): main pushes > nodes > risk updates.
  Risk updates are lowest priority and gated by decision.risk_send_allowed:
  INVALIDATED always, REVIEW only in Q4 late (remaining <= 5 min) or when
  raw_prob fell below its push threshold; early-game REVIEW (e.g. Q3) is
  logged only (RISK held), never tg-sent, never SENT_RISK_MSG.

Usage:
  sender.py <US date YYYYMMDD> <event_id:slug:home_margin>
    [--chat ID] [--interval SECS] [--state FILE] [--log FILE]
    [--dry-run] [--test-input FILE] [--once] [--max-hours H]
"""
import argparse, json, os, re, subprocess, sys, time

TEAM_CN = {
    "ATL": "老鹰", "BKN": "篮网", "BOS": "凯尔特人", "CHA": "黄蜂", "CHI": "公牛",
    "CLE": "骑士", "DAL": "独行侠", "DEN": "掘金", "DET": "活塞", "GSW": "勇士",
    "HOU": "火箭", "IND": "步行者", "LAC": "快船", "LAL": "湖人", "MEM": "灰熊",
    "MIA": "热火", "MIL": "雄鹿", "MIN": "森林狼", "NOP": "鹈鹕", "NYK": "尼克斯",
    "OKC": "雷霆", "ORL": "魔术", "PHI": "76人", "PHX": "太阳", "POR": "开拓者",
    "SAC": "国王", "SAS": "马刺", "TOR": "猛龙", "UTA": "爵士", "WAS": "奇才",
}
Q_CN = {1: "一", 2: "二", 3: "三", 4: "四"}
NAME_CN = {
    "Hawks": "老鹰", "Nets": "篮网", "Celtics": "凯尔特人", "Hornets": "黄蜂",
    "Bulls": "公牛", "Cavaliers": "骑士", "Mavericks": "独行侠", "Nuggets": "掘金",
    "Pistons": "活塞", "Warriors": "勇士", "Rockets": "火箭", "Pacers": "步行者",
    "Clippers": "快船", "Lakers": "湖人", "Grizzlies": "灰熊", "Heat": "热火",
    "Bucks": "雄鹿", "Timberwolves": "森林狼", "Pelicans": "鹈鹕", "Knicks": "尼克斯",
    "Thunder": "雷霆", "Magic": "魔术", "76ers": "76人", "Suns": "太阳",
    "Trail Blazers": "开拓者", "Kings": "国王", "Spurs": "马刺", "Raptors": "猛龙",
    "Jazz": "爵士", "Wizards": "奇才",
}
TG_PY = os.path.expanduser("~/workspace/custom-connectors/custom.telegram/bin/tg.py")
FC_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fast_check.py")
FC_DIR = os.path.dirname(os.path.abspath(__file__))

# v1 context: decision helpers are optional; sender works without them
# (text parsing remains the push path in bypass stage).
try:
    from decision import should_send_risk, format_context_block, risk_send_allowed
    _DECISION_OK = True
except Exception:
    _DECISION_OK = False

def cn(team):
    return TEAM_CN.get(team) or NAME_CN.get(team) or team

def mult(price_c):
    return 100.0 / price_c if price_c else 0.0

def ceil(price_c):
    return min(price_c + 5, 100)

def parse_price_prob(text):
    m = re.search(r"ask (\d+(?:\.\d+)?)c", text)
    price = float(m.group(1)) if m else None
    pm = re.search(r"(?:prob|cover) (\d+(?:\.\d+)?)%", text)
    prob = float(pm.group(1)) if pm else None
    return price, prob

def parse_item(text):
    """Return (key, info) or None. Key excludes price: a price move alone
    must not retrigger."""
    price, prob = parse_price_prob(text)
    mm = re.match(r"ML (\S+) ask", text)
    if mm:
        return ("hit", "ml", mm.group(1), None), {"price": price, "prob": prob, "text": text}
    mm = re.match(r"Spread (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return ("hit", "spread", mm.group(1), float(mm.group(2))), {"price": price, "prob": prob, "text": text}
    mm = re.match(r"Total (Over|Under) (\d+(?:\.\d+)?) ask", text)
    if mm:
        return ("hit", "total", mm.group(1), float(mm.group(2))), {"price": price, "prob": prob, "text": text}
    mm = re.match(r"LEAK Plus (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return ("leak_plus", "spread", mm.group(1), float(mm.group(2))), {"price": price, "prob": prob, "text": text}
    mm = re.match(r"LEAK (Over|Under) (\d+(?:\.\d+)?) ask", text)
    if mm:
        return ("leak", "total", mm.group(1), float(mm.group(2))), {"price": price, "prob": prob, "text": text}
    mm = re.match(r"LEAK Spread (.+) \(([+-]?\d+(?:\.\d+)?)\) ask", text)
    if mm:
        return ("leak", "spread", mm.group(1), float(mm.group(2))), {"price": price, "prob": prob, "text": text}
    return None

def totals_detail(text):
    """(tot, is_over, gap, proj) from a totals HIT/LEAK parenthetical."""
    m = re.search(r"\(now (\d+), (need \d+ more|cushion \d+), proj (\d+)\)", text)
    if not m:
        return None
    tot, mid, proj = m.group(1), m.group(2), m.group(3)
    if mid.startswith("need"):
        n = re.search(r"need (\d+) more", mid).group(1)
        return tot, True, n, proj
    c = re.search(r"cushion (\d+)", mid).group(1)
    return tot, False, c, proj

def totals_gap_phrase(line, is_over, n):
    """Explicit sum-gap wording: name the line so the gap is unmissable."""
    if is_over:
        return f"离 {line:g} 还差 {n} 分"
    return f"离 {line:g} 还能再得 {n} 分才爆"

def vs_note(text):
    """'比 X 更容易中还便宜 N¢' for a leak line, or None."""
    m = re.search(r"vs Under (\d+(?:\.\d+)?) .*?easier\+cheaper by (\d+)c", text)
    if m:
        return f"比 {m.group(1)} 更容易中还便宜 {m.group(2)}¢"
    m = re.search(r"vs Over (\d+(?:\.\d+)?) .*?easier\+cheaper by (\d+)c", text)
    if m:
        return f"比 {m.group(1)} 更容易中还便宜 {m.group(2)}¢"
    m = re.search(r"vs \(([+-]?\d+(?:\.\d+)?)\) .*?easier\+cheaper by (\d+)c", text)
    if m:
        return f"比 {float(m.group(1)):+.1f} 更容易中还便宜 {m.group(2)}¢"
    m = re.search(r"vs ML (\d+(?:\.\d+)?)c.*?: easier than ML, cheaper-or-equal", text)
    if m:
        ml_c = float(m.group(1))
        price, _ = parse_price_prob(text)
        diff = ml_c - (price or 0)
        if diff <= 0:
            return "和胜负同价但更容易中"
        return f"比胜负 {ml_c:.0f}¢ 更容易中还便宜 {diff:.0f}¢"
    return None

def fmt_main(key, info, is_conclusion, ot):
    """Format a leak/ML/total item for the message body."""
    kind, market, pick, line = key
    price, prob = info["price"], info["prob"]
    text = info["text"]
    ptxt = f"{price:.0f}¢（{mult(price):.2f}x）" if price else ""
    probtxt = f"模型 {prob:.0f}%" if prob is not None else ""
    is_leak = kind in ("leak", "leak_plus")
    mark = "【倒挂】" if is_leak else ""
    note = vs_note(text) if is_leak else None
    if market == "ml":
        body = f"{cn(pick)}胜负 @ {ptxt}，{probtxt}，核价线 {ceil(price):.0f}¢"
        return f"{mark}{body}" if not is_conclusion else f"买{body}"
    if market == "total":
        dcn = "大分" if pick == "Over" else "小分"
        det = totals_detail(text)
        if is_conclusion and det:
            _tot, is_over, n, _proj = det
            body = f"{dcn} {line:g} @ {ptxt}，{probtxt}（{totals_gap_phrase(line, is_over, n)}，加时概率 {ot}%），核价线 {ceil(price):.0f}¢"
        elif det:
            _tot, is_over, n, _proj = det
            body = f"{dcn} {line:g} @ {ptxt} {probtxt}（{totals_gap_phrase(line, is_over, n)}）"
        else:
            body = f"{dcn} {line:g} @ {ptxt} {probtxt}"
        if note:
            body += f"——{note}"
        if is_conclusion:
            return f"买{body}" if not is_leak else f"{mark}{body}"
        return f"{mark}{body}"
    if market == "spread":
        body = f"{cn(pick)} {line:+.1f} @ {ptxt}，{probtxt}，核价线 {ceil(price):.0f}¢"
        if note:
            body += f"——{note}"
        return f"{mark}买{body}" if is_conclusion else f"{mark}{body}"
    return text

def fmt_spread_attach(key, info):
    _, _, pick, line = key
    price, prob = info["price"], info["prob"]
    return f"{cn(pick)} {line:+.1f} @ {price:.0f}¢ 覆盖 {prob:.0f}%"

def parse_output(out):
    """Return (status, state, items). status: live/waiting/final/none.
    items: list of (key, info) in display order (leaks first, then hits)."""
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return "none", None, []
    first = lines[0]
    m = re.match(r"^(\S+)@(\S+): (\S+) (\d+):(\d+)", first)
    if m:
        desc = m.group(3)
        if desc == "Final":
            return "final", {"desc": desc}, []
        return "waiting", {"desc": desc}, []
    m = re.match(r"^(\S+) (\d+) @ (\S+) (\d+) \| Q(\d+) (\S+) \| WP \S+ [\d.]+% / \S+ [\d.]+% \| proj total (\d+) \| OT ([\d.]+)%", first)
    if not m:
        return "none", None, []
    aab, as_, hab, hs, per, clk, proj, ot = m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5)), m.group(6), m.group(7), m.group(8)
    state = {"aab": aab, "as_": as_, "hab": hab, "hs": hs, "per": per, "clk": clk, "proj": proj, "ot": ot}
    items = []
    seen = set()
    for l in lines[1:]:
        s = l.strip()
        if s.startswith("结论："):
            t = s[len("结论："):]
        elif s.startswith("【倒挂】"):
            t = s[len("【倒挂】"):]
        elif s.startswith("[HIT]"):
            t = s[len("[HIT]"):].strip()
        else:
            continue
        r = parse_item(t)
        if not r:
            continue
        k, info = r
        sig = (k[1], k[2], k[3])
        if sig in seen:
            continue
        seen.add(sig)
        items.append((k, info))
    return "live", state, items

def state_line(state):
    aab, as_, hab, hs, per, clk = state["aab"], state["as_"], state["hab"], state["hs"], state["per"], state["clk"]
    tot = as_ + hs
    proj = state["proj"]
    if per == 2 and clk in ("0.0", "0:00"):
        return f"{cn(aab)} {as_}:{hs} {cn(hab)}，中场，共 {tot} 分，预测全场 {proj} 分"
    if per >= 5:
        q = "加时"
    else:
        q = f"第{Q_CN.get(per, str(per))}节"
    if ":" in clk:
        c = clk
    else:
        c = f"{clk} 秒"
    return f"{cn(aab)} {as_}:{hs} {cn(hab)}，{q}剩 {c}，共 {tot} 分，预测全场 {proj} 分"

def compose(state, newly_main, newly_spread, prefix=""):
    ot = state["ot"]
    parts = [f"{prefix}{state_line(state)}"]
    concl_key, concl_info = newly_main[0]
    parts.append(f"结论：{fmt_main(concl_key, concl_info, True, ot)}")
    rest = []
    for k, info in newly_main[1:]:
        rest.append(fmt_main(k, info, False, ot))
    if rest:
        parts.append("另有 " + "；".join(rest))
    if newly_spread:
        parts.append("附带 " + "；".join(fmt_spread_attach(k, info) for k, info in newly_spread))
    return "\n\n".join(parts)

def clock_secs(clk):
    if ":" in clk:
        try:
            m, s = clk.split(":")
            return float(m) * 60 + float(s)
        except Exception:
            return 9999.0
    try:
        return float(clk)
    except Exception:
        return 9999.0

def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return (set(tuple(x) for x in d.get("qualified", [])),
                bool(d.get("q3_node", False)), bool(d.get("q4_node", False)),
                int(d.get("prev_per", 0)),
                set(tuple(x) for x in d.get("pending", [])),
                dict(d.get("sent_at", {})),
                dict(d.get("risk_alerts", {})))
    except Exception:
        return set(), False, False, 0, set(), {}, {}


def save_state(path, qualified, q3_node=False, q4_node=False, prev_per=0,
               pending=None, sent_at=None, risk_alerts=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"qualified": [list(x) for x in qualified],
                   "q3_node": q3_node, "q4_node": q4_node,
                   "prev_per": prev_per,
                   "pending": [list(x) for x in (pending or set())],
                   "sent_at": sent_at or {},
                   "risk_alerts": risk_alerts or {}}, f)
    os.replace(tmp, path)


def context_suffix(structured, top_key):
    """Context block for a message, without duplicating the state line.

    structured: fast_check structured JSON (or None). top_key: sender
    key (kind, market, pick, line) for the conclusion candidate.
    Returns "" when no structured decision is available.
    """
    if not structured or not _DECISION_OK:
        return ""
    try:
        game = structured.get("game") or {}
        process = structured.get("process")
        cands = structured.get("candidates_all") or []
        match = None
        for c in cands:
            if (c.get("kind"), c.get("market"), c.get("pick"), c.get("line")) == tuple(top_key):
                match = c
                break
        if not match:
            return ""
        block = format_context_block(game, process, {"windows": match.get("market_windows")}, match.get("decision"))
        lines = block.splitlines()
        # Drop the duplicated game line (sender already prints state_line).
        lines = [l for l in lines if not l.startswith("比赛：")]
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def load_structured(path, max_age_sec=120, force=False):
    """Load a fast_check structured file, or None when missing/stale.

    force=True is for test mode (SENDER_STRUCTURED_FILE), where the
    file is intentionally pre-made and age is irrelevant.
    """
    try:
        if not path or not os.path.exists(path):
            return None
        if not force and time.time() - os.path.getmtime(path) > max_age_sec:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def log_msg(logf, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if logf:
        with open(logf, "a") as f:
            f.write(line + "\n")

def tg_send(text, chat_id):
    try:
        r = subprocess.run([sys.executable, TG_PY, "send", chat_id, text],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and '"ok": true' in r.stdout
    except Exception:
        return False

def run_fast_check(date, spec, test_input, structured_path=None, target_usd=None):
    if test_input:
        with open(test_input) as f:
            return f.read()
    env = dict(os.environ)
    env["FC_EARLY_LOG_ONLY"] = "1"
    if structured_path:
        env["FC_STRUCTURED_OUT"] = structured_path
    if target_usd is not None:
        env["FC_TARGET_USD"] = str(target_usd)
    r = subprocess.run([sys.executable, FC_PY, date, spec],
                       capture_output=True, text=True, timeout=40, cwd=FC_DIR, env=env)
    return r.stdout


def _mark_sent(date_us, event_id, items):
    """Mark sent candidates in experience, only after a TG ok.

    items: sender (key, info) list for MAIN candidates only (spreads
    attach-only are not formal pushes). Never called on failure or
    dry-run. Failures here must not break the send loop.
    """
    try:
        sys.path.insert(0, FC_DIR)
        import experience as _exp
        for k, info in items:
            kind, market, pick, line = k
            prob = info.get("prob")
            sent_prob = prob / 100.0 if prob is not None and prob > 1 else prob
            _exp.mark_sent((date_us, str(event_id), market, pick, line, kind),
                           sent_price_c=info.get("price"), sent_prob=sent_prob)
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    ap.add_argument("spec")
    ap.add_argument("--chat", default="")
    ap.add_argument("--interval", type=float, default=45)
    ap.add_argument("--state", required=True)
    ap.add_argument("--log")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-input")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-hours", type=float, default=4.5)
    ap.add_argument("--target-usd", type=float, default=None,
                    help="target USD for cost calculation; unset means cost unknown, never zero")
    a = ap.parse_args()
    prev, q3_node, q4_node, prev_per, pending, sent_at, risk_alerts = load_state(a.state)
    _event_id = a.spec.split(":")[0] if ":" in a.spec else a.spec
    _structured_path = a.state + ".structured.json"
    t0 = time.time()
    while True:
        if time.time() - t0 > a.max_hours * 3600:
            log_msg(a.log, "MAX_HOURS exit")
            return 0
        try:
            out = run_fast_check(a.date, a.spec, a.test_input,
                                 structured_path=_structured_path, target_usd=a.target_usd)
        except Exception as e:
            log_msg(a.log, f"FAST_CHECK ERROR {type(e).__name__}")
            if a.once:
                return 0
            time.sleep(a.interval)
            continue
        status, state, items = parse_output(out)
        # Structured context for this cycle, if fresh (or test-forced).
        _test_struct = os.environ.get("SENDER_STRUCTURED_FILE")
        if a.test_input and _test_struct:
            structured = load_structured(_test_struct, force=True)
        else:
            structured = load_structured(_structured_path)
        if status == "final":
            log_msg(a.log, "STATUS final exit")
            return 0
        if status != "live":
            log_msg(a.log, f"STATUS {status} waiting")
            if a.once:
                return 0
            time.sleep(a.interval)
            continue
        def sig_of(k):
            return (k[1], k[2], k[3])
        def sig_key(k):
            return f"{k[1]}|{k[2]}|{k[3]}"
        cur = set(sig_of(k) for k, _ in items)
        newly = [(k, info) for k, info in items if sig_of(k) not in prev]
        # Spread lines (any kind) are attach-only, never triggers
        # (user 2026-10-25: spread leaks demoted - late leads collapse systematically).
        newly_main = [(k, i) for k, i in newly if k[1] != "spread"]
        newly_spread = [(k, i) for k, i in newly if k[1] == "spread"]
        # Fixed nodes (minimum guarantee: every game gets a Q3-end broadcast
        # and a Q4-6min broadcast, even if no line qualifies -> one-line status).
        per = state["per"]
        clk_s = clock_secs(state["clk"])
        # Early-game gate (user 2026-10-25): Q1/Q2 regular qualifications are
        # logged-only, never sent; leaks stay sendable (fleeting arbitrage).
        # On entering Q3, prev clears so persisting lines send once as new.
        if prev_per <= 2 and per >= 3:
            prev = set()
            newly = [(k, info) for k, info in items if sig_of(k) not in prev]
            newly_main = [(k, i) for k, i in newly if k[1] != "spread"]
            newly_spread = [(k, i) for k, i in newly if k[1] == "spread"]
        prev_per = per
        # Cooldown (user 2026-10-25): a line sent within 15 min never resends,
        # even if it dropped out and returned (kills flicker spam).
        _now = time.time()
        newly = [(k, i) for k, i in newly if _now - sent_at.get(sig_key(k), 0) >= 900]
        newly_main = [(k, i) for k, i in newly if k[1] != "spread"]
        newly_spread = [(k, i) for k, i in newly if k[1] == "spread"]
        node_kind = None
        if not q3_node and per == 4 and clk_s > 0:
            node_kind = "q3"
        elif not q4_node and per == 4 and 0 < clk_s <= 360:
            node_kind = "q4"
        if node_kind:
            label = "【三节结束】" if node_kind == "q3" else "【Q4剩6分钟】"
            main_all = [(k, i) for k, i in items if k[1] != "spread"]
            spread_all = [(k, i) for k, i in items if k[1] == "spread"]
            if main_all:
                node_msg = compose(state, main_all, spread_all, prefix=label)
                _ctx = context_suffix(structured, main_all[0][0])
                if _ctx:
                    node_msg = node_msg + "\n\n" + _ctx
            else:
                node_msg = f"{label}{state_line(state)}，暂无够格线"
                # Node with no qualified line still gets process context
                # when structured data is available (no candidate, no cost).
                if structured and _DECISION_OK:
                    try:
                        _blk = format_context_block(structured.get("game") or {}, structured.get("process"), None, None)
                        _blk_lines = [l for l in _blk.splitlines() if not l.startswith("比赛：")]
                        if _blk_lines:
                            node_msg = node_msg + "\n" + "\n".join(_blk_lines)
                    except Exception:
                        pass
            if a.dry_run:
                log_msg(a.log, f"DRY-RUN node {node_kind} would send:\n" + node_msg)
                if node_kind == "q3":
                    q3_node = True
                    if 0 < clk_s <= 360:
                        q4_node = True
                else:
                    q4_node = True
                prev = cur
                pending = set()
                for k, _ in main_all + spread_all:
                    sent_at[sig_key(k)] = time.time()
                save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
            else:
                ok = tg_send(node_msg, a.chat)
                log_msg(a.log, f"NODE {node_kind} {'ok' if ok else 'FAILED'} main={len(main_all)} spread={len(spread_all)}")
                if ok:
                    log_msg(a.log, "SENT_NODE_MSG:\n" + node_msg)
                    # v1: a node send counts as a send for cooldown and
                    # for experience (main candidates only).
                    _mark_sent(a.date, _event_id, main_all)
                if ok:
                    if node_kind == "q3":
                        q3_node = True
                        if 0 < clk_s <= 360:
                            q4_node = True
                    else:
                        q4_node = True
                    prev = cur
                    pending = set()
                    for k, _ in main_all + spread_all:
                        sent_at[sig_key(k)] = time.time()
                    save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
                # on failure flags stay: next cycle retries
            if a.once:
                return 0
            time.sleep(a.interval)
            continue
        # Confirmation + gate (user 2026-10-25: fewer, more valuable sends).
        # Leaks send immediately; regular hits need 2 consecutive polls;
        # Q1/Q2 regular hits never send (early gate, already live).
        _confirmed = [(k, i) for k, i in items
                      if sig_of(k) in pending and sig_of(k) in cur
                      and k[1] != "spread"]
        _new_leaks = [(k, i) for k, i in newly_main if k[0] in ("leak", "leak_plus")]
        _new_hits = [(k, i) for k, i in newly_main
                     if k[0] not in ("leak", "leak_plus") and sig_of(k) not in pending]
        if per <= 2:
            if not _new_leaks:
                log_msg(a.log, f"GATED early Q{per} newly={len(newly_main)} logged-only")
                prev = cur
                pending = set()
                save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
                if a.once:
                    return 0
                time.sleep(a.interval)
                continue
            _send_main = _new_leaks
            _send_spread = []
        else:
            _send_main = _new_leaks + _confirmed
            _send_spread = newly_spread
        _pending_after = set(sig_of(k) for k, _ in _new_hits) if per >= 3 else set()
        if _send_main:
            msg = compose(state, _send_main, _send_spread)
            _ctx = context_suffix(structured, _send_main[0][0])
            if _ctx:
                msg = msg + "\n\n" + _ctx
            if a.dry_run:
                log_msg(a.log, "DRY-RUN would send:\n" + msg)
                prev = cur
                pending = _pending_after
                for k, _ in _send_main + _send_spread:
                    sent_at[sig_key(k)] = time.time()
                save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
            else:
                ok = tg_send(msg, a.chat)
                log_msg(a.log, f"SEND {'ok' if ok else 'FAILED'} newly={len(_send_main)} spread={len(_send_spread)}")
                if ok:
                    log_msg(a.log, "SENT_MSG:\n" + msg)
                    _mark_sent(a.date, _event_id, _send_main)
                if ok:
                    prev = cur
                    pending = _pending_after
                    for k, _ in _send_main + _send_spread:
                        sent_at[sig_key(k)] = time.time()
                    save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
                # on failure prev/pending/sent_at stay: next cycle retries
        elif _new_hits:
            log_msg(a.log, f"PENDING {len(_new_hits)} awaiting confirmation")
            prev = cur
            pending = set(sig_of(k) for k, _ in _new_hits)
            save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
        else:
            # v1 risk escalation: a previously sent line whose structured
            # decision is now REVIEW/INVALIDATED with NEW reasons is sent
            # even inside the 15-minute cooldown, once per reason set.
            # Candidates never sent before do not get a risk update here;
            # their context is already in the main/node message when sent.
            _risk_sent = False
            if structured and _DECISION_OK:
                for _c in structured.get("candidates_all") or []:
                    _dec = _c.get("decision") or {}
                    _state_r = _dec.get("state")
                    _reasons = _dec.get("reason_codes") or []
                    if _state_r not in ("REVIEW", "INVALIDATED") or not _reasons:
                        continue
                    _ckey = f"{_c['market']}|{_c['pick']}|{_c['line']}"
                    if _ckey not in sent_at:
                        continue
                    _skey = (_dec.get("kind") or _c.get("kind"), _c["market"], _c["pick"], _c["line"])
                    if not should_send_risk(_ckey, _state_r, _reasons, risk_alerts):
                        continue
                    # Priority gate (user 2026-10-26): risk is lowest priority.
                    # Early-game REVIEW is logged only, never sent, and never
                    # marked in risk_alerts, so it can still fire if the
                    # gate later opens (Q4 late / INVALIDATED / prob drop).
                    _game_ctx = structured.get("game") if isinstance(structured, dict) else None
                    if not risk_send_allowed(_state_r, _dec.get("kind") or _c.get("kind"),
                                             _c.get("raw_prob"), _game_ctx):
                        log_msg(a.log, f"RISK held (priority gate) {_ckey} {_state_r} period={per} reasons={','.join(_reasons)}")
                        try:
                            sys.path.insert(0, FC_DIR)
                            import experience as _exp_h
                            _exp_h.log_risk({"date_us": a.date, "event_id": str(_event_id),
                                             "market": _c["market"], "pick": _c["pick"], "line": _c["line"],
                                             "kind": _c.get("kind"), "state": _state_r,
                                             "reason_codes": _reasons, "gate": "held"})
                        except Exception:
                            pass
                        continue
                    _alert_key = f"{_ckey}|{_state_r}|{','.join(sorted(_reasons))}"
                    _state_cn = "需复核" if _state_r == "REVIEW" else "原条件失效"
                    _reason_txt = "；".join(_dec.get("reasons_text") or _reasons)
                    _risk_msg = (f"{state_line(state)}\n\n风险更新：{fmt_main(_skey, {'price': _c.get('raw_price') * 100 if _c.get('raw_price') is not None else None, 'prob': (_c.get('raw_prob') * 100 if _c.get('raw_prob') is not None else None), 'text': _c.get('text', '')}, False, state['ot'])}\n"
                                 f"状态：{_state_cn}｜原因：{_reason_txt}\n说明：先前条件已变化，仅作提醒，不是卖出指令")
                    if a.dry_run:
                        log_msg(a.log, "DRY-RUN risk would send:\n" + _risk_msg)
                        risk_alerts[_alert_key] = time.time()
                        _risk_sent = True
                    else:
                        _ok = tg_send(_risk_msg, a.chat)
                        log_msg(a.log, f"RISK {'ok' if _ok else 'FAILED'} {_ckey} {_state_r}")
                        if _ok:
                            log_msg(a.log, "SENT_RISK_MSG:\n" + _risk_msg)
                            risk_alerts[_alert_key] = time.time()
                            try:
                                sys.path.insert(0, FC_DIR)
                                import experience as _exp
                                _exp.log_risk({"date_us": a.date, "event_id": str(_event_id),
                                               "market": _c["market"], "pick": _c["pick"], "line": _c["line"],
                                               "kind": _c.get("kind"), "state": _state_r,
                                               "reason_codes": _reasons})
                            except Exception:
                                pass
                            _risk_sent = True
                    break
            if not _risk_sent:
                log_msg(a.log, f"STATUS live nothing-new qualified={len(cur)}")
            prev = cur
            pending = set()
            save_state(a.state, prev, q3_node, q4_node, per, pending, sent_at, risk_alerts)
        if a.once:
            return 0
        time.sleep(a.interval)

if __name__ == "__main__":
    sys.exit(main())
