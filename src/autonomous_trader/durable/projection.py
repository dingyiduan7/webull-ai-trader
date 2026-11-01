"""Version-1 deterministic journal reducer and approved risk arithmetic.

No database, wall clock, random generator, adapter, or external side effect.
Money is serialized as exact decimal strings; FIFO lots exclude fees, which
are recognized once as incurred expense on the execution's session date.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, localcontext, Inexact
from hashlib import sha256
import json

from ..contracts import Instrument, OrderIntent, payload_hash, utc
from ..numeric import aligned
from ..reference import NEW_YORK, Session
from ..risk import POLICY
from ..economic_policy import SandboxEconomics, PolicyProposal, effective_policy, policy_revision, account_fingerprint, daily_downside_budget

D = Decimal
Z = D("0")
MAX_ENTRY_SUBMISSIONS_PER_SESSION = 2
MAX_ENTRY_SUBMISSIONS_PER_EXCHANGE_WEEK = 10
MAX_REAL_ROUND_TRIPS_PER_EXCHANGE_WEEK = 10


class Rejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def stamp(value):
    return utc(value).isoformat()


def key(event, suffix):
    return digest([event["event_id"], suffix])


def incident(s, event, reason, pending=False):
    s["incidents"][event["event_id"]] = {"reason": reason, "pending_correction": pending}
    s["reconciliation"]["ready"] = False
    s["latches"]["incident"] = True


def current_session(s):
    return Session.model_validate(s["session"])


def apply_mark(s, c, at):
    require(c["instrument_id"] in s["instruments"], "UNKNOWN_INSTRUMENT")
    require(utc(c["observed_at"]) <= at, "FUTURE_MARK")
    if c.get('received_at') is not None or c.get('valid_until') is not None:
        require(s.get('execution_scope')=='REAL' and c.get('received_at') is not None
            and c.get('valid_until') is not None, 'REAL_MARK_VALIDITY_REQUIRED')
        require(current_session(s).opens_at<=utc(c['observed_at'])<=at
            and utc(c['observed_at'])<=utc(c['received_at'])<=at<utc(c['valid_until'])
            and utc(c['valid_until'])<=min(current_session(s).closes_at,utc(c['observed_at'])+timedelta(seconds=10)), 'REAL_MARK_VALIDITY_INVALID')
    old = s["marks"].get(c["instrument_id"])
    require(old is None or utc(c["observed_at"]) >= utc(old["observed_at"]), "REGRESSING_MARK")
    s["marks"][c["instrument_id"]] = c


def exit_horizon(s):
    """Latest permitted exit deadline: the current liquidation time, or (when the charter-bound
    REAL_CONFIGURATION authorized carry_sessions) 16:00 New York on that many settlement days later."""
    session = current_session(s)
    sessions = s["config"].get("carry_sessions") or 0
    later = [d for d in s["config"]["settlement_days"] if d > session.label.isoformat()]
    if not sessions or not later:
        return session.liquidation_at
    from datetime import time as _time
    day = datetime.fromisoformat(later[min(sessions, len(later)) - 1]).date()
    return max(session.liquidation_at, datetime.combine(day, _time(16), NEW_YORK))


def entry_submission_counts(s,at):
    """Derive durable entry authority from consumed one-use permits.

    Permit consumption precedes every broker write and survives terminal
    rejection, uncertainty, partial fill, and process restart.  Derivation
    avoids a second mutable counter that could drift from journal authority.
    """
    label=current_session(s).label;week_start=label-timedelta(days=label.weekday())
    session_count=week_count=0;seen=set()
    for permit in s['permits'].values():
        if not permit['consumed']:continue
        intent_id=permit.get('intent_id');order=s['orders'].get(intent_id)
        require(order is not None,'ENTRY_SUBMISSION_COUNT_UNAVAILABLE')
        if order['intent']['side']!='BUY':continue
        require(intent_id not in seen,'ENTRY_SUBMISSION_COUNT_UNAVAILABLE');seen.add(intent_id)
        consumed_at=permit.get('consumed_at')
        require(consumed_at is not None,'ENTRY_SUBMISSION_COUNT_UNAVAILABLE')
        # The bound entry deadline is part of immutable intent authority and is
        # always inside its regular exchange session. Database receipt time is
        # retained as submission evidence but must not redefine that session.
        try:submitted=utc(order['intent']['deadline']).astimezone(NEW_YORK).date()
        except (KeyError,TypeError,ValueError):raise Rejected('ENTRY_SUBMISSION_COUNT_UNAVAILABLE') from None
        if submitted==label:session_count+=1
        if week_start<=submitted<week_start+timedelta(days=7):week_count+=1
    return session_count,week_count


def active_episode_ids(s, instrument_id):
    """Episodes that still carry position, reservation, or order authority."""
    result={p["episode_id"] for p in s["positions"].values() if p["instrument_id"]==instrument_id}
    result.update(r["episode_id"] for r in s["reservations"].values() if r["instrument_id"]==instrument_id)
    result.update(o["intent"]["episode_id"] for o in s["orders"].values()
        if not o["final"] and o["intent"]["instrument_id"]==instrument_id)
    return result


def round_trip_capacity(s,at):
    """Reserve one entry-to-flat position round trip, including unknown orders.

    Filled entries retain their slot after exit; final proven zero-fill entries
    release it. Exits never consume a slot. No mutable counter or daily ceiling.
    """
    label=current_session(s).label;start=label-timedelta(days=label.weekday())
    groups={}
    for order_id,order in s['orders'].items():
        if order['intent']['side']!='BUY':continue
        day=utc(order.get('round_trip_entry_at',order['intent']['deadline'])).astimezone(NEW_YORK).date()
        if not start<=day<start+timedelta(days=7):continue
        group=groups.setdefault(order.get('round_trip_id',order_id),{'used':False,'reserved':False})
        filled=order.get('round_trip_used',False) or D(order['filled_quantity'])>0
        group['used']|=filled
        group['reserved']|=not order['final'] or order['phase']=='UNKNOWN'
    return sum(g['used'] for g in groups.values()),sum(g['reserved'] and not g['used'] for g in groups.values())


def active_round_trip(s,intent):
    origins=set()
    position=s['positions'].get(intent.instrument_id)
    if position:origins.update(position['originating_orders'])
    origins.update(oid for oid,o in s['orders'].items() if not o['final']
        and o['intent']['side']=='BUY' and o['intent']['instrument_id']==intent.instrument_id
        and o['intent']['episode_id']==intent.episode_id)
    groups={s['orders'][oid].get('round_trip_id',oid) for oid in origins}
    require(len(groups)<=1,'ROUND_TRIP_BINDING_UNAVAILABLE')
    if not groups:return intent.intent_id,stamp(intent.deadline),False
    group=next(iter(groups));origin=s['orders'].get(group)
    require(origin is not None,'ROUND_TRIP_BINDING_UNAVAILABLE')
    return group,origin.get('round_trip_entry_at',origin['intent']['deadline']),True


def settlement_observation_date(s, at):
    """Broker overnight settlement can expose the next business day after close."""
    at=utc(at);day=at.astimezone(NEW_YORK).date();session=current_session(s)
    if at>=session.closes_at:
        later=[datetime.fromisoformat(value).date() for value in s['config']['settlement_days'] if value>session.label.isoformat()]
        if later:return later[0]
    return day


def economic_projection(s):
    """Recompute effective accounting from granular executions plus corrections.

    Original FIFO order is immutable journal sequence, never caller time sorting.
    Corrections can revise quantity/price/fees but never instrument/order/time.
    """
    lots, settlements, positions, daily, consumed, totals = {}, {}, {}, {}, {}, {}
    settled = D(s["initial_settled"])
    # Broker-truth adoptions (CASH_ADOPT): journaled broker-minus-ledger cash differences, per bucket.
    adoptions = list(s.get("cash_adoptions", {}).values())
    settled += sum((D(a["settled"]) for a in adoptions), Z)
    gross = fees = Z
    opening=s['config'].get('opening_statement')
    if opening:
        gross,fees=D(opening['realized_gross_usd']),D(opening['fees_usd'])
        daily[s['config']['session']['label']]=gross-fees
        amount=D(opening['unsettled_usd'])
        confirmed=D(s['settled_confirmations'].get('opening-receivable','0'))
        settled+=confirmed
        settlements['opening-receivable']=dict(execution_id='opening-receivable',amount_usd=str(amount),
            confirmed_usd=str(confirmed),settlement_date=opening['settlement_date'],outstanding_usd=str(amount-confirmed))
    for receivable in s['config'].get('opening_receivables',[]):
        key='external-opening-'+receivable['evidence_id']
        amount=D(receivable['amount_usd']);confirmed=D(s['settled_confirmations'].get(key,'0'))
        settled+=confirmed
        settlements[key]=dict(execution_id=key,amount_usd=str(amount),confirmed_usd=str(confirmed),
            settlement_date=receivable['settlement_date'],outstanding_usd=str(amount-confirmed))
    for execution_id, x in sorted(s["executions"].items(), key=lambda item: (item[1]["sequence"], item[0])):
        order = s["orders"][x["intent_id"]]
        intent = order["intent"]
        instrument, episode = intent["instrument_id"], intent["episode_id"]
        q, price, fee = D(x["quantity"]), D(x["price_usd"]), D(x["fee_usd"])
        require(q >= 0 and fee >= 0 and price > 0, "INVALID_CORRECTION_VALUE")
        total = totals.setdefault(x["intent_id"], {"quantity": Z, "fees": Z})
        total["quantity"] += q
        total["fees"] += fee
        require(total["quantity"] <= D(intent["quantity"]), "OVERFILL_REQUIRES_RECONCILIATION")
        session_date = utc(x["executed_at"]).astimezone(NEW_YORK).date().isoformat()
        daily[session_date] = daily.get(session_date, Z) - fee
        fees += fee
        consumed[episode] = consumed.get(episode, Z) + fee
        if intent["side"] == "BUY":
            settled -= q * price + fee
            if q:
                lots[execution_id] = dict(lot_id=execution_id, instrument_id=instrument,
                    episode_id=episode, intent_id=x["intent_id"], quantity=str(q), entry_price_usd=str(price),
                    original_quantity=str(q), first_fill_at=x["executed_at"])
        else:
            remaining = q
            for lot in lots.values():
                if lot["instrument_id"] != instrument or lot["episode_id"] != episode:
                    continue
                used = min(remaining, D(lot["quantity"]))
                pnl = used * (price - D(lot["entry_price_usd"]))
                gross += pnl
                daily[session_date] += pnl
                consumed[episode] += max(Z, -pnl)
                lot["quantity"] = str(D(lot["quantity"]) - used)
                remaining -= used
                if remaining == 0:
                    break
            require(remaining == 0, "CORRECTION_WOULD_CREATE_UNOWNED_SALE")
            amount = q * price - fee
            require(amount >= 0, "SELL_FEES_EXCEED_PROCEEDS")
            confirmed = D(s["settled_confirmations"].get(execution_id, "0"))
            settled += confirmed
            settlements[execution_id] = dict(execution_id=execution_id, amount_usd=str(amount),
                confirmed_usd=str(confirmed), settlement_date=x["settlement_date"],
                outstanding_usd=str(amount - confirmed))
    # Each lot's original execution quantity/order sequence survives in the journal.
    for lot in lots.values():
        q = D(lot["quantity"])
        if not q:
            continue
        symbol = lot["instrument_id"]
        p = positions.setdefault(symbol, dict(instrument_id=symbol, episode_id=lot["episode_id"],
            quantity="0", basis_usd="0", entry_risk_usd="0", originating_orders=[]))
        p["quantity"] = str(D(p["quantity"]) + q)
        p["basis_usd"] = str(D(p["basis_usd"]) + q * D(lot["entry_price_usd"]))
        if lot["intent_id"] not in p["originating_orders"]:
            p["originating_orders"].append(lot["intent_id"])
    unrealized = exposure = Z
    for symbol, p in positions.items():
        protection = s["protection"].get(symbol)
        origin = s["orders"][p["originating_orders"][0]]["intent"]
        stop = D(protection["stop_usd"] if protection else origin["protection"]["initial_stop_usd"])
        # Fallback is explicitly indicative; readiness fails without a fresh mark.
        mark = D(s["marks"].get(symbol, {}).get("bid_usd", origin["maximum_entry_usd"]))
        q = D(p["quantity"])
        entry_risk = sum((D(l["quantity"]) * max(Z, D(l["entry_price_usd"]) - stop)
            for l in lots.values() if l["instrument_id"] == symbol), Z)
        costs = sum((D(s["orders"][o]["exit_cost_usd"]) + D(s["orders"][o]["stress_allowance_usd"])
            for o in p["originating_orders"]), Z)
        p.update(mark_usd=str(mark), stop_usd=str(stop), exposure_usd=str(q * mark),
            entry_risk_usd=str(entry_risk + costs),
            open_risk_usd=str(max(entry_risk, q * max(Z, mark - stop)) + costs),
            protected_quantity=str(min(q, D(protection["quantity"])) if protection else Z))
        unrealized += q * mark - D(p["basis_usd"])
        exposure += q * mark
    reservations = {}
    for order_id, order in s["orders"].items():
        intent = order["intent"]
        total = totals.get(order_id, {"quantity": Z, "fees": Z})
        order["filled_quantity"], order["fees_usd"] = str(total["quantity"]), str(total["fees"])
        if order["final"]:
            continue
        remaining = D(intent["quantity"]) - total["quantity"]
        r = dict(intent_id=order_id, instrument_id=intent["instrument_id"], episode_id=intent["episode_id"],
                 cash_usd="0", shares="0", exposure_usd="0", risk_usd="0")
        if intent["side"] == "BUY":
            price = D(intent["maximum_entry_usd"])
            fee_left = max(Z, D(order["fee_allowance_usd"]) - total["fees"])
            costs = D(order["exit_cost_usd"]) + D(order["stress_allowance_usd"]) if total["quantity"] == 0 else Z
            r.update(cash_usd=str(remaining * price + fee_left), exposure_usd=str(remaining * price),
                risk_usd=str(remaining * (price - D(intent["protection"]["initial_stop_usd"])) + fee_left + costs))
        else:
            r["shares"] = str(remaining)
        reservations[order_id] = r
    for symbol in {r["instrument_id"] for r in reservations.values()}:
        sell_reserved = sum((D(r["shares"]) for r in reservations.values() if r["instrument_id"] == symbol), Z)
        owned = D(positions.get(symbol, {}).get("quantity", "0"))
        require(sell_reserved <= owned, "CORRECTION_CONFLICTS_WITH_RESERVED_SHARES")
    unsettled = sum((D(x["outstanding_usd"]) for x in settlements.values()), Z)
    unsettled += sum((D(a["unsettled"]) for a in adoptions), Z)
    reserved = sum((D(r["cash_usd"]) for r in reservations.values()), Z)
    s.update(lots=lots, settlements=settlements, positions=positions, reservations=reservations)
    s["cash"] = dict(settled_usd=str(settled), reserved_usd=str(reserved), unsettled_usd=str(unsettled),
        realized_gross_usd=str(gross), fees_usd=str(fees), realized_net_usd=str(gross - fees),
        unrealized_usd=str(unrealized), liquidation_equity_usd=str(settled + unsettled + exposure))
    s["daily_realized"] = {day: str(value) for day, value in daily.items()}
    for episode_id, ep in s["episodes"].items():
        ep["consumed_loss_usd"] = str(max(D(ep["consumed_loss_usd"]), consumed.get(episode_id, Z)))


def risk_violations(s):
    """Approved risk definitions; owner-authorized daily downside bound is versioned."""
    issues = []
    policy=effective_policy(s)
    cash = s["cash"]
    equity = D(cash["liquidation_equity_usd"])
    basis = min(equity, D(s["day_start_equity"]))
    if equity <= 0:
        issues.append("NONPOSITIVE_EQUITY")
    if D(cash["settled_usd"]) < D(cash["reserved_usd"]):
        issues.append("INSUFFICIENT_SETTLED_CASH")
    symbols, exposure = set(s["positions"]), {}
    for symbol, p in s["positions"].items():
        exposure[symbol] = D(p["exposure_usd"])
    for r in s["reservations"].values():
        if D(r["exposure_usd"]) > 0:
            symbols.add(r["instrument_id"])
        exposure[r["instrument_id"]] = exposure.get(r["instrument_id"], Z) + D(r["exposure_usd"])
    if len(symbols) > policy.maximum_positions:
        issues.append("POSITION_LIMIT")
    if sum(exposure.values(), Z) > equity * policy.account_exposure:
        issues.append("ACCOUNT_EXPOSURE_LIMIT")
    if any(x > equity * policy.stock_exposure for x in exposure.values()):
        issues.append("STOCK_EXPOSURE_LIMIT")
    if s.get('execution_scope')=='REAL':
        deployed={iid:D(p['basis_usd']) for iid,p in s['positions'].items()}
        for reservation in s['reservations'].values():
            iid=reservation['instrument_id']
            deployed[iid]=deployed.get(iid,Z)+D(reservation['exposure_usd'])
        if sum(deployed.values(),Z)>D('500'):issues.append('REAL_DEPLOYMENT_SCOPE_LIMIT')
        if any(x>equity*policy.stock_exposure for x in deployed.values()):issues.append('REAL_SYMBOL_SCOPE_LIMIT')
    open_risk = sum((D(p["open_risk_usd"]) for p in s["positions"].values()), Z)
    open_risk += sum((D(r["risk_usd"]) for r in s["reservations"].values()), Z)
    if open_risk > basis * policy.open_risk:
        issues.append("OPEN_RISK")
    if s.get('economic_policy',{}).get('daily_downside_budget') and open_risk>daily_downside_budget(s):
        issues.append('DAILY_DOWNSIDE_BUDGET')
    for episode, ep in s["episodes"].items():
        remaining = sum((D(p["entry_risk_usd"]) for p in s["positions"].values() if p["episode_id"] == episode), Z)
        remaining += sum((D(r["risk_usd"]) for r in s["reservations"].values() if r["episode_id"] == episode), Z)
        allowance=basis*policy.trade_risk if 'economic_policy' in s else min(D(ep['allowance_usd']),basis*policy.trade_risk)
        if remaining > 0 and D(ep["consumed_loss_usd"]) + remaining > allowance:
            issues.append("EPISODE_RISK:" + episode)
    return issues


def readiness(s, event, buy=True):
    at = utc(event["logical_at"])
    session = current_session(s)
    require(session.contains(at), "CLOSED")
    rec = s["reconciliation"]
    require(rec["ready"] and rec["epoch"] == event["epoch"], "RECONCILING")
    require(at <= utc(rec["valid_until"]), "STALE_ACCOUNT")
    require(not any(i["pending_correction"] for i in s["incidents"].values()), "UNRESOLVED_CORRECTION")
    require(not any(o["phase"] == "UNKNOWN" for o in s["orders"].values()), "UNKNOWN_ORDER_STATE")
    if not buy:
        return
    require(session.permits_entry(at), "SCHEDULED_LIQUIDATION")
    require(not any(s["latches"].values()), "SAFETY_LATCHED")
    require(rec["settlement_known"], "UNKNOWN_SETTLEMENT")
    for symbol, p in s["positions"].items():
        require(D(p["protected_quantity"]) == D(p["quantity"]), "UNPROTECTED_POSITION")
        fresh_mark(s, symbol, at)


def fresh_mark(s, symbol, at):
    quote = s["marks"].get(symbol)
    require(quote is not None, "UNKNOWN_MARK")
    age = at - utc(quote["observed_at"])
    if s.get('execution_scope')=='REAL' and quote.get('valid_until') is not None:
        require(current_session(s).opens_at<=utc(quote['observed_at'])<=at
            and age<=timedelta(seconds=10)
            and utc(quote['received_at'])<=at<utc(quote['valid_until']), 'STALE_MARKET')
    else:
        require(timedelta(0) <= age <= timedelta(seconds=s["config"]["mark_max_age_seconds"]), "STALE_MARKET")
    return D(quote["bid_usd"])


def entry_mark_valid(s, intent, at):
    # Pending entries may have no position yet, so the held-position readiness
    # loop cannot validate their quote or stop geometry at the authority boundary.
    mark = fresh_mark(s, intent["instrument_id"], at)
    require(D(intent["protection"]["initial_stop_usd"]) < mark, "STOP_GEOMETRY")


def mint_permit(s, event, intent_id):
    order = s["orders"][intent_id]
    for p in s["permits"].values():
        if p["intent_id"] == intent_id and not p["consumed"]:
            p["revoked"] = True
    nonce = key(event, "permit")
    s["permits"][nonce] = dict(nonce=nonce, intent_id=intent_id,
        intent_hash=digest(order["intent"]), policy_hash=s["policy_hash"], account_id=s["account_id"],
        environment='REAL' if s.get('execution_scope')=='REAL' else 'OFFLINE', reservation_ids=[intent_id], account_state_version=event["sequence"],
        fencing_token=s["leader"]["token"], expires_at=event["permit_expires_at"], consumed=False, revoked=False)
    s["outbox"][intent_id]["nonce"] = nonce


def finalize_details(s, c):
    order = s["orders"][c["intent_id"]]
    executions = {xid: x for xid, x in s["executions"].items() if x["intent_id"] == c["intent_id"]}
    require(set(c["execution_ids"]) == set(executions) and len(c["execution_ids"]) == len(executions), "INCOMPLETE_EXECUTION_DETAILS")
    total_quantity = sum((D(x["quantity"]) for x in executions.values()), Z)
    total_fees = sum((D(x["fee_usd"]) for x in executions.values()), Z)
    require(D(c["total_quantity"]) == total_quantity and D(c["total_fees_usd"]) == total_fees, "FINAL_TOTALS_MISMATCH")
    require(c["status"] != "FILLED" or total_quantity == D(order["intent"]["quantity"]), "UNFILLED_ORDER")
    order.update(final=True, phase=c["status"])
    s["outbox"][c["intent_id"]]["status"] = "FINAL"
    for permit in s["permits"].values():
        if permit["intent_id"] == c["intent_id"] and not permit["consumed"]:
            permit["revoked"] = True


def refresh_latches(s):
    policy=effective_policy(s)
    equity = D(s["cash"]["liquidation_equity_usd"])
    historic = max(D(s["historic_high_water"]), equity)
    s["historic_high_water"] = str(historic)
    segment = s["segments"][-1]
    high = max(D(segment["high_water_usd"]), equity)
    segment["high_water_usd"] = str(high)
    day_start = D(s["day_start_equity"])
    daily_realized = D(s["daily_realized"].get(s["session"]["label"], "0"))
    if daily_realized <= -day_start * policy.daily_loss or equity <= day_start * (1 - policy.daily_loss):
        s["latches"]["daily"] = True
    if equity <= high * (1 - policy.drawdown):
        s["latches"]["drawdown"] = True
    if equity <= historic * (1 - policy.experiment_death):
        s["latches"]["terminated"] = True
    # Breaches are response flags; no automatic liquidation or extra entry allowance.
    s["risk_breaches"] = risk_violations(s)


def reduce_event(previous, event):
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.traps[Inexact] = True
        return _reduce(previous, event)


def _reduce(previous, event):
    c, kind = event["command"], event["command"]["kind"]
    at = utc(event["logical_at"])
    if kind == "CREATE":
        require(previous is None, "ACCOUNT_ALREADY_EXISTS")
        s = dict(account_id=c["account_id"], experiment_id=c["experiment_id"], owner_id=c["owner_id"],
            initial_settled=c["initial_settled_usd"], day_start_equity=c["initial_settled_usd"],
            historic_high_water=c["initial_settled_usd"], policy_hash=payload_hash(POLICY),
            config=c, session=c["session"], instruments={x["instrument_id"]: x for x in c["instruments"]},
            orders={}, executions={}, lots={}, positions={}, marks={}, protection={}, episodes={},
            settled_confirmations={}, settlements={}, reservations={}, permits={}, outbox={}, restarts={}, incidents={},
            latches=dict(daily=False, drawdown=False, terminated=False, incident=False),
            leader=dict(holder=None, token=0, expires_at=None, epoch=None, last_clock_at=event["server_at"]),
            reconciliation=dict(ready=False, settlement_known=False, epoch=None, evidence_id=None, valid_until=event["logical_at"]),
            segments=[dict(segment_id="initial", reference_usd=c["initial_settled_usd"], high_water_usd=c["initial_settled_usd"],
                           started_at=event["logical_at"], restart_action_id=None)])
        if event.get('execution_scope')=='REAL':
            require(c['capabilities']['environment']=='REAL','CAPABILITY_ENVIRONMENT_MISMATCH')
            s['execution_scope']='REAL'
            if c.get('risk_policy'):s['real_policy']=dict(c['risk_policy'])
            s['policy_hash']=payload_hash(effective_policy(s))
            opening_equity=D(c['initial_settled_usd'])+sum((D(r['amount_usd']) for r in c.get('opening_receivables',[])),Z)
            s['day_start_equity']=s['historic_high_water']=str(opening_equity)
            s['segments'][0].update(reference_usd=str(opening_equity),high_water_usd=str(opening_equity))
        opening=c.get('opening_statement')
        if opening:
            s['day_start_equity']=opening['day_start_equity_usd']
            s['historic_high_water']=opening['historic_high_water_usd']
            s['segments'][0].update(reference_usd=opening['experiment_reference_usd'],high_water_usd=opening['historic_high_water_usd'])
            s['episodes'][opening['episode_id']]=dict(instrument_id=opening['instrument_id'],
                consumed_loss_usd=opening['consumed_loss_usd'],
                allowance_usd=str(min(D(c['initial_settled_usd'])+D(opening['unsettled_usd']),D(s['day_start_equity']))*effective_policy(s).trade_risk))
    else:
        require(previous is not None, "UNKNOWN_ACCOUNT")
        s = deepcopy(previous)
        require((s.get('execution_scope')=='REAL') == (event.get('execution_scope')=='REAL'), 'LEDGER_ENVIRONMENT_MISMATCH')
        require(at >= utc(s["last_logical_at"]), "CLOCK_REGRESSION")
        if kind == "LEASE":
            s["leader"] = c["leader"]
            if c["new_term"]:
                s["reconciliation"]["ready"] = False
            if c["recovery"]:
                for oid, order in s["orders"].items():
                    if not order["final"]:
                        order["phase"] = "UNKNOWN"
                        s["outbox"][oid]["status"] = "RECOVERY_UNCERTAIN"
                for p in s["permits"].values():
                    if not p["consumed"]:
                        p["revoked"] = True
        elif kind == 'AUTHORIZE_SANDBOX_ECONOMICS':
            require(event.get('policy_scope')=='SANDBOX','SANDBOX_POLICY_SCOPE_REQUIRED')
            require('economic_policy' not in s,'POLICY_ALREADY_AUTHORIZED')
            default_version=event.get('economic_policy_default_version')
            require(default_version in (None,1),'UNKNOWN_ECONOMIC_POLICY_DEFAULT_VERSION')
            policy=SandboxEconomics(model_budget_usd='100') if default_version is None else SandboxEconomics()
            s['economic_policy']=dict(revision=1,scope='SANDBOX',origin='OWNER_DEFAULTS',
                authorization_id=c['authorization_id'],policy=policy.model_dump(mode='json'),
                effective_at=event['logical_at'],event_id=event['event_id'])
            s['policy_hash']=digest(s['economic_policy'])
            s['policy_history']=[deepcopy(s['economic_policy'])|{'hash':s['policy_hash']}]
        elif kind == 'RELEASE_SANDBOX_PREFERENCES':
            require(event.get('policy_scope')=='SANDBOX' and s.get('economic_policy',{}).get('scope')=='SANDBOX',
                'SANDBOX_POLICY_SCOPE_REQUIRED')
            require(not s['economic_policy'].get('preferences_released'),'PREFERENCES_ALREADY_RELEASED')
            require(not s['positions'] and not s['reservations'] and all(o['final'] for o in s['orders'].values()),
                'POLICY_ORDER_AUTHORITY_ACTIVE')
            rec=s['reconciliation']
            require(rec['ready'] and rec['epoch']==event['epoch'] and at<=utc(rec['valid_until']),'RECONCILING')
            require(rec['settlement_known'],'UNKNOWN_SETTLEMENT')
            require(not any(s['latches'].values()) and not any(i['pending_correction'] for i in s['incidents'].values()),
                'SAFETY_LATCHED')
            old=effective_policy(s)
            # No capital allocation, planned-loss budget, or planning cadence
            # is prescribed. Retain cash/equity, catastrophic loss and certified
            # operational capacity boundaries, with all history intact.
            policy=SandboxEconomics(**(old.model_dump()|dict(stock_exposure=old.account_exposure,
                trade_risk=old.account_exposure,open_risk=old.account_exposure,
                maximum_positions=9,deep_min_interval_seconds=0,calls_per_300s=120)))
            s['economic_policy']=dict(revision=policy_revision(s)+1,scope='SANDBOX',origin='OWNER_PREFERENCES_RELEASED',
                authorization_id=c['authorization_id'],preferences_released=True,policy=policy.model_dump(mode='json'),
                effective_at=event['logical_at'],event_id=event['event_id'])
            s['policy_hash']=digest(s['economic_policy'])
            s['policy_history'].append(deepcopy(s['economic_policy'])|{'hash':s['policy_hash']})
        elif kind == 'AUTHORIZE_DAILY_DOWNSIDE_BUDGET':
            old=s.get('economic_policy',{})
            require(event.get('policy_scope')=='SANDBOX' and old.get('scope')=='SANDBOX'
                and old.get('preferences_released'),'SANDBOX_POLICY_SCOPE_REQUIRED')
            require(not old.get('daily_downside_budget'),'DAILY_DOWNSIDE_ALREADY_AUTHORIZED')
            require(not s['positions'] and not s['reservations'] and all(o['final'] for o in s['orders'].values()),
                'POLICY_ORDER_AUTHORITY_ACTIVE')
            rec=s['reconciliation']
            require(rec['ready'] and rec['epoch']==event['epoch'] and at<=utc(rec['valid_until']),'RECONCILING')
            require(rec['settlement_known'],'UNKNOWN_SETTLEMENT')
            require(not any(s['latches'].values()) and not any(i['pending_correction'] for i in s['incidents'].values()),
                'SAFETY_LATCHED')
            s['economic_policy']={**old,'revision':policy_revision(s)+1,'daily_downside_budget':True,
                'origin':'OWNER_CATASTROPHIC_DOWNSIDE_BOUND','authorization_id':c['authorization_id'],
                'effective_at':event['logical_at'],'event_id':event['event_id']}
            s['policy_hash']=digest(s['economic_policy'])
            s['policy_history'].append(deepcopy(s['economic_policy'])|{'hash':s['policy_hash']})
        elif kind == 'AUTHORIZE_MODEL_BUDGET_CAP':
            old=s.get('economic_policy',{})
            require(event.get('policy_scope')=='SANDBOX' and old.get('scope')=='SANDBOX',
                'SANDBOX_POLICY_SCOPE_REQUIRED')
            previous=old.get('model_budget_cap_authorization_id')
            allowed_upgrade=(
                c['authorization_id']=='owner-model-budget-cap-225-20260924'
                and previous=='owner-model-budget-cap-125-20260923') or (
                c['authorization_id']=='owner-model-budget-cap-275-20260925'
                and previous=='owner-model-budget-cap-225-20260924')
            require(not old.get('model_budget_cap_authorized') or allowed_upgrade,
                'MODEL_BUDGET_CAP_ALREADY_AUTHORIZED')
            require(not s['positions'] and not s['reservations'] and all(o['final'] for o in s['orders'].values()),
                'POLICY_ORDER_AUTHORITY_ACTIVE')
            rec=s['reconciliation']
            require(rec['ready'] and rec['epoch']==event['epoch'] and at<=utc(rec['valid_until']),'RECONCILING')
            require(rec['settlement_known'],'UNKNOWN_SETTLEMENT')
            require(not any(s['latches'].values()) and not any(i['pending_correction'] for i in s['incidents'].values()),
                'SAFETY_LATCHED')
            cap={
                'owner-model-budget-cap-125-20260923':D('125'),
                'owner-model-budget-cap-225-20260924':D('225'),
                'owner-model-budget-cap-275-20260925':D('275'),
            }[c['authorization_id']]
            policy=effective_policy(s).model_copy(update={'model_budget_usd':cap})
            s['economic_policy']={**old,'revision':policy_revision(s)+1,
                'origin':'OWNER_MODEL_BUDGET_CAP','model_budget_cap_authorization_id':c['authorization_id'],
                'model_budget_cap_authorized':True,'policy':policy.model_dump(mode='json'),
                'effective_at':event['logical_at'],'event_id':event['event_id']}
            s['policy_hash']=digest(s['economic_policy'])
            s['policy_history'].append(deepcopy(s['economic_policy'])|{'hash':s['policy_hash']})
        elif kind == 'REVISE_SANDBOX_ECONOMICS':
            require(event.get('policy_scope')=='SANDBOX' and s.get('economic_policy',{}).get('scope')=='SANDBOX',
                'SANDBOX_POLICY_SCOPE_REQUIRED')
            proposal=PolicyProposal.model_validate(c['proposal'])
            if s['economic_policy'].get('preferences_released'):
                previous_policy=effective_policy(s)
                require(not proposal.release_halts and all(getattr(proposal.policy,k)==getattr(previous_policy,k)
                    for k in ('daily_loss','drawdown','experiment_death')),'CATASTROPHIC_POLICY_IMMUTABLE')
            require(proposal.based_revision==policy_revision(s) and proposal.based_policy_hash==s['policy_hash'],'STALE_POLICY_VERSION')
            require(utc(c['context_at'])<=utc(c['response_at'])<=at<=utc(c['context_at'])+timedelta(seconds=90),'STALE_POLICY_CONTEXT')
            require(c['account_fingerprint']==account_fingerprint(s),'POLICY_ACCOUNT_CHANGED')
            readiness(s,event,buy=False)
            require(s['reconciliation']['settlement_known'],'UNKNOWN_SETTLEMENT')
            require(not s['latches']['incident'],'OPERATIONAL_LATCH_BLOCKS_POLICY')
            require(all(o['final'] for o in s['orders'].values()),'POLICY_ORDER_AUTHORITY_ACTIVE')
            reconciled_at=utc(s['reconciliation']['valid_until'])-timedelta(seconds=s['config']['reconciliation_max_age_seconds'])
            require(reconciled_at>=utc(c['response_at']),'POLICY_REQUIRES_POST_RESPONSE_RECONCILIATION')
            for symbol in s['positions']:fresh_mark(s,symbol,at)
            require(D(s['cash']['liquidation_equity_usd'])>0,'NONPOSITIVE_EQUITY')
            require(all(s['latches'][name] for name in proposal.release_halts),'POLICY_HALT_NOT_ACTIVE')
            s['economic_policy']=dict(revision=policy_revision(s)+1,scope='SANDBOX',origin='ASTRA',
                authorization_id=s['economic_policy']['authorization_id'],policy=proposal.policy.model_dump(mode='json'),
                effective_at=event['logical_at'],event_id=event['event_id'],decision=deepcopy(c),
                **({'preferences_released':True} if s['economic_policy'].get('preferences_released') else {}),
                **({'daily_downside_budget':True} if s['economic_policy'].get('daily_downside_budget') else {}),
                **({'model_budget_cap_authorized':True,
                    'model_budget_cap_authorization_id':s['economic_policy'].get('model_budget_cap_authorization_id')}
                    if s['economic_policy'].get('model_budget_cap_authorized') else {}))
            for name in proposal.release_halts:s['latches'][name]=False
            refresh_latches(s)
            require(not any(s['latches'][name] for name in proposal.release_halts),'POLICY_RELEASE_THRESHOLD_STILL_BREACHED')
            require(not risk_violations(s),'POLICY_CONFLICTS_WITH_ACTIVE_EXPOSURE')
            s['policy_hash']=digest(s['economic_policy'])
            s['policy_history'].append(deepcopy(s['economic_policy'])|{'hash':s['policy_hash']})
        elif kind == "REGISTER_INSTRUMENTS":
            for raw in c["instruments"]:
                item=Instrument.model_validate(raw)
                require(item.country=="US" and item.asset_type=="STOCK" and item.liquid
                    and item.tradable and not item.halted, "INELIGIBLE_STOCK")
                old=s["instruments"].get(item.instrument_id)
                require(old is None or old==item.model_dump(mode="json"), "INSTRUMENT_REFERENCE_CONFLICT")
                s["instruments"][item.instrument_id]=item.model_dump(mode="json")
        elif kind == "MARK":
            apply_mark(s, c, at)
        elif kind == "ADMIT":
            intent = OrderIntent.model_validate(c["intent"])
            require(intent.environment==('REAL' if s.get('execution_scope')=='REAL' else 'OFFLINE'), 'INTENT_ENVIRONMENT_MISMATCH')
            readiness(s, event, intent.side == "BUY")
            require(intent.account_id == s["account_id"] and at < intent.deadline, "INTENT_BINDING_OR_EXPIRY")
            require(intent.intent_id not in s["orders"], "DUPLICATE_INTENT")
            require(all(o["intent"]["client_order_id"] != intent.client_order_id for o in s["orders"].values()), "DUPLICATE_CLIENT_ID")
            require(intent.instrument_id in s["instruments"], "UNKNOWN_INSTRUMENT")
            instrument = Instrument.model_validate(s["instruments"][intent.instrument_id])
            require(instrument.country == "US" and instrument.asset_type == "STOCK" and instrument.liquid
                and instrument.tradable and not instrument.halted, "INELIGIBLE_STOCK")
            require(aligned(intent.quantity, instrument.quantity_increment) and intent.quantity >= instrument.minimum_quantity, "QUANTITY_INCREMENT")
            for price in (intent.limit_price_usd, intent.trigger_price_usd, intent.maximum_entry_usd,
                          intent.protection.initial_stop_usd if intent.protection else None):
                require(price is None or aligned(price, instrument.price_increment_usd), "PRICE_INCREMENT")
            require(intent.capability_version == s["config"]["capabilities"]["version"], "CAPABILITY_VERSION")
            if intent.side == "BUY":
                if event.get('entry_submission_limit_version') in (2,3):
                    require(s.get('execution_scope')=='REAL','REAL_ROUND_TRIP_SCOPE_REQUIRED')
                    round_trip_id,round_trip_entry_at,existing_round_trip=active_round_trip(s,intent)
                    used,reserved=round_trip_capacity(s,at)
                    require(event.get('entry_submission_limit_version')==3 or existing_round_trip or used+reserved<MAX_REAL_ROUND_TRIPS_PER_EXCHANGE_WEEK,'WEEKLY_ROUND_TRIP_LIMIT')
                if 'economic_policy' in s:require(c.get('policy_hash')==s['policy_hash'],'STALE_POLICY_VERSION')
                mark = fresh_mark(s, intent.instrument_id, at)
                caps = s["config"]["capabilities"]
                require(intent.order_type in caps["entry_order_types"] and intent.protection.mechanism in caps["protection_mechanisms"], "UNSUPPORTED_CAPABILITY")
                require(aligned(intent.quantity, D("1")) or caps["fractional_protection"], "FRACTIONAL_PROTECTION_UNCERTIFIED")
                require(intent.protection.initial_stop_usd < mark, "STOP_GEOMETRY")
                require(intent.deadline <= utc(c["latest_exit_at"]) <= exit_horizon(s), "EXIT_DEADLINE")
                require(active_episode_ids(s,intent.instrument_id)<= {intent.episode_id}, "EPISODE_FRAGMENTATION")
                require(intent.episode_id not in s["episodes"] or s["episodes"][intent.episode_id]["instrument_id"] == intent.instrument_id, "EPISODE_BINDING")
                if intent.instrument_id in s["positions"]:
                    require(intent.protection.initial_stop_usd == D(s["positions"][intent.instrument_id]["stop_usd"]), "ADD_STOP_MUST_MATCH_ACTIVE_STOP")
                s["episodes"].setdefault(intent.episode_id, dict(instrument_id=intent.instrument_id, consumed_loss_usd="0",
                    allowance_usd=str(min(D(s["cash"]["liquidation_equity_usd"]), D(s["day_start_equity"])) * effective_policy(s).trade_risk)))
            else:
                p = s["positions"].get(intent.instrument_id)
                require(p is not None and p["episode_id"] == intent.episode_id, "OWNED_POSITION_REQUIRED")
                shares = sum((D(r["shares"]) for r in s["reservations"].values() if r["instrument_id"] == intent.instrument_id), Z)
                require(intent.quantity <= D(p["quantity"]) - shares, "SHARES_ALREADY_RESERVED")
            s["orders"][intent.intent_id] = {**c, "phase": "RESERVED", "final": False, "filled_quantity": "0", "fees_usd": "0"}
            if intent.side=='BUY' and event.get('entry_submission_limit_version') in (2,3):
                s['orders'][intent.intent_id].update(round_trip_id=round_trip_id,round_trip_entry_at=round_trip_entry_at)
            s["outbox"][intent.intent_id] = dict(client_order_id=intent.client_order_id, status="READY", nonce=None)
            economic_projection(s)
            if intent.side == "BUY":
                require(not risk_violations(s), ",".join(risk_violations(s)))
            mint_permit(s, event, intent.intent_id)
        elif kind == "REFRESH_PERMIT":
            order = s["orders"][c["intent_id"]]
            require(order["phase"] == "RESERVED" and not order["final"], "NO_RETRY_OF_DISPATCHED_INTENT")
            readiness(s, event, order["intent"]["side"] == "BUY")
            require(at < utc(order["intent"]["deadline"]), "PLAN_EXPIRED")
            if order["intent"]["side"] == "BUY":
                entry_mark_valid(s, order["intent"], at)
                require(not risk_violations(s), "RISK_REVALIDATION_FAILED")
            mint_permit(s, event, c["intent_id"])
        elif kind == "CONSUME":
            p = s["permits"].get(c["nonce"])
            require(p and not p["consumed"] and not p["revoked"], "PERMIT_UNAVAILABLE")
            order = s["orders"][p["intent_id"]]
            require(p["fencing_token"] == s["leader"]["token"] and p["account_state_version"] == s["version"], "STALE_PERMIT")
            require(utc(event["server_at"]) < utc(p["expires_at"]), "PERMIT_EXPIRED")
            require(at < utc(order["intent"]["deadline"]) and p["intent_hash"] == digest(order["intent"]), "INTENT_EXPIRED_OR_CHANGED")
            readiness(s, event, order["intent"]["side"] == "BUY")
            if order["intent"]["side"] == "BUY":
                limit_version=event.get('entry_submission_limit_version')
                require(limit_version in (None,1,2,3),'UNKNOWN_ENTRY_SUBMISSION_LIMIT_VERSION')
                if limit_version==1:
                    session_entries,week_entries=entry_submission_counts(s,at)
                    require(week_entries<MAX_ENTRY_SUBMISSIONS_PER_EXCHANGE_WEEK,'WEEKLY_ENTRY_SUBMISSION_LIMIT')
                    require(session_entries<MAX_ENTRY_SUBMISSIONS_PER_SESSION,'SESSION_ENTRY_SUBMISSION_LIMIT')
                elif limit_version in (2,3):
                    require(s.get('execution_scope')=='REAL','REAL_ROUND_TRIP_SCOPE_REQUIRED')
                    used,reserved=round_trip_capacity(s,at)
                    require(limit_version==3 or used+reserved<=MAX_REAL_ROUND_TRIPS_PER_EXCHANGE_WEEK,'WEEKLY_ROUND_TRIP_LIMIT')
                entry_mark_valid(s, order["intent"], at)
                require(not risk_violations(s), "RISK_REVALIDATION_FAILED")
            p["consumed"] = True
            p["consumed_at"] = event["server_at"]
            s["outbox"][p["intent_id"]]["status"] = "CLAIMED_UNCERTAIN"
            order["phase"] = "UNKNOWN"
            s["reconciliation"]["ready"] = False
        elif kind == "ORDER_OBSERVATION":
            order = s["orders"][c["intent_id"]]
            require(order["phase"] != "RESERVED" and not order["final"], "ORDER_NOT_DISPATCHED")
            order["phase"] = c["status"]
            if c["status"] == "ACKNOWLEDGED":
                s["outbox"][c["intent_id"]]["status"] = "ACKNOWLEDGED"
            else:
                s["reconciliation"]["ready"] = False
        elif kind == "EXECUTION":
            require(c["execution_id"] not in s["executions"], "DUPLICATE_EXECUTION_ID")
            order = s["orders"][c["intent_id"]]
            require(order["phase"] != "RESERVED", "EXECUTION_BEFORE_DISPATCH")
            require(utc(c["executed_at"]) <= at, "FUTURE_EXECUTION")
            require((c["settlement_date"] is not None) == (order["intent"]["side"] == "SELL_OWNED"), "SETTLEMENT_DATE_REQUIRED_FOR_SELL_ONLY")
            if c["settlement_date"]:
                trade_date = utc(c["executed_at"]).astimezone(NEW_YORK).date().isoformat()
                later = [d for d in s["config"]["settlement_days"] if d > trade_date]
                require(later and c["settlement_date"] == later[0], "UNKNOWN_OR_INCORRECT_SETTLEMENT_DATE")
            s["executions"][c["execution_id"]] = {**c, "revision": 0, "sequence": event["sequence"]}
            if (event.get('entry_submission_limit_version') in (2,3)
                    and order['intent']['side']=='BUY' and D(c['quantity'])>0):
                order['round_trip_used']=True
            symbol = order["intent"]["instrument_id"]
            if order["intent"]["side"] == "BUY" and symbol not in s["positions"]:
                s["protection"].pop(symbol, None)
            elif order["intent"]["side"] == "SELL_OWNED" and symbol in s["protection"]:
                s["protection"][symbol]["quantity"] = str(max(Z, D(s["protection"][symbol]["quantity"]) - D(c["quantity"])))
            if D(order["fees_usd"]) + D(c["fee_usd"]) > D(order["fee_allowance_usd"]):
                incident(s, event, "EXECUTION_FEES_EXCEED_RESERVED_ALLOWANCE")
            if order["final"]:
                order["final"] = False
                order["phase"] = "UNKNOWN"
                incident(s, event, "LATE_EXECUTION_AFTER_FINALITY")
            # Protection coverage cannot silently grow with the position.
            if not current_session(s).contains(utc(c["executed_at"])):
                incident(s, event, "EXECUTION_OUTSIDE_SESSION")
            limit = order["intent"]["limit_price_usd"]
            if limit and ((order["intent"]["side"] == "BUY" and D(c["price_usd"]) > D(limit))
                          or (order["intent"]["side"] == "SELL_OWNED" and D(c["price_usd"]) < D(limit))):
                incident(s, event, "EXECUTION_PRICE_OUTSIDE_BOUND")
        elif kind == "CORRECT_EXECUTIONS":
            candidate = deepcopy(s)
            try:
                seen = set()
                for change in c["changes"]:
                    require(change["execution_id"] not in seen, "DUPLICATE_CORRECTION_TARGET")
                    seen.add(change["execution_id"])
                    x = candidate["executions"][change["execution_id"]]
                    require(x["revision"] == change["expected_revision"], "STALE_CORRECTION")
                    x.update(quantity=change["quantity"], price_usd=change["price_usd"], fee_usd=change["fee_usd"], revision=x["revision"] + 1)
                    order = candidate["orders"][x["intent_id"]]
                    candidate["protection"].pop(order["intent"]["instrument_id"], None)
                    order["final"] = False
                    order["phase"] = "UNKNOWN"
                for detail in c["terminal_details"]:
                    finalize_details(candidate, detail)
                economic_projection(candidate)
                for resolved in c["resolves_correction_ids"]:
                    require(resolved in candidate["incidents"] and candidate["incidents"][resolved]["pending_correction"], "UNKNOWN_PENDING_CORRECTION")
                    candidate["incidents"][resolved]["pending_correction"] = False
                s = candidate
                incident(s, event, "CORRECTION_APPLIED_RECONCILIATION_REQUIRED")
            except (Rejected, KeyError):
                incident(s, event, "CORRECTION_QUARANTINED_DEPENDENCY_OR_REVISION_CONFLICT", pending=True)
        elif kind == "FINALIZE":
            finalize_details(s, c)
        elif kind == "SETTLE":
            lot = s["settlements"][c["execution_id"]]
            require(lot["settlement_date"] <= c["effective_on"] <= settlement_observation_date(s,at).isoformat()
                and c["effective_on"] in s["config"]["settlement_days"], "SETTLEMENT_NOT_DUE_OR_UNKNOWN_DAY")
            require(D(c["amount_usd"]) == D(lot["amount_usd"]), "SETTLEMENT_AMOUNT_MISMATCH")
            s["settled_confirmations"][c["execution_id"]] = c["amount_usd"]
        elif kind == "CASH_ADOPT":
            require(s.get("execution_scope") == "REAL", "REAL_SCOPE_REQUIRED")
            require(not s["reservations"] and all(o["final"] and o.get("phase") != "UNKNOWN" for o in s["orders"].values()),
                "CASH_ADOPTION_REQUIRES_QUIET_ACCOUNT")
            require(D(s["cash"]["settled_usd"]) + D(c["settled_delta_usd"]) == D(c["broker_settled_usd"])
                and D(s["cash"]["unsettled_usd"]) + D(c["unsettled_delta_usd"]) == D(c["broker_unsettled_usd"]),
                "CASH_ADOPTION_ARITHMETIC")
            s.setdefault("cash_adoptions", {})[event["event_id"]] = dict(settled=str(D(c["settled_delta_usd"])),
                unsettled=str(D(c["unsettled_delta_usd"])))
            # A cash flow, not performance: move every equity reference with it so no loss, drawdown or
            # termination latch can trip from an adoption alone (and a deposit never inflates high-water).
            flow = D(c["settled_delta_usd"]) + D(c["unsettled_delta_usd"])
            s["day_start_equity"] = str(max(Z, D(s["day_start_equity"]) + flow))
            s["historic_high_water"] = str(max(Z, D(s["historic_high_water"]) + flow))
            segment = s["segments"][-1]
            segment["high_water_usd"] = str(max(Z, D(segment["high_water_usd"]) + flow))
            if "reference_usd" in segment:
                segment["reference_usd"] = str(max(Z, D(segment["reference_usd"]) + flow))
        elif kind == "PROTECTION":
            p = s["positions"].get(c["instrument_id"])
            require(p is not None and D(c["quantity"]) <= D(p["quantity"]), "PROTECTION_QUANTITY")
            require(D(p["stop_usd"]) <= D(c["stop_usd"]) < D(p["mark_usd"]), "STOP_LOOSENING_OR_GEOMETRY")
            s["protection"][c["instrument_id"]] = c
            if D(c["quantity"]) < D(p["quantity"]):
                incident(s, event, "PROTECTION_FAILURE")
        elif kind == "RECONCILE":
            expected_positions = {symbol: p["quantity"] for symbol, p in s["positions"].items()}
            observed = {symbol: str(D(q)) for symbol, q in c["positions"]}
            position_match = (len(observed) == len(c["positions"]) and set(observed) == set(expected_positions)
                and all(D(observed[k]) == D(v) for k, v in expected_positions.items()))
            outstanding = {k for k, o in s["orders"].items() if not o["final"]}
            observed_outstanding=set(c["outstanding_intent_ids"])
            cash_match=(D(c["settled_usd"]) == D(s["cash"]["settled_usd"])
                and D(c["unsettled_usd"]) == D(s["cash"]["unsettled_usd"]))
            unique_outstanding=len(c["outstanding_intent_ids"]) == len(observed_outstanding)
            no_unknown=not any(o["phase"] == "UNKNOWN" for o in s["orders"].values())
            outstanding_match=(observed_outstanding == outstanding and unique_outstanding)
            clean = cash_match and position_match and outstanding_match and no_unknown
            if clean:
                for resolved in c["resolved_incident_ids"]:
                    require(resolved in s["incidents"] and not s["incidents"][resolved]["pending_correction"], "UNRESOLVED_CORRECTION")
                    del s["incidents"][resolved]
                s["latches"]["incident"] = bool(s["incidents"])
            else:
                missing=outstanding-observed_outstanding;unexpected=observed_outstanding-outstanding
                cancel_only=(c.get("reconciliation_protocol")=="CANCEL_RECOVERY_V1"
                    and cash_match and position_match and unique_outstanding and no_unknown and missing and not unexpected
                    and all(s["orders"][intent_id]["phase"]=="CANCEL_PENDING" for intent_id in missing))
                intent_ids=sorted(missing)
                duplicate=cancel_only and any(x.get("scope")=="CANCEL_PENDING_OUTSTANDING_ONLY"
                    and x.get("intent_ids")==intent_ids for x in s["incidents"].values())
                if duplicate:
                    s["reconciliation"]["ready"] = False;s["latches"]["incident"] = True
                else:
                    incident(s, event, "RECONCILIATION_MISMATCH")
                    if cancel_only:
                        s["incidents"][event["event_id"]].update(
                            scope="CANCEL_PENDING_OUTSTANDING_ONLY",intent_ids=intent_ids)
            s["reconciliation"] = dict(ready=clean, settlement_known=c["settlement_known"], epoch=event["epoch"],
                evidence_id=c["evidence_id"], valid_until=stamp(at + timedelta(seconds=s["config"]["reconciliation_max_age_seconds"])))
        elif kind == "REAL_CONFIGURATION":
            require(s.get("execution_scope") == "REAL", "REAL_SCOPE_REQUIRED")
            label = current_session(s).label.isoformat()
            existing = s["config"]["settlement_days"]
            require(set(d for d in existing if d >= label) <= set(c["settlement_days"]), "SETTLEMENT_DAYS_REMOVED")
            s["config"]["settlement_days"] = sorted(set(existing) | set(c["settlement_days"]))
            s["config"]["carry_sessions"] = c["carry_sessions"]
            if c["policy_hash"] != s["policy_hash"]:
                # A revision carries the full limits; the hash must match them (journal-bound policy).
                require(bool(c.get("risk_policy")), "POLICY_VALUES_REQUIRED")
                from ..economic_policy import real_policy_from
                require(payload_hash(real_policy_from(c["risk_policy"])) == c["policy_hash"], "POLICY_HASH_MISMATCH")
                s.setdefault("policy_history", []).append(dict(hash=c["policy_hash"], previous=s["policy_hash"],
                    charter_id=c["charter_id"], at=event["logical_at"]))
                s["real_policy"] = dict(c["risk_policy"])
                s["policy_hash"] = c["policy_hash"]
        elif kind == "NEW_SESSION":
            session = Session.model_validate(c["session"])
            require(session.label > current_session(s).label and session.contains(at), "INVALID_NEW_SESSION")
            require(s["reconciliation"]["ready"] and s["reconciliation"]["epoch"] == event["epoch"], "RECONCILING")
            require(at <= utc(s["reconciliation"]["valid_until"]), "STALE_ACCOUNT")
            require(D(s["cash"]["liquidation_equity_usd"]) > 0, "NONPOSITIVE_EQUITY")
            s["session"] = c["session"]
            for mark in c.get("opening_marks") or ():
                require(mark["instrument_id"] in s["positions"], "OPENING_MARK_NOT_HELD")
                apply_mark(s, mark, at)
            if c.get("opening_marks"):
                economic_projection(s)
            s["day_start_equity"] = s["cash"]["liquidation_equity_usd"]
            s["latches"]["daily"] = False
        elif kind == "RESTART":
            action = c["action"]
            require(event.get("authenticated_owner") == s["owner_id"] == action["owner_id"], "AUTHENTICATED_OWNER_REQUIRED")
            require(s["latches"]["drawdown"] and not s["latches"]["terminated"], "RESTART_NOT_PERMITTED")
            require(action["experiment_id"] == s["experiment_id"] and action["segment_id"] == s["segments"][-1]["segment_id"], "RESTART_BINDING")
            require(action["action_id"] not in s["restarts"] and action["action_id"] != "initial", "RESTART_REPLAY")
            require(D(action["expected_equity_usd"]) == D(s["cash"]["liquidation_equity_usd"]), "RESTART_EQUITY_CHANGED")
            require(utc(action["issued_at"]) <= at < utc(action["expires_at"]), "RESTART_EXPIRED")
            s["restarts"][action["action_id"]] = {"action": action, "authenticated_owner": event["authenticated_owner"], "event_id": event["event_id"]}
            equity = s["cash"]["liquidation_equity_usd"]
            s["segments"].append(dict(segment_id=action["action_id"], reference_usd=equity, high_water_usd=equity,
                started_at=event["logical_at"], restart_action_id=action["action_id"]))
            s["latches"]["drawdown"] = False
        elif kind == "QUARANTINE":
            # An impossible observed execution is unresolved economic evidence,
            # not an ordinary incident that matching the old checkpoint can clear.
            incident(s, event, c["reason"], pending=True)
            oid = c["original_command"].get("intent_id")
            if oid in s["orders"]:
                s["orders"][oid]["phase"] = "UNKNOWN"
                s["outbox"][oid]["status"] = "RECOVERY_UNCERTAIN"
        elif kind == "INCIDENT":
            incident(s, event, c["reason"])
        elif kind == "REJECTED":
            pass  # Audited rejection has no new trading authority.
        else:
            raise Rejected("UNKNOWN_EVENT_KIND")
    economic_projection(s)
    refresh_latches(s)
    s["version"] = event["sequence"]
    s["last_logical_at"] = event["logical_at"]
    s["leader"]["last_clock_at"] = event["server_at"]
    return s
