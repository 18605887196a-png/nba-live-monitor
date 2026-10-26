#!/usr/bin/env python3
"""game_context.py - game clock, history windows, and process risk tags.

Shared by fast_check.py and monitor.py so there is exactly one clock
parser and one win-probability formula in the codebase.

Rules (2026-10-25 architecture, section 4):
- Game time, not wall time, drives windows. A 5-minute wall pause is
  not 5 minutes of game time.
- Overtime is counted separately: regulation elapsed is 48 minutes,
  each OT period adds 5 minutes. Never compute OT elapsed as
  ``48 - OT remaining``.
- With no history, a window is ``unknown``, never zero.
- Without play-by-play data, a fast scoring stretch is called
  "scoring pace", never true pace (possessions).
- Risk tags are facts and review reasons only. They never add or
  subtract probability points from the raw model.
"""
import json
import math
import os
import time

try:
    from policy_config import GAME_WINDOWS_MIN, WINDOW_MAX_GAP_MIN
except Exception:  # pragma: no cover - direct script execution fallback
    GAME_WINDOWS_MIN = (2, 5)
    WINDOW_MAX_GAP_MIN = 3.0


def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def parse_clock_seconds(clock):
    """ESPN displayClock -> seconds remaining in the current period.

    Accepts "12:00", "5:22", "43.5" (bare seconds under a minute) and
    "0.0". Unparseable input returns None, never 0.0: 0.0 means period
    end, and returning it for a bad clock would fabricate a final-like
    probability. Callers must treat None as a data error.
    """
    if clock is None:
        return None
    s = str(clock).strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return float(m) * 60.0 + float(sec)
        return float(s)
    except Exception:
        return None


def game_time(period, clock):
    """Return (elapsed_min, remaining_min, is_ot) for a period/clock.

    Regulation: period 1..4, each 12 minutes.
      elapsed   = (period-1)*12 + (12 - clock_min)
      remaining = (4-period)*12 + clock_min
    Overtime: period 5, 6, ... each 5 minutes.
      elapsed   = 48 + (period-5)*5 + (5 - clock_min)
      remaining = clock_min
    Period 0 / None (pre-game) returns (0.0, 48.0, False) and callers
    must still gate on live status; this function alone never makes a
    pre-game state look live.
    """
    if period is None:
        return 0.0, 48.0, False
    try:
        per = int(period)
    except Exception:
        return 0.0, 48.0, False
    _clk = parse_clock_seconds(clock)
    if _clk is None:
        return None, None, False
    clk_min = _clk / 60.0
    if per >= 5:
        elapsed = 48.0 + (per - 5) * 5.0 + (5.0 - clk_min)
        return elapsed, clk_min, True
    if per >= 1:
        elapsed = (per - 1) * 12.0 + (12.0 - clk_min)
        remaining = (4 - per) * 12.0 + clk_min
        return elapsed, remaining, False
    return 0.0, 48.0, False


def wp_home(lead_home, home_margin, remaining_min):
    """Win probability for the home team, the one shared model.

    WP = Phi((lead + M * t/48) / (13 * sqrt(t/48))), t = remaining.
    Returns None when t < 0.5 (model undefined at the buzzer), matching
    the pre-existing fast_check/monitor behaviour.
    """
    t = float(remaining_min)
    if t < 0.5:
        return None
    sd = 13.0 * math.sqrt(t / 48.0)
    return Phi((lead_home + home_margin * t / 48.0) / sd)


def ot_probability(lead_home, home_margin, remaining_min):
    """P(final margin lands on 0) under the same normal model."""
    t = float(remaining_min)
    if t < 0.5:
        return 0.0
    sd = 13.0 * math.sqrt(t / 48.0)
    mean = lead_home + home_margin * t / 48.0
    return Phi((0.5 - mean) / sd) - Phi((-0.5 - mean) / sd)


class SnapshotHistory:
    """Append-only JSONL history per event, shared by both entries.

    Each snapshot carries received_at (wall, CST string + epoch) and
    source_at=None, because ESPN gives no source timestamp. Callers
    must never present received_at age as source age.
    """

    def __init__(self, directory, event_id, max_keep=500):
        self.directory = directory
        self.event_id = str(event_id)
        self.path = os.path.join(directory, f"game-{self.event_id}.jsonl")
        self.max_keep = max_keep

    def append(self, snapshot):
        try:
            os.makedirs(self.directory, exist_ok=True)
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
        # Received order, NOT elapsed order: sorting by elapsed would hide
        # clock regressions and score corrections. Callers that need a
        # monotonic trend must detect anomalies explicitly (see
        # window_features), not sort them away.
        out.sort(key=lambda r: r.get("received_epoch", 0.0))
        return out


def window_features(history, current):
    """2/5-minute game-time windows from snapshot history.

    history: snapshots in RECEIVED order with elapsed_min, lead, tot,
    scores. current: dict with elapsed_min, lead, tot, home_score,
    away_score.
    Returns {window_min: feature} where feature is either a dict with
    actual_start_elapsed, actual_delta_min, coverage, coverage_status
    ("full"/"partial"), lead_change, home_delta, away_delta,
    tot_delta, scoring_pace_per_min, or {"status": "unknown",
    "reason": ...} when no earlier snapshot exists, when the window
    spans a detected history anomaly (clock regression or score
    correction), or when there is no elapsed delta. A partial window
    reports its actual observed change and coverage; it never claims
    to be a full 5-minute window, and risk_tags only fires its
    5-minute thresholds on full windows. Wall-clock gaps (pauses) do
    not enter this calculation at all: only game elapsed matters.
    """
    out = {}
    cur_el = float(current.get("elapsed_min", 0.0))
    # Anomaly points (in received order): elapsed going backwards, or
    # a score/total going down while elapsed goes up (a correction).
    # A window that spans such a point is unknown, not a trend.
    anomalies = []
    _max_el = None
    _prev = None
    for s in history or []:
        try:
            el = float(s.get("elapsed_min", 0.0))
        except Exception:
            continue
        if _max_el is not None and el < _max_el - 0.01:
            anomalies.append(el)
        if _prev is not None:
            try:
                if el > float(_prev.get("elapsed_min", 0.0)) and (
                    float(s.get("home_score", 0)) < float(_prev.get("home_score", 0))
                    or float(s.get("away_score", 0)) < float(_prev.get("away_score", 0))
                    or float(s.get("tot", 0)) < float(_prev.get("tot", 0))
                ):
                    anomalies.append(el)
            except Exception:
                pass
        if _max_el is None or el > _max_el:
            _max_el = el
        _prev = s
    for w in GAME_WINDOWS_MIN:
        target = cur_el - float(w)
        earlier = [s for s in history if float(s.get("elapsed_min", 0.0)) < cur_el]
        if not earlier:
            out[w] = {"status": "unknown", "reason": "no_history", "coverage_status": "unknown"}
            continue
        full = [s for s in earlier if float(s.get("elapsed_min", 0.0)) <= target]
        if full:
            # Base is the latest GAME-time snapshot at/before target,
            # not the last received one: a regressed snapshot that
            # arrives late must not become the window base.
            base = max(full, key=lambda s: float(s.get("elapsed_min", 0.0)))
            coverage = 1.0
        else:
            base = max(earlier, key=lambda s: float(s.get("elapsed_min", 0.0)))
            base_el = float(base.get("elapsed_min", 0.0))
            coverage = round((cur_el - base_el) / float(w), 3) if w else 0.0
        base_el = float(base.get("elapsed_min", 0.0))
        delta = cur_el - base_el
        if delta <= 0:
            out[w] = {"status": "unknown", "reason": "no_elapsed_delta", "coverage_status": "unknown"}
            continue
        if any(base_el < a <= cur_el for a in anomalies):
            out[w] = {"status": "unknown", "reason": "history_anomaly",
                      "coverage_status": "unknown",
                      "actual_start_elapsed": base_el,
                      "actual_delta_min": round(delta, 3)}
            continue
        lead_change = float(current.get("lead", 0)) - float(base.get("lead", 0))
        home_delta = float(current.get("home_score", 0)) - float(base.get("home_score", 0))
        away_delta = float(current.get("away_score", 0)) - float(base.get("away_score", 0))
        tot_delta = float(current.get("tot", 0)) - float(base.get("tot", 0))
        # Gap check (2026-10-26): a window is only "full" if it is
        # actually sampled through. Base deviation from the window
        # start, and every adjacent game-time gap inside the window
        # (base -> ... -> current), must be within tolerance. A
        # 10-minute span with only endpoints is an observed 10-minute
        # change, never a 5-minute change; no interpolation is done.
        # Pauses cannot trip this: they do not advance game elapsed,
        # so they create no game-time gap.
        _span_els = sorted([float(s.get("elapsed_min", 0.0)) for s in earlier
                            if base_el <= float(s.get("elapsed_min", 0.0)) < cur_el] + [cur_el])
        _gaps = [b - a for a, b in zip(_span_els, _span_els[1:])] if len(_span_els) > 1 else [delta]
        _max_gap = max(_gaps) if _gaps else delta
        _base_dev = (target - base_el) if base_el < target else 0.0
        _insufficient = (_max_gap > WINDOW_MAX_GAP_MIN or _base_dev > WINDOW_MAX_GAP_MIN)
        if coverage >= 1.0 and _insufficient:
            _cov_status = "insufficient_coverage"
        elif coverage >= 1.0:
            _cov_status = "full"
        else:
            _cov_status = "insufficient_coverage" if _insufficient else "partial"
        out[w] = {
            "status": "ok",
            "coverage_status": _cov_status,
            "history_gap": bool(_insufficient),
            "max_gap_min": round(_max_gap, 3),
            "base_deviation_min": round(_base_dev, 3),
            "actual_start_elapsed": base_el,
            "actual_delta_min": round(delta, 3),
            "coverage": coverage,
            "lead_change": lead_change,
            "home_delta": home_delta,
            "away_delta": away_delta,
            "tot_delta": tot_delta,
            "scoring_pace_per_min": round(tot_delta / delta, 3),
        }
    return out


def risk_tags(current, windows, candidate=None):
    """Factual process tags for the current snapshot.

    candidate (optional): {"market": "ml"|"total"|"spread", "pick": ...}
    for direction-aware tags. Tags never change probabilities; they are
    consumed by decision.py as review reasons. No foul/rotation event
    source exists in v1, so those tags are never fabricated: they are
    simply absent, and decision.py reports them as unknown when asked.
    """
    tags = []
    per = current.get("period")
    rem = float(current.get("remaining_min", 0.0))
    lead = float(current.get("lead", 0))
    w5 = windows.get(5, {}) if isinstance(windows, dict) else {}
    # Full-window rule only: a partial or insufficient-coverage
    # window may be displayed as an observed change, but it must not
    # trigger a "5-minute" threshold.
    if w5.get("status") == "ok" and w5.get("coverage_status") == "full":
        lc = float(w5.get("lead_change", 0.0))
        # Lead moved toward zero by a notable amount in 5 game minutes.
        if abs(lead) < abs(lead - lc) and abs(lc) >= 5:
            tags.append("lead_narrowing_5m")
        if abs(lc) >= 10 and abs(lead) < abs(lead - lc):
            tags.append("lead_narrowing_large_5m")
        if lc != 0 and abs(lead) > abs(lead - lc):
            tags.append("lead_widening_5m")
    if per is not None and int(per) >= 4 and abs(lead) >= 20:
        tags.append("blowout_tail")
    if per is not None and int(per) >= 4 and abs(lead) <= 5 and 0 < rem <= 3.0:
        # Close late game: may enter a foul phase. Never stated as
        # "intentional fouling has started" without an event source.
        tags.append("close_late_may_enter_foul_phase")
    if current.get("is_ot"):
        tags.append("overtime")
    return tags
