"""Exactly one essential sandbox buy/sell; stops on uncertainty; never retries writes."""
from pathlib import Path
import sys
import os
import json
import argparse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
for name in ('SSLKEYLOGFILE', 'GRPC_TRACE', 'GRPC_VERBOSITY', 'WEBULL_LOG_LEVEL', 'DEBUG'):
    os.environ.pop(name, None)

from webull.trade.request.v3.get_order_history_request_v2 import OrderHistoryRequestV2
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import Operation, build_request, SandboxHttp
from autonomous_trader.sandbox.evidence import fixture, save_json
from autonomous_trader.sandbox.quiet import quiet_sdk
from autonomous_trader.sandbox.certification.controls import MODE, verify_flat, exact_decimal, regular_session
from autonomous_trader.sandbox.certification.round_trip import (
    AUTHORIZATION, RoundTripJournal, RoundTripHttp, account_state, checked_detail, marketable_order)
from autonomous_trader.sandbox.certification.events import OrderEvents
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer
from autonomous_trader.durable.ledger import LocalDatabase

WRITER_LOCK=17482026

def acquire_writer_lock():
    connection=LocalDatabase('postgres','m2_admin',schema='mvp_sandbox').connect();connection.autocommit=True
    if not connection.execute('SELECT pg_try_advisory_lock(%s)',(WRITER_LOCK,)).fetchone()[0]:
        connection.close();raise PreflightError('RUNNER_ALREADY_ACTIVE')
    return connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', required=True, choices=[MODE])
    options = parser.parse_args()
    regular_session(datetime.now(timezone.utc))
    writer_lock=acquire_writer_lock()
    credentials = load_credentials(ROOT)
    sanitizer = OrderSanitizer(credentials.sensitive_values())
    journal = RoundTripJournal(ROOT/'.local/m3b/certification.sqlite3', credentials.account_id)
    if journal.db.execute("SELECT 1 FROM intents WHERE kind LIKE 'ROUND_TRIP_%'").fetchone():
        journal.close()
        raise PreflightError('ROUND_TRIP_ALREADY_STARTED_READ_ONLY_RECONCILIATION_REQUIRED')
    reads = SandboxHttp(credentials)
    http = RoundTripHttp(credentials, mode=options.mode, journal=journal)
    stream = OrderEvents(credentials)
    run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = ROOT/'tests/fixtures/webull_m3b'/run
    report = {'run_id': run, 'scope': 'M3B_ESSENTIAL_ROUND_TRIP', 'authorization': AUTHORIZATION,
        'results': {}, 'events': [], 'order_submissions': 0, 'prior_reservation_released': False,
        'round_trip_obligation_closed': False, 'grpc_blocking': False, 'result': 'STOPPED'}

    def record(name, observation):
        data = fixture(observation, sanitizer)
        path = folder/(name+'.json')
        save_json(path, data, sanitizer)
        journal.record('ESSENTIAL_HTTP_OBSERVATION', data)
        report['results'][name] = {'status': observation.status_code, 'fixture': path.relative_to(ROOT).as_posix()}
        if observation.status_code != 200:
            raise PreflightError('HTTP_FAILURE_STOP_NO_WRITE_RETRY')
        return observation

    def bundle(prefix):
        return {name: record(prefix+'_'+name, reads.get_response(build_request(op, credentials.account_id)))
            for name, op in (('accounts', Operation.ACCOUNTS), ('balance', Operation.BALANCE),
                ('positions', Operation.POSITIONS), ('orders', Operation.OPEN_ORDERS),
                ('instruments', Operation.INSTRUMENTS), ('snapshot', Operation.SNAPSHOT))}

    def capabilities(observations):
        profiles = observations['instruments'].body.get('data')
        if (not isinstance(profiles, list) or len(profiles) != 1 or profiles[0].get('symbol') != 'AAPL'
                or profiles[0].get('category') != 'US_STOCK' or profiles[0].get('sub_category') != 'COMMON_STOCK'
                or profiles[0].get('status') != 'OC' or exact_decimal(profiles[0].get('lot_size')) != 1):
            raise PreflightError('INSTRUMENT_CAPABILITY_CHANGED')

    def collect():
        for event in stream.drain():
            safe = dict(event, payload=sanitizer.clean(event['payload']))
            report['events'].append(safe)
            journal.record('ESSENTIAL_GRPC_OBSERVATION', safe)

    def fill(prefix, order, broker_id):
        for number in range(6):
            detail = record(prefix+'_detail_'+str(number), http.detail(order['client_order_id']))
            row = checked_detail(detail.body, order, broker_id)
            collect()
            if row['status'] == 'FILLED':
                report[prefix+'_fill'] = sanitizer.clean(row)
                return detail
            if row['status'] not in ('PENDING', 'SUBMITTED') or exact_decimal(row['filled_quantity']) != 0:
                raise PreflightError('UNEXPECTED_STATUS_OR_PARTIAL_FILL_STOP')
        raise PreflightError('FILL_NOT_CONFIRMED_WITHIN_BOUNDED_WINDOW')

    def submit(prefix, order, observations, buy_proof=None):
        intent = journal.prepare_leg(order)
        journal.record('ESSENTIAL_RESERVED', {'side': order['side'], 'quantity': '1',
            'cash_and_full_principal_risk': str(exact_decimal(order['limit_price'])+25) if prefix=='buy' else '0',
            'pending_sell_risk_offset': '0'}, intent)
        report[prefix+'_order'] = sanitizer.clean(order)
        response = record(prefix+'_submit', http.submit_leg(intent, order, observations, observations['snapshot'], buy_proof))
        body = response.body
        if not isinstance(body, dict) or body.get('client_order_id') != order['client_order_id'] or not body.get('order_id'):
            raise PreflightError('SUBMIT_IDENTIFIERS_UNKNOWN')
        return body['order_id']

    try:
        with quiet_sdk():
            report['stream_subscribed'] = stream.start()  # observational only
            old = journal.db.execute("SELECT payload FROM intents WHERE kind='PLACE' AND claimed=1").fetchall()
            if len(old) != 1:
                raise PreflightError('PRIOR_INTENT_NOT_UNIQUE')
            prior = json.loads(old[0][0])
            old_detail = record('prior_detail', http.detail(prior['client_order_id']))
            broker_id = old_detail.body.get('combo_order_id')
            prior_row = checked_detail(old_detail.body, prior, broker_id)
            original = json.loads((ROOT/'tests/fixtures/webull_m3b/20260911T190032474030Z/submit.json').read_text())['body']
            old_events = [json.loads(r[0]) for r in journal.db.execute("SELECT payload FROM events WHERE kind='HTTP_OBSERVATION'")]
            original_submit = next((e['body'] for e in old_events if e.get('path')=='/trading/orders/place'), None)
            # Journal observations are aliased; deterministic run-local aliases are compared
            # with the immutable original fixture. Raw client ID stays private in intent.
            if original_submit != original or prior_row['status'] != 'CANCELLED' or exact_decimal(prior_row['filled_quantity']) != 0:
                raise PreflightError('PRIOR_CANCELLATION_NOT_CONFIRMED')
            observations = bundle('before')
            verify_flat(MODE, credentials.account_id, observations, datetime.now(timezone.utc))
            capabilities(observations)
            journal.release_prior({'authorization': AUTHORIZATION, 'cancelled_zero_fill': True,
                'matching_identifiers': True, 'flat_unchanged_cash': True, 'released_cash': '324.34'})
            report['prior_reservation_released'] = True
            order = marketable_order(observations['snapshot'].body, 'BUY', datetime.now(timezone.utc))
            buy_broker = submit('buy', order, observations)
            fill('buy', order, buy_broker)
            # Refresh direct filled detail immediately before the fresh account bundle.
            buy_detail = record('buy_confirmed_detail', http.detail(order['client_order_id']))
            if checked_detail(buy_detail.body, order, buy_broker)['status'] != 'FILLED':
                raise PreflightError('BUY_FILL_CHANGED')
            holding = bundle('holding')
            account_state(holding, credentials.account_id, datetime.now(timezone.utc), owned=True)
            capabilities(holding)
            journal.record('ROUND_TRIP_BUY_FILLED_RECONCILED', {'quantity': '1', 'symbol': 'AAPL',
                'detail_and_position_agree': True, 'sell_proceeds_not_available': True})
            sell = marketable_order(holding['snapshot'].body, 'SELL', datetime.now(timezone.utc))
            sell_broker = submit('sell', sell, holding, (buy_detail, order, buy_broker))
            fill('sell', sell, sell_broker)
            after = bundle('after')
            account_state(after, credentials.account_id, datetime.now(timezone.utc), owned=False)
            final_buy = record('final_buy_detail', http.detail(order['client_order_id']))
            final_sell = record('final_sell_detail', http.detail(sell['client_order_id']))
            if (checked_detail(final_buy.body, order, buy_broker)['status'] != 'FILLED'
                    or checked_detail(final_sell.body, sell, sell_broker)['status'] != 'FILLED'):
                raise PreflightError('FINAL_DETAIL_CONFLICT')
            history = OrderHistoryRequestV2()
            history.set_account_id(credentials.account_id)
            record('after_history', http._send_bounded(history, '/trading/orders/historical-orders/list'))
            journal.record('ESSENTIAL_ROUND_TRIP_COMPLETE', {'bought': '1', 'sold': '1', 'position': '0',
                'harness_share_obligation_closed': True, 'sale_proceeds_reused': False,
                'fee_and_settlement_finality_not_assumed': True})
            report['round_trip_obligation_closed'] = True
            report['result'] = 'ESSENTIAL_ROUND_TRIP_COMPLETE_FLAT'
    except PreflightError as exc:
        report['stop_reason'] = str(exc)
    except Exception:
        report['stop_reason'] = 'UNEXPECTED_ERROR_DETAILS_SUPPRESSED_STOP'
    finally:
        with quiet_sdk(): stream.close()
        collect()
        report['stream_result'] = stream.result
        report['http_contact_attempts'] = reads.contacted + http.contacted
        report['order_submissions'] = sum(x['path']=='/trading/orders/place' for x in http.contacted)
        save_json(folder/'manifest.json', report, sanitizer)
        save_json(ROOT/'docs/m3b-round-trip.json', report, sanitizer)
        journal.record('ESSENTIAL_SEQUENCE_STOPPED', {'result': report['result'], 'stop_reason': report.get('stop_reason')})
        journal.close()
    print(json.dumps({k: report.get(k) for k in ('result','stop_reason','order_submissions','prior_reservation_released','round_trip_obligation_closed')}))
    writer_lock.close()


if __name__ == '__main__':
    try: main()
    except PreflightError as exc:
        print(json.dumps({'result':'STOPPED_BEFORE_SEQUENCE','reason':str(exc)})); sys.exit(2)
    except Exception:
        print('{"result":"STOPPED_DETAILS_SUPPRESSED"}'); sys.exit(1)
