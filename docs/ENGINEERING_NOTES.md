# Autonomous trader - bounded sandbox certification

**M1/M2 remain offline; M3A remains read-only. The owner-revised essential
M3B sandbox round trip is complete.** One AAPL share was bought and sold,
both fills confirmed through REST and observed on gRPC; the account is flat.
The earlier canceled-order reservation was released by an owner-authorized
harness-only record, preserving history.

There is no production activation, strategy engine, autonomous trading,
OpenAI/Astra client or money-movement interface. M3B's private intent journal
cannot relabel the M2 OFFLINE ledger or grant M2 risk permits. See
`docs/M3B_REPORT.md` and `docs/m3b-certification-summary.json`.
Do not reset the private journal or rerun order tests to bypass its stop.

Source of truth:

- `autonomous-trader-architecture.md`, especially J2, J2a, J5 and J6.
- `trade-decision.schema.json`, unchanged. An identical packaged copy is checked by a test.
- The owner's approved restart and M2 risk interpretations in `OPEN_DECISIONS.md`.

## Run the offline suite

Tested with **CPython 3.13.3** on Windows. `.python-version` pins that runtime;
`pyproject.toml` limits this initial package to Python 3.13. The optional sandbox
dependency is the tested official `webull-openapi-python-sdk==3.0.0`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\scripts\local_postgres.ps1 start
.\.venv\Scripts\python -m pytest -q
```

Dependency installation requires a package index or a prepopulated wheelhouse.
M1 requires no service; M2 requires the local PostgreSQL fixture. Follow
[`docs/M2_RUNBOOK.md`](docs/M2_RUNBOOK.md) for first-time database setup and the
split test commands used for this evidence bundle. The recovery suite stops and
restarts ONLY this repository's disposable cluster and may need the Windows
identity that started it. Do not run it concurrently with other database tests.
No brokerage account or API credentials are used. Python socket connections are
blocked in tests; psycopg's native libpq connection is restricted by configuration
to loopback. `requirements.txt` pins the complete tested dependency set.

## Project layout

```text
src/autonomous_trader/
  contracts.py      immutable J2 contracts, envelopes, hashes
  numeric.py        bounded Decimal parsing and exact arithmetic
  decisions.py      local JSON Schema parser and contextual semantic checks
  reference.py      exchange sessions and separate settlement calendar fixtures
  execution.py      order reducer and script-driven simulated broker
  portfolio.py      in-memory cash/position/reservation reference model
  risk.py           fixed policy, risk formulas, independent latches and restart
  invariants.py     deterministic checks and offline permit-consumption model
  durable/
    models.py      typed internal ledger commands and correction records
    projection.py  deterministic journal reducer, FIFO accounting and risk
    ledger.py      row-locked transactions, permits, fencing and replay
    schema.sql     journal, checkpoints and relational projections
tests/               semantic, fault, boundary and generated event-sequence tests
  m2/                real PostgreSQL races, crash boundaries and recovery tests
scripts/             disposable local PostgreSQL lifecycle helper
docs/                architecture, data dictionary, acceptance coverage
policy/              fixed paper limits and offline capability fixture
OPEN_DECISIONS.md    explicit assumptions and unresolved safety decisions
```

Parse a raw JSON proposal with `parse_trade_decision`, then call
`validate_semantics` with a trusted `DecisionContext`, explicit registry and
injected current time. Successful validation returns a **proposal**. It creates
no intent, reservation, permit, or order. The simulator accepts only separately
constructed typed offline intents from test code.

All monetary and quantity calculations use `Decimal`. Session times normalize
to UTC; session labels use `America/New_York`. Calendars have explicit finite
coverage and separate exchange/settlement closures. No calendar is fetched.

## Preserved policy

Maximum account exposure 100%; stock exposure 20%; five simultaneous stock
positions or entry commitments; planned trade loss 1.5%; aggregate open risk 5%;
daily loss halt 5%; drawdown halt 20%; experiment termination 90%. No margin,
borrowing, transfers or external money movement. US-listed liquid stocks,
long-only, regular session only. Liquidation begins at actual close minus five
minutes; cancellation remains available after hours.

A 20% drawdown latches `DRAWDOWN_HALTED`. An authenticated owner restart creates
a new segment using current equity, preserves the original history and
watermark, and cannot clear daily/incident latches or experiment termination.
Authentication defaults to deny. A local HMAC verifier exists solely to test
authenticated actions; it loads no keys and is not a production owner service.

Exposure uses current reconciled liquidation equity. Planned and aggregate open
risk use `min(current equity, start-of-day equity)`. Existing positions reserve
the larger applicable entry-relative loss or current-mark giveback to the stop,
plus supplied exit costs/stress. Pending buys count; pending sells receive no
offset until filled. **Daily-headroom coupling is unapproved and absent.**

M2 commits journal events, cash/lots/reservations, independent latches, restart
history, one-use permits and outbox state atomically. Journal replay rebuilds the
current state. Fencing and recovery gates retain uncertain obligations across
process death, PostgreSQL restart and restore into a new database. Consuming a
permit records uncertainty; it does not send an order.

Evidence: **187 passed, 0 failed** (149 M1 + 38 M2), including real PostgreSQL
WAL recovery and backup/restore. See [`docs/M2_REPORT.md`](docs/M2_REPORT.md),
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) and [`OPEN_DECISIONS.md`](OPEN_DECISIONS.md).
No scanner, allocator, execution gateway or model service is included.

## M3A sandbox preflight

SDK 3.0.0 is pinned in `requirements.txt` and the optional `sandbox` dependency.
In your own PowerShell window, run the local credential setup:

```powershell
& "D:\DS Projects\AITrader\scripts\set_webull_sandbox_secrets.ps1"
```

This stores Windows DPAPI-encrypted SecureStrings in ignored
`.secrets/webull-sandbox.clixml`, readable only under the same Windows identity
on this computer. Paste App Key and App Secret into the two masked prompts and
press Enter after each. The script trims surrounding whitespace, rejects empty
values and attempts to clear the clipboard after secure capture. A clipboard
clear failure prints a warning and continues. Existing optional account
ID and access token fields are preserved without additional prompts.
It never prints the values. Alternatively the runner reads
`WEBULL_SANDBOX_APP_KEY`, `WEBULL_SANDBOX_APP_SECRET`,
`WEBULL_SANDBOX_ACCOUNT_ID` and optional `WEBULL_SANDBOX_ACCESS_TOKEN` from its
process environment. Partial environment credentials fail closed and do not
fall back to the file. No `.env` is created or loaded.

Run an explicitly authorized read-only probe with
`.\.venv\Scripts\python scripts/webull_sandbox_preflight.py`; `--auth-only`
limits it to authentication configuration and account listing. Offline pytest
never runs this script. Observations are sanitized into `tests/fixtures/webull_sandbox/`
and a report in `docs/`; no credentials or broker data enter PostgreSQL.
`scripts/check_m3a_artifacts.py` verifies current known-secret absence in source
and evidence without printing matches.

Authenticated read-only preflight now succeeds with SDK 3.0.0/Python 3.13.3.
The designated CASH/INDIVIDUAL_CASH account returns $1,000,000 settled cash,
zero unsettled cash and empty positions/orders. HTTP snapshots/batch history,
MQTT and gRPC subscriptions succeeded. Observed AAPL samples were near real time;
no explicit entitlement package was returned. Single-history GET remains 404.
No observations were imported into the ledger. See `docs/M3A_REPORT.md`,
`docs/m3a-certification-summary.json` and `docs/ACCEPTANCE.md` for test evidence
and remaining field/settlement/increment limitations.

No order was placed, modified or canceled.
**Stop at M3A. Order certification requires separate owner approval.**

## Real-account credential storage only

Production execution remains disabled. To store a separate real-account App Key
and App Secret for future owner-authorized work, open your own PowerShell window
and run:

```powershell
& "D:\DS Projects\AITrader\scripts\set_webull_real_secrets.ps1"
```

Paste each value into its masked prompt and press Enter. Never paste a real key
or secret into Chat or a Codex prompt. Do not use the sandbox credential script
for real-account values. The script trims surrounding whitespace, rejects empty
values, attempts to clear the clipboard after every capture, and never prints a
credential. If clipboard clearing fails, it warns and continues so you can clear
the clipboard manually.

The values are stored only as Windows DPAPI-encrypted SecureStrings in the
ignored `.secrets/webull-real.clixml` file for the current Windows user and
computer. This storage step does not connect to Webull, enable `PRODUCTION`,
alter scheduling, or grant the runner access to the real-account file.

## M3B status

305 automated tests passed, including PostgreSQL crash/restart and backup/restore.
The essential round trip bought one AAPL share at $332.76 and sold it at $332.75.
Two final-fill gRPC events matched REST. The $332.73 sale proceeds remained
unsettled and were not reused. Fee labels and general settlement finality remain
uncertified; no replacement, protection/OCO or excluded edge cases were tested.

The round-trip runner has consumed its one-use intents and cannot start another
trip. Do not reset its private journal. Original first-sequence evidence and
reports remain historical; earlier M3A text describes that milestone only.
See [M3B report](docs/M3B_REPORT.md), [acceptance](docs/ACCEPTANCE.md) and
[open decisions](OPEN_DECISIONS.md). M4 and production/autonomous execution
remain disabled.


### Minimum autonomous MVP

See [MVP report](docs/MVP_REPORT.md). Run `.venv\Scripts\python.exe scripts/run_trading_cycle.py --key-file <local-key-file>` with PostgreSQL running. `AI_TRADER_MODE=DRY_RUN` is the default; explicitly set `SANDBOX` for paper execution. Configure `AI_TRADER_MODEL` and `AI_TRADER_REASONING` at runtime. PRODUCTION is disabled. Closed-session `--analyze-closed` permits analysis only. Keep the process running while a sandbox position is held; local protection is not broker-native.


### Local dashboard and continuous sandbox runner

Start with `.\scripts\start_ai_trader.ps1 -Mode SANDBOX`; open http://127.0.0.1:8765 . No per-trade human approval is required. The installed `AITrader-Sandbox` task starts before Monday's session and restarts crashed trader processes. Keep Windows signed in (locking is fine). See [monitoring operating report](docs/MONITORING_MVP_REPORT.md) for scheduling, wake settings, tests, recovery and disabling autostart. PRODUCTION is disabled; the dashboard is read-only.
