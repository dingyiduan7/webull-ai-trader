# Auto Trader release log

Every change Claude ships to this repository is recorded here: what changed,
why, how it was verified, and how to undo it. Newest first.

---

## 2026-10-13 — R7: 40% per-stock cap, liquidity floor, runner/trailing playbook

**Live trading status:** still NOT enabled. The charter scope embeds the hard policy, so the
owner must re-sign (`scripts\owner_sign_authorization.py`) before this build can trade.

### What changed
- **Per-stock exposure cap 20% -> 40% of equity** (owner-approved 2026-10-13). Trade risk
  (1.5%), open risk (5%), daily-loss halt (5%), drawdown halt (20%), cash-only and long-only
  are unchanged. `risk.py`, `economic_policy.py` default, `policy/paper-policy.json`; the two
  places that had `.20` written in (`real_market.py`, `real_strategy_gateway.py`) now read
  `POLICY.stock_exposure`.
- **Liquidity floor for new entries** (`real_market.py`): mid price >= $2 and spread <= 100 bps.
  Held positions are never filtered.
- **Prompt `claude-decision-v2`**: runner-continuation setup, trailing-stop guidance for
  position reviews, abstain on names up > ~100% on the day, updated size/limit text.
- Tests: `tests/test_portfolio.py` quantities rescaled to the 40% cap; new
  `tests/test_real_liquidity_floor.py`.

### Why
Oct 12 replay of the top gainers: micro-caps (< $2, tiny market cap) are untradable at this
size (halts, gaps through stops); BMBL ran midday (not at the open); fixed 2-6% targets
capped a +56% runner. The 20% cap, not the loss limits, was holding winners to ~$100.

### Verification
Replica: 82/85 on the touched suites before the three cap-calibrated tests were rescaled;
full-suite run pending the owner's Windows run.

### Undo
Restore the files from `.local\archive\r7-20261013\` (same relative paths).

---

## 2026-10-12 — R6: self-applying release installer; final setup polish

**Live trading status:** still NOT enabled.
- Remaining: Tuesday's bootstrap, then the scheduled-task install.
- First bootstrap attempt (Sun night): $217.56 unsettled from a Friday sale.
  Oct 12 is not a settlement day, so it settles Tue Oct 13.
- The other AI's tasks are all Disabled and none of its processes are running
  (owner checked).

### What changed
- **`install_reviewed.py`** (repo root). Claude ships releases as verified
  bundles in `.local/releases/incoming/<id>/` (`release.json` + `files/`).
  - Every file's new hash and expected current hash is checked first.
  - The old files and the release record are backed up with a rollback
    manifest.
  - The new tree is validated in a fresh interpreter (compile all; import the
    live entry modules; compute the build digest).
  - Any failure restores the exact previous state.
  - Only code, tests and docs paths are allowed; secrets and runtime data can
    never be replaced.
  - Applied and rejected bundles move to `applied/` or `rejected/` with a
    RESULT.json; a rejected bundle stops the queue.
- **Safe point:** `run_real_daily.py` applies pending bundles only at the first
  start of a trading day, before any market read, model call or order. A
  same-day restart never swaps code. Results go to
  `.local/releases/last-apply.json`. Installer failure never blocks trading.
- `install_real_daily_task.ps1`: the pre-check message now names the owner
  charter and bootstrap. No admin needed (current user, limited run level).
- Tests: the 3 `install_rollback` spec tests now pass, plus 7 new installer
  tests. Two stale advisory tests fixed: the supervisor command line now
  includes `-I -B`, and the external `agent_layer` test skips when that repo is
  absent.

### Verification
- Replica: 1454 passed / 31 failed. No new failures; the remainder are
  Linux-only or need machine-local files.

### Rollback
Previous versions are in `.local/archive/r6-20261012/`. New files can be deleted.

---

## 2026-10-12 — R5: fresh account bootstrap (flat checkpoint V3, measured entitlement)

**Live trading status:** still NOT enabled.
- Done: charter signed; Webull and Claude keys stored.
- Pending: bootstrap during market hours, then the scheduled task.

### Why
The inherited readiness evidence was hard-wired to the other AI's September
account files (flat checkpoint V1/V2 with fixed hashes) and to a developer-portal
screenshot. This copy must not depend on either.

### What changed
- **`scripts/real_bootstrap.py`** (read-only, never orders):
  - confirms the cash account is flat (no positions or open orders), fully
    settled and cash-consistent;
  - reconciles the observation journal and writes `flat-checkpoint-v3-<time>.json`;
  - reports the local ledger status;
  - during market hours, measures AAPL/QQQ quote age (must be ≤10 s) as
    real-time entitlement evidence;
  - writes `deployment-evidence.json`.
- **`REAL_FLAT_CHECKPOINT_V3`** validation (`real_flat_checkpoint.py`): exact
  cash, zero unsettled, flat, bound to the account and the journal version.
- **Measured entitlement** (`real_trading.py`,
  `WEBULL_OPENAPI_REALTIME_OBSERVED`) alongside the legacy screenshot path.
- **First ledger opening:** when this copy has no REAL ledger, the daily
  preparation opens it from the V3 checkpoint (`open_ledger_from_checkpoint`).
  Before, it could never start: no certificate without a ledger, no ledger
  without a certificate.
- `scripts/webull_real_token_setup.py` (R4.3) reads the encrypted keys instead
  of a plaintext file.

### Verification
- Replica: 1444 passed; the same 35 known failures (Linux-only and known
  items). New `tests/test_real_bootstrap.py` (14) runs against real PostgreSQL
  with a fake read-only broker.

### Rollback
Previous versions are in `.local/archive/r5-20261012/`. New files can be deleted.

---

## 2026-10-12 — R4.2: Windows suite 1469 passed / 11 failed; test isolation fixes

Windows run after R4.1: 1469 passed, 11 failed (was 1033 passed / 10 failed /
413 errors before R2). Fixes (tests only, no runtime change):
- `test_key_loading_from_env_and_file` reached the owner's real encrypted key
  on the PC. It now uses an explicit empty root.
- Three supervisor tests (launcher restart policy, preventive controls,
  supervisor incident) replaced `subprocess.Popen` with fakes. The supervisor
  launches through the Windows job-object wrapper, so the tests now replace
  `spawn` and `confirm_stop` instead.

Expected remaining Windows failures (7):
- `install_rollback` (3): the spec for the release installer.
- `after_hours_packet` (2): need the real flat checkpoint; produced during live
  setup.
- `sme_advisory` (2): the old advisory path, scheduled for retirement.

---

## 2026-10-12 — R4.1: Claude connection verified; 1-hour cache pricing

- First live check passed (`.local/claude-check/latest.json`): models listed,
  native and prompt-schema decisions valid, 3 calls ~$0.011.
  Account notes: the key must be workspace-scoped (workspace "Auto Trader"),
  and API credits are billed separately from the Claude subscription.
- Cost estimates now price Claude cache writes at the 1-hour TTL rate (2x
  input), matching the cached system prompt. Estimates were slightly low before.
- Backup: `.local/archive/r4_1-20261012/`.

---

## 2026-10-12 — R4: decision intelligence (features, Claude prompts, learning memory)

**Live trading status:** still NOT enabled.

### What changed
- **Session features computed in code** (`real_features.py`). Each shortlisted
  stock now carries compact, exact facts instead of only its last five 1-minute
  bars:
  - Session: VWAP and distance from it, 15-minute opening range, change from
    open, 5/15/30-minute momentum, day-range position, minutes since the day
    high, 1-minute range and volatility, volume pace, bar coverage (thin
    trading).
  - Daily: prior close/high/low, gap, ATR14, 20-day volume and dollar volume,
    5/20-day returns.

  1-minute bars are fetched incrementally (a full session once, then only new
  minutes); request counts are unchanged. Daily bars are fetched once per
  session.
- **Market backdrop:** SPY features as `context.market_regime`. Never blocks
  trading; marked unavailable on any read failure.
- **Claude-native prompts** (`decision_api/claude_prompts.py`,
  `claude-decision-v1`): a cached system prompt (1-hour TTL) covering
  - objective: long-run net profit, no daily target, ABSTAIN is normal;
  - cash-account mechanics: T+1, one round trip per settled dollar, ~$100
    positions, spread math;
  - evidence rules and a 5-setup intraday playbook;
  - a decision method: thesis, invalidation, reward-to-risk after spread, stop
    beyond 1-minute noise, sizing;
  - output rules.

  Replaces the inherited "Astra" prompts and their "10% per trading day"
  benchmark for Claude models. Decision evidence records the prompt version and
  hash actually sent.
- **Learning memory:** live Claude calls carry the runtime scope, a
  per-strategy scoreboard (trades, wins, net, return on capital, confidence:
  anecdotal / emerging / established), account-period economics and recent
  trades. The long legacy owner/manual text blocks are no longer sent each
  call; their substance now lives in the cached system prompt.

- **Connection fix:** the first live key check failed with HTTP 400 on the
  model list. The client now lists models without a `limit` parameter
  (paginated) and sends no JSON content-type on GET requests. API errors keep
  Anthropic's error type and a short scrubbed message for diagnosis.

### Verification (Linux replica)
- 1430 passed / 35 failed. Same 35 known failures as R3; nothing new.
- New tests: `test_real_features.py` (7), `test_real_learning_memory.py` (2),
  and 2 more Claude prompt tests.

### Rollback
Previous versions are in `.local/archive/r4-20261012/`. New files can be deleted.

---

## 2026-10-12 — R3: Claude is the decision model; spend circuit breaker; call triggers

**Live trading status:** still NOT enabled. Additionally needs the Claude key
(`scripts/set_anthropic_key.ps1`) and a passing `scripts/claude_connection_check.py`.

### What changed
- **Claude replaces Astra (OpenAI).** New `decision_api/claude_client.py`
  (Anthropic Messages API) keeps the existing request builders, budget claim,
  fixed error codes, local schema + semantic validation and cost evidence;
  only the wire transport differs. `decision_api/brain.py` is the single
  construction point: decisions `claude-sonnet-5-5`, shadow audits
  `claude-haiku-5-5`. Adaptive thinking with the call's effort level; system
  prompt cached. Schemas that fit Claude's structured-output subset are
  enforced natively; larger ones (the entry decision) are embedded in the
  prompt. Every response is still validated locally against the full schema.
- **Key storage:** `.secrets/anthropic.clixml`, DPAPI-encrypted for the
  owner's Windows account (same pattern as the Webull secrets).
- **Pricing:** `decision_api/pricing.py`, cache-aware per-model prices
  (verified 2026-10-12). Unknown models are refused before any call.
- **Spend circuit breaker (live mode):**
  - The rolling limit of 6 calls per 5 minutes (4 for non-position calls) now
    applies in live mode as well. It used to be sandbox-only.
  - New daily model-spend ceiling of $3 per New York session. Entry research
    stops at $3; position management may continue up to $6, so open positions
    are always managed.
  - Rate-limit and overload replies (HTTP 429/529) trigger a 60-second cooldown.
- **Entry-call triggers** (`real_triggers.py`). A new entry call happens only on:
  - the first look of the session,
  - a new candidate entering the shortlist,
  - a ≥1% move in a shortlisted price since the last call,
  - changed cost evidence,
  - a 30-minute heartbeat.

  Tick flicker can no longer buy calls. The previous key hashed 1-minute bars
  and could trigger a call almost every minute.
- **Bounded shortlist:** at most 8 instruments per call (held first). Live
  discovery interleaves the volume, relative-volume and mover surfaces by
  broker rank. Bars are fetched only for instruments that can reach the
  shortlist. The live path previously sent every affordable discovered symbol.

### Verification (Linux replica)
- R2 1376 passed / 48 failed → R3 **1418 passed / 35 failed**. No new failure;
  the 3 known gaps (spend breaker, trigger hysteresis, shortlist bound) now pass.
- New tests: `tests/mvp/test_claude_client.py` (18),
  `tests/test_real_triggers.py` (4), 6 budget tests.
- Remaining failures: Windows-only, machine-local files, the `install_rollback`
  spec, and 2 old advisory tests.

### Rollback
Previous versions are in `.local/archive/r3-20261012/`. New files can be deleted.

---

## 2026-10-12 — R2: two runtime fixes, test suite aligned to the current code

**Live trading status:** still NOT enabled (no charter, credentials or evidence yet).

### Runtime fixes (trading-critical: `real_runner.py`)
1. **Authority is re-verified after every recovery read.** The account-only
   recovery path again re-checks the complete authority receipt after reading
   the account, not only the file-change detector. A revocation, release or
   certificate change during that read can no longer let the read clear an
   incident. (The newer inherited build had dropped this check.)
2. **No duplicate market reads on a prepared handoff.** A market-data packet
   prepared moments earlier in the same cycle is now reused instead of
   discarded and re-fetched. It still passes the full authority audit and
   current-time freshness validation, so a stale packet is still rejected.
   This completes the fix for the inherited "repeated market-data
   preparation amplifies rate limits" issue (the reviewed patch fixed only the
   other caller).

### Test suite
- pytest now uses a repo-local temp folder (`.pytest-tmp/`, git-ignored). The
  shared `%TEMP%\pytest-of-<user>` folder is locked on this PC and caused 413
  setup errors on the last Windows run.
- Updated test doubles that predated the current runtime contract (gates
  without `read_checked`, receipts without validity windows, certificate
  binding): account-incident recovery, flat recovery, runner boundary,
  strategy gateway, launcher composition, packet handoff, daily autonomous
  startup, offline round trip. Protective assertions kept; where a contract
  became stricter (two clean observations before clearing an incident) the
  test now asserts the stricter rule.
- Added: startup rejects a certificate that differs from what readiness
  collection produced.
- Entry quote-validity tests aligned to the runtime's deliberate 10-second
  window (was 2s), still asserting that older quotes are rejected.
- Retired (owner approved): 4 tests of the removed `judicator-real-go.json`
  fallback; the offline round-trip tail that exercised the old multi-agent
  advisory layer through a staging-only path.

### Verification (Linux replica, pinned packages, PostgreSQL 16)
1,376 passed / 48 failed. All 48 classified:
- Windows-only APIs (DPAPI, `CREATE_NO_WINDOW`, process/handle APIs): pass on Windows.
- Need machine-local artifacts absent from this copy (real flat checkpoint,
  scheduler XML): revisit when fresh evidence is generated.
- Old advisory agent layer (2): removed with the Claude decision module.
- Install-rollback (3): kept as the specification for the release installer.
- **Known gaps, owned by the next release (Claude decision module):**
  - Live mode has no limit on AI decision calls (2 budget tests): will be
    replaced by a spend-rate circuit breaker.
  - Model-call dedup re-triggers on 1-cent spread flicker near a bucket
    boundary (1 test): call-trigger policy with hysteresis.
  - Portfolio context promises <=8 candidates but sends all (1 test): candidate
    count per call set in the decision module.

### Rollback
Previous versions in `.local/archive/r2-20261012/`.

---

## 2026-10-12 — R1: canonical source tree + owner charter authority

**Live trading status:** NOT enabled. No charter signed, no credentials
configured for this copy, no deployment evidence, no scheduled task installed.
Nothing in this release can place an order.

### Why
`D:\auto trader` held three diverging versions of the code with no git
history: an older working copy (`src/`, `scripts/`), a newer reviewed build
(`HANDOVER_SNAPSHOT/installed-runtime/`), and ~30 unmerged candidate patches
in `.local/review/`. The tests tracked the newer build, so the older working
copy failed them.

### What changed
- **Code base:** `src/` and `scripts/` now match the newer reviewed build,
  plus the latest reviewed fix (`.local/review/read-quota-20261008`:
  `scripts/run_real_trader.py`, market-data request de-duplication).
- **Isolation preserved:** this copy keeps its own database port (55433),
  dashboard port (8766) and task names (`AutoTraderFork-*`), so it cannot
  collide with the original AI Trader install.
- **Not copied:** the reviewed build's `runtime-storage.json`, which pointed
  this copy's data folder at `D:\DS Projects\AITrader` (the other AI's repo).
  Data stays inside `D:\auto trader`.
- **Authorization replaced:** Windows Hello / protected-install authority
  removed; replaced by the owner-signed charter (see `docs/OWNER_AUTHORIZATION.md`).
  New: `owner_authorization.py`, `real_owner_trust.py`,
  `real_owner_authorization.py`, `scripts/owner_*.py`. Removed:
  `owner_authorize_real.py`, `check_owner_bootstrap.py`, `fido2` dependency.
- **Scheduler:** kept this copy's fix that dates the daily task triggers from
  the install date (avoids a winter time-boundary offset).
- **Tests:** owner-authorization tests rewritten for the charter design;
  `test_real_daily.py` updated: a code release is no longer an owner-scope
  change (it now verifies the build is re-bound without a new signature).

### Verification (Linux replica: same pinned packages, real PostgreSQL 16)
- Before: 1168 passed, 129 failed. After: **1262 passed, 40 failed**.
- 98 previously failing tests now pass.
- 9 tests newly fail; all 9 were written for the older runtime's interfaces
  (fake gates without `read_checked`, or the removed `judicator-real-go.json`
  fallback) and fail against the reviewed build regardless of this change.
  Pending owner decision on updating them (see below).
- 39 fail in both: Windows-only modules on Linux, Windows path assumptions,
  machine-local files, date-sensitive fixtures, and a few pre-existing
  test/code mismatches. To be re-checked on Windows.
- Deployed files verified byte-identical to the tested files.

### Open decision for the owner
Nine tests encode older runtime behavior. Options: update their test doubles
and expected values to the current runtime, or retire the four that test the
removed `judicator-real-go.json` path. Claude has not changed or removed them.

### Rollback
Previous versions of every overwritten file are in
`.local/archive/pre-canonical-20261012/` (same relative paths). Copy them back
to restore the prior state. New files from this release can simply be deleted.
