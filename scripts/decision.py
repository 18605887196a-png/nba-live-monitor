#!/usr/bin/env python3
"""decision.py - merge a candidate into one explainable state.

Four states only, per candidate version (2026-10-25, section 6):
  WATCH       base candidate holds, no pausing conflict found.
              Price value is listed separately; WATCH is NOT "buy".
  REVIEW      core info missing, or an unexplained change exists:
              stale snapshot, market anomaly, process risk against
              the candidate, or cost unverifiable for a configured
              target. REVIEW does not flip the direction and does not
              change the probability.
  INVALIDATED a prior candidate no longer meets its explicit condition:
              identity mismatch, base condition false in structured
              data, market closed, or price missing. INVALIDATED does
              not mean the bet must lose, and it never triggers an
              automatic sell.
  CLOSED      game over or that market stopped trading.

Rules:
- Raw probability is passed through unchanged, at full precision.
  calibrated_probability is always None in v1 (uncalibrated model).
- No manual +/- points, no whale score, no site score, no composite
  "stability score" is ever added here.
- Reasons are capped at 2 in the message; the full list lives in the
  structured output. Missing items are explicit, never filled with 0.
- A state change needs a new valid snapshot and forms a new version.
  Version here is a stable hash of (candidate key + state + reasons),
  so "same reason, same version" can be deduplicated by the sender.
- In bypass stage (v1), this state does NOT gate sender pushes:
  sender still uses its existing newly/pending/cooldown/node rules.
  The only sender behaviour this state changes is the risk-update
  path: a REVIEW/INVALIDATED transition with new reasons is sent
  even inside the 15-minute cooldown, once per reason set.
"""
import hashlib
import json

try:
    from policy_config import (
        LEAD_NARROW_POINTS_5M,
        POLICY_VERSION,
        MODEL_VERSION,
        QUOTE_MAX_RECEIVED_AGE_SEC,
    )
except Exception:  # pragma: no cover
    LEAD_NARROW_POINTS_5M = 10
    POLICY_VERSION = "v1-bypass-20261025"
    MODEL_VERSION = "fast_check-20261025-wp-pace"
    QUOTE_MAX_RECEIVED_AGE_SEC = 120

REASON_TEXT = {
    "identity_mismatch": "合约身份不匹配（换线/换合约，不能按原合约算涨跌）",
    "base_condition_false": "基础条件在结构化数据里已不成立",
    "market_closed": "该市场已停止交易",
    "price_missing": "当前买价缺失，无法核价",
    "quote_stale": "快照接收时间过久，可能已过期",
    "quote_age_unknown": "行情来源时间未知，只能按接收时间判断",
    "market_move_no_score": "盘口在比分未变时明显移动，需复核",
    "spread_widening": "买卖价差明显扩大，流动性异常",
    "last_only_move": "只有旧成交价跳动，当前买卖报价未确认",
    "process_lead_narrowing": "近5分钟领先优势明显缩小，与候选方向相反",
    "process_blowout_tail": "大比分领先进入收尾，胜负与让分须分开看",
    "process_close_late": "比分接近进入末节，可能进入犯规阶段",
    "cost_unverifiable_target": "已设目标金额但订单簿不足，无法确认全额成本",
    "cost_unknown": "当前成本未核实（无目标金额或无渠道费用）",
    "history_unknown": "比赛历史不足，过程窗口未知",
}


def _version_for(key, state, reasons):
    payload = json.dumps([key, state, sorted(reasons)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def decide_candidate(candidate, game, process, market, cost, game_closed=False, market_closed=False):
    """Decide one candidate. candidate: dict with market/pick/line/kind/
    raw_prob/raw_price/ask/bid/identity. game/process/market/cost may be
    None/empty, in which case missing items are reported, not assumed.
    Returns a structured decision dict.
    """
    key = [
        candidate.get("market"),
        candidate.get("pick"),
        candidate.get("line"),
        candidate.get("kind"),
    ]
    reasons = []
    missing = []
    raw_prob = candidate.get("raw_prob")
    raw_price = candidate.get("raw_price")

    # CLOSED first.
    if game_closed or market_closed:
        state = "CLOSED"
        reasons = ["market_closed" if market_closed else "base_condition_false"]
        return _build(key, candidate, state, reasons, missing, cost)

    # INVALIDATED: explicit conditions only.
    if candidate.get("identity_mismatch"):
        reasons.append("identity_mismatch")
    if candidate.get("base_condition_false"):
        reasons.append("base_condition_false")
    if raw_price is None:
        reasons.append("price_missing")
        missing.append("buy_price")
    if reasons:
        return _build(key, candidate, "INVALIDATED", reasons, missing, cost)

    # REVIEW: missing/stale, market anomaly, process risk, cost.
    if process is None:
        missing.append("game_history")
        reasons.append("history_unknown")
    if market is None:
        missing.append("market_history")
    # Quote age: only received-at age is knowable.
    recv_age = None
    if market and isinstance(market, dict):
        recv_age = market.get("received_age_sec")
    if recv_age is not None and recv_age > QUOTE_MAX_RECEIVED_AGE_SEC:
        reasons.append("quote_stale")
    # Market windows: only the 120s full-coverage anomaly counts.
    if market and isinstance(market, dict):
        _mw = market.get("windows") or {}
        w120 = _mw.get(120, _mw.get("120", {}))
        cls = w120.get("classification") if isinstance(w120, dict) else None
        if cls == "review_price_move_no_score":
            reasons.append("market_move_no_score")
        elif cls == "review_spread_widening":
            reasons.append("spread_widening")
        elif cls == "insufficient_last_only":
            reasons.append("last_only_move")
    # Process risk, direction-aware where possible.
    tags = []
    if process and isinstance(process, dict):
        tags = process.get("risk_tags") or []
    if "lead_narrowing_large_5m" in tags:
        # Only a review when it runs against the candidate's direction.
        # ML candidate direction is the picked team; totals have no
        # lead direction, so a narrowing lead alone is not a totals
        # review reason in v1 (it is still shown as process fact).
        if candidate.get("market") == "ml":
            reasons.append("process_lead_narrowing")
    if "blowout_tail" in tags and candidate.get("market") in ("ml", "spread"):
        # Shown as a caution, not a hard review, unless the candidate
        # is a spread: late blowout margin is the least stable.
        if candidate.get("market") == "spread":
            reasons.append("process_blowout_tail")
    if "close_late_may_enter_foul_phase" in tags and candidate.get("market") == "total":
        reasons.append("process_close_late")
    # Cost: configured target + incomplete book -> review. No target
    # configured -> cost is reported as unknown, but does NOT force
    # every candidate into REVIEW (otherwise WATCH could never occur).
    if cost and isinstance(cost, dict):
        if cost.get("status") == "partial":
            reasons.append("cost_unverifiable_target")
        elif cost.get("status") == "unknown" and cost.get("reason") == "no_book_or_no_target":
            missing.append("cost")
    state = "REVIEW" if reasons else "WATCH"
    return _build(key, candidate, state, reasons, missing, cost)


def _build(key, candidate, state, reasons, missing, cost):
    reasons = list(dict.fromkeys(reasons))
    version = _version_for(key, state, reasons)
    ev = None
    if cost and cost.get("cost_per_share_incl_fee") is not None and candidate.get("raw_prob") is not None:
        ev = round(float(candidate["raw_prob"]) - float(cost["cost_per_share_incl_fee"]), 6)
    return {
        "signal_id": f"{candidate.get('event_id') or ''}:{candidate.get('market')}:{candidate.get('pick')}:{candidate.get('line')}",
        "version": version,
        "policy_version": POLICY_VERSION,
        "model_version": MODEL_VERSION,
        "market": candidate.get("market"),
        "pick": candidate.get("pick"),
        "line": candidate.get("line"),
        "kind": candidate.get("kind"),
        "raw_probability": candidate.get("raw_prob"),
        "calibrated_probability": None,
        "raw_price": candidate.get("raw_price"),
        "cost": cost,
        "ev_per_share_raw_model": ev,
        "state": state,
        "reason_codes": reasons,
        "reasons_text": [REASON_TEXT.get(r, r) for r in reasons[:2]],
        "missing": missing,
    }


def risk_send_allowed(state, kind, raw_prob, game):
    """Priority gate for risk updates (user 2026-10-26).

    Returns True only when the risk is worth sending, not just logging:
    INVALIDATED always; REVIEW only in Q4/OT late (remaining <= 5 min)
    or when raw_prob fell below its push threshold (hit 0.95, leak 0.90).
    Early-game REVIEW (e.g. Q3, price repricing with prob/cutoff intact)
    returns False: the caller logs it only and does not mark it sent.
    game may be the structured game dict (period/remaining_min) or None;
    with no game info, only INVALIDATED passes.
    """
    if state == "INVALIDATED":
        return True
    if state != "REVIEW":
        return False
    if raw_prob is not None:
        thresh = 0.90 if kind in ("leak", "leak_plus") else 0.95
        if raw_prob < thresh:
            return True
    if isinstance(game, dict):
        try:
            per = int(game.get("period") or 0)
            rem = float(game.get("remaining_min")) if game.get("remaining_min") is not None else None
        except Exception:
            per, rem = 0, None
        if per >= 4 and rem is not None and rem <= 5.0:
            return True
    return False


def should_send_risk(candidate_key, state, reasons, risk_alerts):
    """Risk-update dedup: same candidate + state + reason set, once.

    risk_alerts: dict from sender state, keyed by
    "market|pick|line|state|reason1,reason2". Returns True only for a
    new reason set. This is what lets a risk update bypass the ordinary
    15-minute cooldown without spamming the same reason.
    """
    if state not in ("REVIEW", "INVALIDATED"):
        return False
    alert_key = f"{candidate_key}|{state}|{','.join(sorted(reasons))}"
    return alert_key not in (risk_alerts or {})


def format_context_block(game, process, market_for_top, decision):
    """One appended context block for a message, per section 6 template.

    All numbers are placeholders-free: unknown fields are written as
    未知, never 0. The block is informational; it contains no "buy"
    conclusion and, for INVALIDATED, no sell instruction.
    """
    lines = []
    if game:
        lines.append(
            f"比赛：{game.get('away_abbr')} {game.get('away_score')} @ {game.get('home_abbr')} {game.get('home_score')}｜"
            f"第{game.get('period')}节剩 {game.get('clock')}｜共 {game.get('tot')} 分"
        )
    if process and isinstance(process, dict):
        _pw = process.get("windows", {}) if isinstance(process.get("windows"), dict) else {}
        w5 = _pw.get(5, _pw.get("5", {}))
        if w5.get("status") == "ok":
            lines.append(
                f"过程：近5分钟领先变化 {w5.get('lead_change'):+.0f} 分，双方共得 {w5.get('tot_delta'):+.0f} 分，"
                f"得分速度 {w5.get('scoring_pace_per_min')} 分/分钟（实际窗口 {w5.get('actual_delta_min')} 分钟，覆盖度 {w5.get('coverage')}）"
            )
        else:
            lines.append("过程：未知（历史不足，刚启动或无上一快照）")
        tags = process.get("risk_tags") or []
        if tags:
            lines.append(f"过程标签：{','.join(tags)}")
    else:
        lines.append("过程：未知")
    if market_for_top and isinstance(market_for_top, dict):
        _mw2 = market_for_top.get("windows") or {}
        w120 = _mw2.get(120, _mw2.get("120", {}))
        if isinstance(w120, dict) and w120.get("status") == "ok":
            lines.append(
                f"盘口：同一合约买价变化 {w120.get('ask_change_c')}¢（实际间隔 {w120.get('actual_delta_sec')} 秒，分类：{w120.get('classification')}）"
            )
        else:
            lines.append("盘口：未知（行情历史不足）")
    else:
        lines.append("盘口：未知")
    if decision:
        cost = decision.get("cost") or {}
        if cost.get("status") == "ok" or cost.get("status") == "partial":
            lines.append(
                f"当前成本：目标金额下每份 {cost.get('avg_price')}（可成交 {cost.get('filled_usd')} 美元，"
                f"{'全额' if cost.get('fill_complete') else '未全额'}），费用后每份 {cost.get('cost_per_share_incl_fee')}"
            )
        else:
            lines.append("当前成本：未核实（无目标金额、订单簿或渠道费用，核价线只是价格容忍，不是盈利安全线）")
        state_cn = {"WATCH": "观察", "REVIEW": "需复核", "INVALIDATED": "原条件失效", "CLOSED": "已结束"}.get(decision.get("state"), decision.get("state"))
        reason_txt = "；".join(decision.get("reasons_text") or []) or "无冲突"
        lines.append(f"状态：{state_cn}｜原因：{reason_txt}")
        if decision.get("state") == "INVALIDATED":
            lines.append("说明：原条件失效不等于必输，不触发自动卖出，仅作提醒")
    return "\n".join(lines)
