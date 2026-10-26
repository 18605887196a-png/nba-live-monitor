#!/usr/bin/env python3
"""market_context.py - fixed-contract market history, anomaly, cost.

Shared market logic for fast_check.py. Rules (2026-10-25, section 5):

- Identity is condition_id + line + rules, never the line number alone.
  A totals line moving 220.5 -> 225.5 is a different contract, not a
  price move on the old contract.
- Display price may be a mid or last trade. It is never a buy price.
  Buy price for a Yes side is its ask; for a No side it is 1 - bid.
- Market windows are wall-clock (30s / 120s) with the ACTUAL snapshot
  time delta always reported. Insufficient coverage yields unknown,
  never a full-window change.
- A price move with a matching score change is normal repricing.
  A move with no score change, a spread widening, or a last-only move
  (bid/ask unconfirmed) is review/insufficient evidence, never proof
  that win probability fell.
- Cost for a target amount is computed level by level from the book.
  With no complete book, only a reference buy price is reported and
  fill for the target amount is unknown, never promised.
- With no target amount or no channel fee configured, cost is unknown,
  never zero. sender.ceil() (+5c tolerance) is a price tolerance, not
  a profit safety line, and it is never presented as one.
"""
import json
import os
import time
import urllib.request

try:
    from policy_config import (
        MARKET_ANOMALY_MOVE_CENTS_120S,
        MARKET_WINDOWS_SEC,
        SPREAD_WIDEN_CENTS,
    )
except Exception:  # pragma: no cover
    MARKET_ANOMALY_MOVE_CENTS_120S = 10
    MARKET_WINDOWS_SEC = (30, 120)
    SPREAD_WIDEN_CENTS = 10


def parse_gamma_market(m):
    """Extract a fixed-contract identity + quotes from a gamma market.

    Returns a dict with condition_id (or None), market_id, question,
    line, bid, ask, last, token_ids. Gamma field names vary; all known
    spellings are tried and missing identity is reported as None, not
    guessed from the line number.
    """
    if not isinstance(m, dict):
        return None
    cond = m.get("conditionId") or m.get("condition_id") or m.get("conditionID")
    tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except Exception:
            tokens = None
    return {
        "condition_id": cond,
        "market_id": m.get("id"),
        "question": m.get("question"),
        "line": m.get("line"),
        "bid": m.get("bestBid"),
        "ask": m.get("bestAsk"),
        "last": m.get("lastTradePrice"),
        "token_ids": tokens,
    }


def contract_key(identity):
    """Stable key for one contract: condition_id wins, else question+line."""
    if not identity:
        return None
    cond = identity.get("condition_id")
    if cond:
        return f"cond:{cond}"
    q = identity.get("question") or ""
    return f"q:{q}|line:{identity.get('line')}"


def buy_price_for_side(bid, ask, side_yes=True):
    """Buy price for one side: Yes -> ask, No -> 1 - bid."""
    try:
        if side_yes:
            return float(ask) if ask is not None else None
        return 1.0 - float(bid) if bid is not None else None
    except Exception:
        return None


class MarketHistory:
    """Append-only JSONL history per contract key."""

    def __init__(self, directory, key, max_keep=500):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))
        self.path = os.path.join(directory, f"market-{safe}.jsonl")
        self.max_keep = max_keep

    def append(self, snapshot):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            rec = dict(snapshot)
            rec.setdefault("received_epoch", time.time())
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._prune()
        except Exception:
            pass

    def _prune(self):
        try:
            with open(self.path) as f:
                lines = f.readlines()
            if len(lines) > self.max_keep:
                with open(self.path, "w") as f:
                    f.writelines(lines[-self.max_keep:])
        except Exception:
            pass

    def load(self):
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            continue
        except Exception:
            pass
        out.sort(key=lambda r: r.get("received_epoch", 0.0))
        return out


def market_window_features(history, current, score_changed=None):
    """30/120s wall windows for one fixed contract.

    history: snapshots with received_epoch, bid, ask, last, and
    optionally price (the candidate's buy price) and scores.
    current: snapshot with the same fields.
    score_changed: bool, or None to compute per window from
    home_score/away_score in the snapshots (preferred).
    Returns {window_sec: feature} with actual_delta_sec, coverage,
    bid_change_c, ask_change_c, price_change_c, spread_change_c,
    last_only_move flag, and classification:
    normal_repricing / review_price_move_no_score /
    review_spread_widening / insufficient_last_only / unknown.
    When a buy price (price) is present, classification uses its move;
    otherwise it uses the ask move. This matters for No-side
    candidates, whose buy price is 1 - bid, not ask.
    """
    out = {}
    cur_t = float(current.get("received_epoch", time.time()))
    for w in MARKET_WINDOWS_SEC:
        target = cur_t - float(w)
        earlier = [s for s in history if float(s.get("received_epoch", 0.0)) < cur_t]
        if not earlier:
            out[w] = {"status": "unknown", "reason": "no_history"}
            continue
        full = [s for s in earlier if float(s.get("received_epoch", 0.0)) <= target]
        if full:
            base = full[-1]
            coverage = 1.0
        else:
            base = earlier[-1]
            base_t = float(base.get("received_epoch", 0.0))
            coverage = round((cur_t - base_t) / float(w), 3) if w else 0.0
        base_t = float(base.get("received_epoch", 0.0))
        delta = cur_t - base_t
        if score_changed is None:
            try:
                sc = (
                    current.get("home_score") is not None
                    and base.get("home_score") is not None
                    and (
                        float(current["home_score"]) != float(base["home_score"])
                        or float(current["away_score"]) != float(base["away_score"])
                    )
                )
            except Exception:
                sc = False
        else:
            sc = bool(score_changed)
        try:
            bid_ch = (float(current["bid"]) - float(base["bid"])) * 100 if current.get("bid") is not None and base.get("bid") is not None else None
            ask_ch = (float(current["ask"]) - float(base["ask"])) * 100 if current.get("ask") is not None and base.get("ask") is not None else None
            price_ch = (float(current["price"]) - float(base["price"])) * 100 if current.get("price") is not None and base.get("price") is not None else None
        except Exception:
            bid_ch = ask_ch = price_ch = None
        move_ch = price_ch if price_ch is not None else ask_ch
        try:
            spread_now = (float(current["ask"]) - float(current["bid"])) * 100 if current.get("ask") is not None and current.get("bid") is not None else None
            spread_base = (float(base["ask"]) - float(base["bid"])) * 100 if base.get("ask") is not None and base.get("bid") is not None else None
            spread_ch = spread_now - spread_base if spread_now is not None and spread_base is not None else None
        except Exception:
            spread_ch = None
        last_only = False
        try:
            if bid_ch == 0 and ask_ch == 0 and current.get("last") is not None and base.get("last") is not None and float(current["last"]) != float(base["last"]):
                last_only = True
        except Exception:
            pass
        # Classification: only the 120s window with full coverage can
        # trigger a review on size; shorter/partial windows are reported
        # as observed changes, not anomalies, to avoid flicker reviews.
        if last_only:
            cls = "insufficient_last_only"
        elif spread_ch is not None and spread_ch >= SPREAD_WIDEN_CENTS:
            cls = "review_spread_widening"
        elif (
            w == 120
            and coverage >= 1.0
            and move_ch is not None
            and abs(move_ch) >= MARKET_ANOMALY_MOVE_CENTS_120S
            and not sc
        ):
            cls = "review_price_move_no_score"
        elif move_ch is not None and abs(move_ch) >= MARKET_ANOMALY_MOVE_CENTS_120S and sc:
            cls = "normal_repricing"
        else:
            cls = "no_anomaly"
        out[w] = {
            "status": "ok",
            "actual_delta_sec": round(delta, 1),
            "coverage": coverage,
            "bid_change_c": bid_ch,
            "ask_change_c": ask_ch,
            "price_change_c": price_ch,
            "spread_change_c": spread_ch,
            "last_only_move": last_only,
            "classification": cls,
        }
    return out


def fetch_book(token_id, timeout=10):
    """Fetch a CLOB book for one token. Returns (asks, bids) or (None, None).

    asks/bids are lists of (price, size) sorted best-first. Any failure
    yields (None, None) with depth_status unknown; callers must never
    treat that as empty depth.
    """
    if not token_id:
        return None, None
    try:
        req = urllib.request.Request(
            f"https://clob.polymarket.com/book?tokenID={token_id}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        asks = [(float(x["price"]), float(x["size"])) for x in d.get("asks", [])]
        bids = [(float(x["price"]), float(x["size"])) for x in d.get("bids", [])]
        asks.sort(key=lambda x: x[0])
        bids.sort(key=lambda x: -x[0])
        return asks, bids
    except Exception:
        return None, None


def cost_for_target(asks, target_usd, fee_per_share=None):
    """Level-by-level cost for a target USD spend on the ask side.

    asks: [(price, size_shares)] best-first. Target is USD to spend.
    Returns a dict with avg_price, filled_usd, filled_shares,
    fill_complete (bool), cost_per_share_incl_fee (or None when fee
    is unknown), and ev_per_share (or None when fee unknown or when
    no probability is supplied by the caller via ev_prob).
    The caller supplies ev_prob separately; this function never invents
    a probability. When asks is None/empty, fill is unknown, not zero.
    """
    if asks is None or target_usd is None:
        return {
            "status": "unknown",
            "reason": "no_book_or_no_target",
            "avg_price": None,
            "filled_usd": None,
            "filled_shares": None,
            "fill_complete": None,
            "cost_per_share_incl_fee": None,
        }
    try:
        target = float(target_usd)
    except Exception:
        return {"status": "unknown", "reason": "bad_target", "avg_price": None,
                "filled_usd": None, "filled_shares": None, "fill_complete": None,
                "cost_per_share_incl_fee": None}
    remaining = target
    spent = 0.0
    shares = 0.0
    for price, size in asks:
        if remaining <= 0:
            break
        # Cost to take the whole level, in USD.
        level_cost = price * size
        if level_cost <= remaining:
            spent += level_cost
            shares += size
            remaining -= level_cost
        else:
            take = remaining / price if price > 0 else 0.0
            spent += remaining
            shares += take
            remaining = 0.0
    if shares <= 0:
        return {"status": "unknown", "reason": "empty_book", "avg_price": None,
                "filled_usd": 0.0, "filled_shares": 0.0, "fill_complete": False,
                "cost_per_share_incl_fee": None}
    avg = spent / shares
    cost = avg + float(fee_per_share) if fee_per_share is not None else None
    return {
        "status": "ok" if remaining <= 0 else "partial",
        "avg_price": round(avg, 6),
        "filled_usd": round(spent, 2),
        "filled_shares": round(shares, 2),
        "fill_complete": remaining <= 0,
        "cost_per_share_incl_fee": round(cost, 6) if cost is not None else None,
    }
