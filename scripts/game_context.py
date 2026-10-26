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
    from policy_config import GAME_WINDOWS_MIN
except Exception:  # pragma: no cover - direct script execution fallback
    GAME_WINDOWS_MIN = (2, 5)


def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def parse_clock_seconds(clock):
    """ESPN displayClock -> seconds remaining in the current period.

    Accepts "12:00", "5:22", "43.5" (bare seconds under a minute) and
    "0.0". Unparseable input returns 0.0, never a fabricated value.
    """
    if clock is None:
        return 0.0
    s = str(clock).strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return float(m) * 60.0 + float(sec)
        return float(s)
    except Exception:
        return 0.0


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
    clk_min = parse_clock_seconds(clock) / 60.0
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
        out.sort(key=lambda r: r.get("elapsed_min", 0.0))
        return out


def window_features(history, current):
    """2/5-minute game-time windows from snapshot history.

    history: list of snapshots with elapsed_min, lead, tot, scores.
    current: dict with elapsed_min, lead, tot, home_score, away_score.
    Returns {window_min: feature} where feature is either a dict with
    actual_start_elapsed, actual_delta_min, coverage, lead_change,
    home_delta, away_delta, tot_delta, scoring_pace_per_min, or
    {"status": "unknown", "reason": "no_history"} when no earlier
    snapshot exists. A snapshot after the window start gives partial
    coverage (< 1.0) and its actual delta is reported, never assumed
    to equal the window length. Wall-clock gaps (pauses) do not enter
    this calculation at all: only game elapsed matters.
    """
    out = {}
    cur_el = float(current.get("elapsed_min", 0.0))
    for w in GAME_WINDOWS_MIN:
        target = cur_el - float(w)
        earlier = [s for s in history if float(s.get("elapsed_min", 0.0)) < cur_el]
        if not earlier:
            out[w] = {"status": "unknown", "reason": "no_history"}
            continue
        # Latest snapshot at or before the window start = full coverage.
        # Otherwise the latest snapshot before now = partial coverage.
        full = [s for s in earlier if float(s.get("elapsed_min", 0.0)) <= target]
        if full:
            base = full[-1]
            coverage = 1.0
        else:
            base = earlier[-1]
            base_el = float(base.get("elapsed_min", 0.0))
            coverage = round((cur_el - base_el) / float(w), 3) if w else 0.0
        base_el = float(base.get("elapsed_min", 0.0))
        delta = cur_el - base_el
        if delta <= 0:
            out[w] = {"status": "unknown", "reason": "no_elapsed_delta"}
            continue
        lead_change = float(current.get("lead", 0)) - float(base.get("lead", 0))
        home_delta = float(current.get("home_score", 0)) - float(base.get("home_score", 0))
        away_delta = float(current.get("away_score", 0)) - float(base.get("away_score", 0))
        tot_delta = float(current.get("tot", 0)) - float(base.get("tot", 0))
        out[w] = {
            "status": "ok",
            "actual_start_elapsed": base_el,
            "actual_delta_min": round(delta, 3),
            "coverage": coverage,
            "lead_change": lead_change,
            "home_delta": home_delta,
            "away_delta": away_delta,
            "tot_delta": tot_delta,
            # Scoring pace only: points per game minute, not possessions.
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
    if w5.get("status") == "ok":
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
