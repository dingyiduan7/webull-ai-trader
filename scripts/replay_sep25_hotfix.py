"""Read-only replay of the retained September 25 alpha evidence.

This does not contact Webull/OpenAI or mutate either database.  The revised
entry count is a retained-evidence projection: per-call live-window payloads
were not journaled, so it is deliberately not presented as an exact billing
counterfactual.
"""
import json
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal
from math import floor, log
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from autonomous_trader.durable.ledger import LocalDatabase


def stamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def log_bucket(value, scale):
    if value is None or Decimal(str(value)) <= 0:
        return None
    return floor(log(float(value)) * scale)


def retained_material_key(payload):
    dossier = payload.get("opportunity_dossier") or {}
    quotes = dossier.get("quote_sequence") or []
    if not quotes:
        return None
    quote = quotes[-1]
    screen = quote.get("screen_evidence") or {}
    bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
    spread = (ask - bid) / bid * 10000 if bid > 0 else Decimal(0)
    return (
        payload.get("instrument_id"), log_bucket(bid, 200), log_bucket(ask, 200),
        floor(float(spread) / 10), log_bucket(quote.get("volume"), 20),
        log_bucket(quote.get("relative_volume_10d"), 10),
        screen.get("source"), screen.get("window_minutes"),
        floor(float(screen.get("window_change_percent") or 0) / 2),
        floor(float(screen.get("percent_per_minute") or 0)),
    )


def replay(day="2026-09-25"):
    with LocalDatabase("postgres", "mvp_reader", schema="mvp_monitor").connect() as c:
        decisions = c.execute("""SELECT at,payload FROM events WHERE mode='SANDBOX'
            AND at::date=%s AND kind='ALPHA_DECISION' AND payload->>'call_type'='FAST_ENTRY'
            ORDER BY at,id""", (day,)).fetchall()
        plans = c.execute("""SELECT at,payload FROM events WHERE mode='SANDBOX'
            AND at::date=%s AND kind=ANY(ARRAY['ALPHA_PLAN_CREATED','ALPHA_PLAN_MODIFIED'])
            ORDER BY at,id""", (day,)).fetchall()
        requests = c.execute("""SELECT at,payload FROM events WHERE mode='SANDBOX'
            AND at::date=%s AND kind='ALPHA_POSITION_REVIEW_REQUESTED' ORDER BY at,id""", (day,)).fetchall()
        outcomes = c.execute("""SELECT at,payload FROM events WHERE mode='SANDBOX'
            AND at::date=%s AND kind='ALPHA_OUTCOME' ORDER BY at,id""", (day,)).fetchall()
    seen = defaultdict(lambda: deque(maxlen=256)); retained_calls = 0
    actual_by_symbol=defaultdict(int);revised_by_symbol=defaultdict(int)
    latencies = []
    quote_rows = defaultdict(dict)
    for at, payload in decisions:
        key = retained_material_key(payload); symbol = payload.get("symbol");actual_by_symbol[symbol]+=1
        if key is not None and key not in seen[symbol]:
            retained_calls += 1; revised_by_symbol[symbol]+=1;seen[symbol].append(key)
        trace = payload.get("latency_trace") or {}; detected = trace.get("hot_signal_at") or trace.get("hot_promoted_at")
        if detected:
            delta = (at - stamp(detected)).total_seconds()
            if delta >= 0: latencies.append(delta)
        for row in (payload.get("opportunity_dossier") or {}).get("quote_sequence", ()):
            if row.get("at") and row.get("bid"):
                quote_rows[symbol][row["at"]] = Decimal(str(row["bid"]))
    created_plans = {}
    plan_timeline=defaultdict(list)
    for at,payload in plans:
        if payload.get("plan"):
            plan_timeline[payload["plan_id"]].append((at,payload["plan"]))
            created_plans.setdefault(payload["plan_id"],payload["plan"])
    # A later recovery backfill may preserve a legacy reconstruction beside the
    # original completed outcome.  One economic episode is one replay sample:
    # prefer the latest non-legacy record for each durable plan identity.
    outcomes_by_plan = {}
    for at, outcome in outcomes:
        previous = outcomes_by_plan.get(outcome["plan_id"])
        if (previous is None or previous[1].get("exit_reason") == "LEGACY_UNRECORDED"
                or outcome.get("exit_reason") != "LEGACY_UNRECORDED"):
            outcomes_by_plan[outcome["plan_id"]] = (at, outcome)
    unique_outcomes = [row[1] for row in sorted(outcomes_by_plan.values(), key=lambda row: row[0])]
    supported_marks = []
    for outcome in unique_outcomes:
        plan = created_plans.get(outcome["plan_id"])
        if not plan: continue
        review_at = stamp(plan["next_review_at"]); exit_at = stamp(outcome["closed_at"])
        candidates = [(abs((stamp(at)-review_at).total_seconds()), at, bid)
            for at,bid in quote_rows[outcome["symbol"]].items()
            if abs((stamp(at)-review_at).total_seconds()) <= 60]
        if candidates:
            _, at, bid = min(candidates)
            supported_marks.append({"symbol": outcome["symbol"], "actual_exit_at": outcome["closed_at"],
                "original_review_at": plan["next_review_at"], "retained_bid_near_review_at": at,
                "retained_bid": str(bid), "actual_exit_usd": outcome["exit_usd"],
                "note": "mark only; no fill, fee, or slippage counterfactual"})
    replay_requests=[]
    for at,payload in requests:
        prior=[plan for changed,plan in plan_timeline[payload["plan_id"]] if changed<=at]
        active=prior[-1] if prior else None
        scheduled=bool(active and at>=stamp(active["next_review_at"]))
        target=bool(payload.get("target_reassessment"))
        replay_requests.append({"plan_id":payload["plan_id"],"at":at.isoformat(),"scheduled":scheduled,
            "target":target,"material_stream_change":bool(payload.get("material_stream_change"))})
    material_only = sum(row["material_stream_change"] and not row["target"] and not row["scheduled"]
        for row in replay_requests)
    result = {
        "day": day,
        "actual_fast_entry_paid_calls": len(decisions),
        "revised_distinct_states_from_retained_fields": retained_calls,
        "largest_symbol_call_reductions": sorted((
            {"symbol":symbol,"actual":count,"revised_retained_states":revised_by_symbol[symbol],
             "suppressed":count-revised_by_symbol[symbol]} for symbol,count in actual_by_symbol.items()),
            key=lambda row:(-row["suppressed"],row["symbol"]))[:12],
        "entry_replay_limit": "live-window evidence for every paid call was not retained; revised count is not an exact billing forecast",
        "actual_runner_to_decision_latency_seconds": {
            "samples": len(latencies),
            "mean": round(sum(latencies)/len(latencies), 3) if latencies else None,
            "median": round(median(latencies), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "actual_position_review_requests": len(requests),
        "material_wiggle_only_requests_removed_by_revised_policy": material_only,
        "scheduled_or_target_requests_retained":sum(row["scheduled"] or row["target"] for row in replay_requests),
        "completed_outcomes": len(unique_outcomes),
        "actual_net_after_all_attributed_trade_costs_usd": str(sum(
            Decimal(str(row["realized_net_after_all_known_costs_usd"])) for row in unique_outcomes)),
        "supported_post_exit_marks": supported_marks,
        "net_counterfactual": "not computed: retained marks do not prove executable fills, fees, or slippage",
    }
    return result


if __name__ == "__main__":
    print(json.dumps(replay(sys.argv[1] if len(sys.argv) > 1 else "2026-09-25"), indent=2))
