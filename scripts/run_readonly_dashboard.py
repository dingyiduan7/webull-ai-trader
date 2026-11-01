"""Always-on loopback dashboard (read-only; never a broker client or trader).

Default port 8777 (the trader's embedded dashboard keeps 8766, so the two can never clash).
Restarts itself after any failure and logs every failure to .local/dashboard/dashboard-errors.log."""
from pathlib import Path
import argparse, sys, time, traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autonomous_trader.monitoring.server import DashboardServer
from autonomous_trader.monitoring.views import DashboardReader


def log_failure(exc):
    try:
        folder = ROOT / '.local' / 'dashboard'; folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'dashboard-errors.log').open('a', encoding='utf-8') as stream:
            stream.write(time.strftime('%Y-%m-%dT%H:%M:%S ') + 'launcher '
                + ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + '\n')
    except Exception:
        pass


def main(argv=None, sleep=time.sleep, attempts=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8777)
    port = parser.parse_args(argv).port
    tries = 0
    while attempts is None or tries < attempts:
        tries += 1
        try:
            DashboardServer(DashboardReader(ROOT), port=port).serve_forever()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            log_failure(exc)
            sleep(min(30, 2 * tries))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
