"""Read-only trader watcher. Reads status files only; no database, broker, key or trading-code access.

Detects events (process death, restarts, stale heartbeat, blocked launch, state changes, session end),
appends them to .local/watcher/events.jsonl and pushes a short text to a private ntfy topic.
Lives outside scripts/ so it is not part of the trading build digest.
"""
import json,os,sys,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
SUP=ROOT/'.local'/'real-supervisor'
READY=ROOT/'.local'/'real-readiness'
OUT=ROOT/'.local'/'watcher'
POLL=30
STALE_SECONDS=90          # trader writes about every 3 s
OPEN_MINUTE=13*60+30      # 13:30Z; the late-October open is 13:30Z (EDT). Alert if nothing runs by +25 min.
OPEN_GRACE=25
CLOSE_MINUTE=20*60


def read_json(path):
    try:
        with open(path,'rb') as f:data=f.read()    # held for microseconds
        return json.loads(data.decode('utf-8'))
    except Exception:
        return None


def count_lines(path):
    try:
        with open(path,'rb') as f:return sum(1 for _ in f)
    except Exception:
        return 0


def parse(stamp):
    try:return datetime.fromisoformat(stamp)
    except Exception:return None


def snapshot(now):
    hb=read_json(SUP/'child-heartbeat.json') or {}
    sup=read_json(SUP/'supervisor-status.json') or {}
    blocked=sorted(p.name for p in READY.glob('daily-blocked-*.json')) if READY.is_dir() else []
    at=parse(hb.get('at','')) if hb else None
    return {'hb':hb,'sup':sup,'age':(now-at).total_seconds() if at else None,
        'attempts':count_lines(SUP/'attempt-history.jsonl'),
        'failures':count_lines(SUP/'child-failure.log'),'blocked':blocked}


def market_hours(now):
    minute=now.hour*60+now.minute
    return now.weekday()<5 and OPEN_MINUTE<=minute<CLOSE_MINUTE


def decide(prev,cur,now):
    """Pure function: (previous state, current snapshot, now) -> list of (key, message)."""
    events=[];hb=cur['hb'];state=hb.get('status')
    if prev is None:
        return events,remember(cur,now)
    if cur['attempts']>prev.get('attempts',0):
        events.append(('RESTART','Trader child exited and the supervisor restarted it (recovery-only). Check why.'))
    if cur['failures']>prev.get('failures',0):
        events.append(('CHILD_FAILURE','Trader recorded a heartbeat-thread failure (see child-failure.log).'))
    new_blocked=[b for b in cur['blocked'] if b not in prev.get('blocked',[])]
    if new_blocked:
        events.append(('LAUNCH_BLOCKED',f'New launch-blocked file: {new_blocked[0]}'))
    if market_hours(now):
        if cur['age'] is None or cur['age']>STALE_SECONDS:
            if state not in ('STOPPED',) :
                events.append(('HEARTBEAT_STALE',f"No fresh trader heartbeat ({'none' if cur['age'] is None else int(cur['age'])} s). Process may be dead."))
        minute=now.hour*60+now.minute
        if minute>=OPEN_MINUTE+OPEN_GRACE and (cur['age'] is None or cur['age']>STALE_SECONDS) and not prev.get('opened'):
            events.append(('NOT_RUNNING_AT_OPEN','Market has been open 25+ min and the trader is not running.'))
    if state and state!=prev.get('state'):
        if state in ('INCIDENT','HUNG','HEARTBEAT_FAILED','RECOVERY_UNRESOLVED'):
            events.append(('STATE_'+state,f'Trader state is now {state}.'))
    if hb.get('unresolved') and not prev.get('unresolved'):
        events.append(('POSITION_OR_ORDER_OPEN','An order or position is now open/unresolved (likely an entry). Check the dashboard.'))
    if prev.get('unresolved') and not hb.get('unresolved'):
        events.append(('POSITION_OR_ORDER_CLOSED','Open order/position resolved (likely an exit). Check the dashboard.'))
    if hb.get('recovery_only') and not prev.get('recovery_only'):
        events.append(('RECOVERY_ONLY','Trader is in recovery-only mode: no new entries until promoted.'))
    if hb.get('last_step_result')=='CLOSED_RECONCILED' and prev.get('last_step_result')!='CLOSED_RECONCILED':
        events.append(('SESSION_CLOSED',f"Session closed and reconciled. Restarts today: {cur['attempts']}."))
    return events,remember(cur,now,prev)


def remember(cur,now,prev=None):
    hb=cur['hb']
    opened=bool((prev or {}).get('opened')) or (cur['age'] is not None and cur['age']<=STALE_SECONDS)
    return {'attempts':cur['attempts'],'failures':cur['failures'],'blocked':cur['blocked'],
        'state':hb.get('status'),'unresolved':bool(hb.get('unresolved')),
        'recovery_only':bool(hb.get('recovery_only')),'last_step_result':hb.get('last_step_result'),
        'opened':opened,'day':now.date().isoformat()}


def push(config,text):
    topic=config.get('ntfy_topic')
    if not topic:return
    try:
        request=urllib.request.Request('https://ntfy.sh/'+topic,data=text.encode('utf-8'),
            headers={'Title':'Auto Trader','Priority':'high'},method='POST')
        urllib.request.urlopen(request,timeout=10).read()
    except Exception:
        pass


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    config=read_json(OUT/'config.json') or {}
    state_path=OUT/'state.json';prev=read_json(state_path)
    last_sent={}
    while True:
        now=datetime.now(timezone.utc)
        try:
            cur=snapshot(now)
            if prev and prev.get('day')!=now.date().isoformat():prev=None   # new day: re-baseline
            events,prev=decide(prev,cur,now)
            for key,message in events:
                if now.timestamp()-last_sent.get(key,0)<600:continue   # at most one alert per kind per 10 min
                last_sent[key]=now.timestamp()
                with open(OUT/'events.jsonl','a',encoding='utf-8') as out:
                    out.write(json.dumps({'at':now.isoformat(),'event':key,'message':message})+'\n')
                push(config,message)
            tmp=state_path.with_suffix('.tmp');tmp.write_text(json.dumps(prev),encoding='utf-8');os.replace(tmp,state_path)
            (OUT/'alive.txt').write_text(now.isoformat(),encoding='utf-8')
        except Exception as exc:
            try:
                with open(OUT/'watcher-errors.log','a',encoding='utf-8') as out:
                    out.write(f"{now.isoformat()} {type(exc).__name__}\n")
            except Exception:pass
        time.sleep(POLL)


if __name__=='__main__':
    main()
