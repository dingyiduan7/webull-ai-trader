# Decisions and open questions

## Approved owner decisions

### 2026-09-30 REAL authority lifecycle

Owner activation persists across market days. Normal daily open/close uses autonomous
fresh account reconciliation and current market-data certification; it does not require
a new human GO each day. Reauthorization remains mandatory after a build change,
account-binding change, hard-risk-limit change, expired or revoked owner authority,
integrity failure, or unresolved exposure. This records the approved boundary only;
the broader authority-lifecycle implementation is deferred from the narrow launcher fix.

### 2026-09-21 SANDBOX economic-policy authorization

For the active paper account, the subsequent owner authorization supersedes the
fixed economic percentages/count and owner-only economic-halt restart rule below.
Astra may propose a complete versioned economic policy and explicitly release
daily, drawdown or experiment economic halts after fresh reconciliation. A release
does not reset realized losses, experiment history, high water or segment history;
the revised threshold must no longer be breached. Incident, settlement, freshness,
session, fencing and uncertain-order blocks cannot be released by this mechanism.
Original OFFLINE accounts and pre-authorization journal events retain their original
policy. Production remains disabled; settled cash, exact ownership, no borrowing,
and aggregate exposure no greater than actual equity remain immutable.

The current ten-symbol broker refresh gives a technical capacity of nine held
positions plus one focal instrument. This is not the former five-position economic
limit. Astra can set the count within that capacity. Model call-rate economics are
adjustable within implementation ceilings of eight concurrent requests and 120
calls/300 seconds; the initial call-rate preference is six calls/300 seconds.
The later owner cost correction sets an independent, hard $100 model-cost ceiling
per America/Denver calendar month, shared by SANDBOX and DRY_RUN. Astra may prefer
less monthly spend but cannot increase that ceiling. Every model category,
including policy review, uses the same pre-dispatch reservation. Unknown-cost
calls retain their reservations across restart. Existing journaled policy history
is preserved; prior session-dollar limits and the extra policy-review reserve
no longer grant spend authority.

The dashboard exposes the effective policy, origin, revision/hash, economic-halt
release history and model budget. A closed-market restart installs only the approved
defaults; a fresh Astra policy decision requires an open session and operationally
clean reconciliation. It is not a claim of live acceptance or profitability.

The implementation request approves latching the 20% drawdown halt and allowing
restart only through an authenticated owner action. Restart creates a new
operating segment using current equity as its reference. Earlier segment
records, observations, initial equity and permanent account high-water mark
remain intact. The cumulative 90% experiment-death rule never resets and remains
latched even if equity recovers. The source architecture's C3 restart question
is therefore resolved by this request, not by autonomous policy selection.

For M2, the owner explicitly approved both risk interpretations below:

- Exposure limits use current reconciled liquidation equity. Planned-trade and
  aggregate open-risk limits use `min(current equity, start-of-day equity)`.
  Daily loss uses fixed start-of-day equity.
- Existing holdings use the more conservative applicable entry-relative remaining
  loss or current-equity giveback to the active stop, including exit costs/stress.
  Pending buys consume risk; pending sells receive no offset until filled. No
  covariance/diversification credit weakens the aggregate open-risk cap.

**Daily-headroom coupling remains unapproved and is NOT implemented.** An M2
regression test admits a valid $13,300 planned risk after a 4% daily loss when
the existing approved cash/exposure/episode/open-risk limits permit it. No extra
limit is inferred from the remaining $10,000 daily headroom.

## Explicit M1 implementation choices

These choices make offline tests concrete. They do not certify a broker or
approve proposed operational restrictions for a later trading environment.

1. **Risk denominators â€” OWNER-APPROVED for M2:** the reference model uses current liquidation equity
   for exposure and `min(current equity, start-of-day equity)` for planned/open
   risk, as proposed in C1. Daily loss uses the fixed start-of-day denominator
   and triggers on either realized net loss or marked equity loss. The M2
   request explicitly approves these denominators.
2. **Open risk â€” OWNER-APPROVED for M2:** existing holdings use the larger of entry-relative stop loss
   and current-mark giveback, plus supplied exit costs/stress allowance. Pending
   buys add risk without offsets for pending sells. Consumed episode losses and
   costs cannot be offset by gains. No covariance credit is used.
3. **Segment watermark:** each restarted segment initializes both its reference
   and its local high-water mark to current equity; its high-water mark then
   ratchets upward. The 20% halt uses that segment watermark. This interprets a
   new operating segment as retaining the architecture's peak drawdown behavior.
   Confirm if the intended segment trigger was instead a fixed restart balance.
4. **Episode identity:** one instrument retains one episode for the entire
   offline reference-account fixture, even after flattening. This deliberately
   prevents relabeling a losing episode. A real definition of when an unrelated
   new trade episode begins is unresolved; this conservative test restriction
   must not silently become a permanent strategy restriction.
5. **Decimal representation:** external values permit at most 18 integer and
   eight fractional digits; exponent notation, nonfinite values and floats are
   rejected. Arithmetic helpers use an 80-digit local context. Quantities round
   down to explicit capability increments. The in-memory average-cost projection
   rounds UP to eight decimals, preserving a conservative risk basis; exact fill
   amounts remain in events. It is not final lot-level accounting or tax basis.
6. **Entry semantics:** uncapped MARKET entries are rejected. LIMIT and STOP_LIMIT
   require bounded prices; stop-limit buy trigger cannot exceed its limit. A new
   stop must be below the limit and current bid. Minimum acceptable quantity and
   target fractions must be positive. Amendments in M1 may tighten stops and
   shorten deadlines; widening stops/deadlines is unsupported pending M2 risk
   revalidation. No implicit OCO exclusivity or fractional protection is assumed.
7. **Trailing grammar:** percent distance means percentage points (`1` means 1%);
   AMOUNT/PERCENT require positive distances and explicit activation. FEATURE_RULE
   requires a registered rule and null distance. These semantics are absent from
   the supplied schema and need confirmation before a plan interpreter is built.
8. **Forecast nullability:** the supplied schema permits null forecasts and
   unavailable estimates. M1 preserves this and does not invent an alpha hurdle
   or authorize execution from absent forecasts. The later entry-selection
   service must define which economic evidence is required.
9. **Provenance:** the exact TradeDecisionV1 body cannot contain all generic J2
   metadata without violating `additionalProperties: false`. A generic immutable
   service-owned `Envelope[TradeDecisionV1]` carries correlation/causation IDs,
   source, revision, usable time and verified payload hash. Context hashes are
   computed from sorted, compact UTF-8 serialization of the typed context, with
   Decimal values serialized as strings. This is a local versioned hash format,
   not a claim of an external canonical-JSON standard.
10. **Fixture semantics:** BrokerEvent quantities and fees are cumulative in the
    M1 order reducer. Each fill event's price applies to its incremental quantity.
    Out-of-order events, busts, inconsistent cumulative values and corrections
    enter UNKNOWN and stop new risk while retaining observations/reservations.
    Final release requires explicit matching terminal detail and fill/fee totals.
    This does not claim final broker events prevent subsequent corrections.
11. **Calendar/clock inputs:** calendars are bounded synthetic fixtures with
    independently supplied exchange and settlement holidays. Unknown dates fail
    closed; passage of T+1 alone does not settle money. Tests use explicit 2-second
    market and 10-second account age settings. No J4 default is silently adopted
    as an operational policy. Market timestamps cannot be in the future. Full
    monotonic-clock/skew monitoring is deferred.
12. **Runtime/authentication:** M1 uses the locally available Python 3.13.3.
    Authentication defaults to deny; HMAC test proofs bind the owner, experiment,
    previous segment, expected equity, action ID and validity window. The key is
    supplied only by a trusted test caller, never loaded from API credentials.
    This is an offline boundary test, not the final owner authentication design.

## Unresolved before operational implementation/certification

- Daily-headroom coupling remains unapproved; M2 does not implement it. The
  denominator and open-risk interpretations above no longer require approval.
- Define economic episode boundaries, broker/tax-lot and correction mappings,
  actual fee schedules and worst-case fee/slippage reserves. M2's internal FIFO
  and revision semantics are specified below. The reference model accepts
  explicit test amounts; zero fees in a fixture are not a production fee claim.
- Select verified exchange and settlement calendars, revision handling, security
  master, liquidity classification and broker quantity/price increments.
- Certify actual individual-account settled-fund fields; partial/fractional
  protection, OCO, replace/cancel finality, cumulative/delta fee semantics,
  trade corrections, event ordering, pagination, rate limits and replay behavior.
- Define production owner authentication/signing, key custody, external replay
  retention and broker evidence required for restart. M2 implements durable audit,
  local lease/recovery and exact internal reconciliation gates; distributed
  gateway fencing and production recovery certification remain later work.
  A restart clears only the drawdown latch; other gates remain.
- Decide corporate-action and owner-initiated external-contribution accounting.
  M1/M2 have no money-movement interface; the loss model is fixed-capital. Pending
  deposits can be represented as ineligible test data, but external contributions
  cannot be applied to the equity model. Future unitization must not erase losses.
- M3A pins official SDK 3.0.0 and verifies imports/request signing on Python
  3.13.3. Authenticated read-only account/data and stream compatibility is observed; see
  the M3A limitations below. No model capability or order execution has been tested.

These questions do not block offline M1/M2. They remain visible prerequisites for
the relevant later milestones; this implementation grants no trading authority.

## Conservative internal M2 representations

The M1 choices above remain a record of that accepted milestone. M2 replaces
M1's rounded average-cost fixture with exact lots, and uses granular internal
executions rather than assuming the M1 cumulative BrokerEvent is a broker wire
contract. M1's original tests and report are retained unchanged.

- **Lot accounting:** exact FIFO quantities and execution prices; no rounded
  average cost. Fees are recognized exactly once as expense on the execution's
  local session date (including buy fees). Lot basis excludes those expensed
  fees. This is an internal performance-accounting convention, not a tax-lot
  election or a Webull field mapping. Realized net P&L is FIFO gross realized
  P&L minus incurred fees; unrealized P&L uses current bid minus remaining basis.
- **Episodes:** retain M1's conservative instrument/episode binding. Consumed
  loss allowances never replenish through gains or favorable corrections. A
  closed episode with an actual gap loss above its planned allowance cannot be
  increased; it does not permanently prohibit unrelated episodes below the
  independent daily halt. Actual losses are not falsely capped at planned risk.
- **Corrections:** immutable granular execution IDs with expected revision and
  quantity/price/fee replacement. Zero quantity denotes a bust. Original order,
  instrument, execution time and FIFO sequence never change. Multiple dependent
  corrections can apply atomically, with explicit complete terminal order details
  if needed to release reservations. A bust alone does not imply cancellation.
  Inconsistent corrections are quarantined, not partially applied. An impossible
  execution observation also remains unresolved and makes its order uncertain;
  reconciling the old balances cannot clear it. Resolving such evidence requires
  an explicit corrected evidence set, not a generic incident acknowledgement.
- **Settlement adjustments:** settlement confirmations contain the confirmed
  total, effective settlement business day and evidence ID. Repeated totals have
  zero additional effect. A corrected sale after settlement can expose a negative
  unsettled adjustment (a reconciliation liability) until a matching adjustment
  is confirmed. It blocks new risk. Recording this fact does not initiate any
  debit, transfer or borrowing; no such route exists. Similarly, an externally
  observed invalid execution can expose a negative cash discrepancy rather than
  silently rewrite observed facts into an apparently funded account.
- **Calendar/reference inputs:** explicit finite settlement business-day coverage
  is persisted at account creation. Session/instrument/capability fixtures are
  owner-controlled offline inputs. There is no live security master or broker
  certification. The service uses logical fixture time for accounting/session
  replay, and PostgreSQL time for lease/permit expiry. M3 must bind logical time
  and reference evidence to trusted providers and clock-health controls.
- **Durability:** PostgreSQL row locks serialize each account; journal and all
  projections, permits and outbox effects commit together with synchronous commit
  and fsync enabled. Projection replacement is deliberately simple and suited
  to correctness tests, not a throughput claim. The reducer retains schema version
  1 semantics; future changes need a migration/replay compatibility policy.
- **Recovery:** PostgreSQL restart time plus database OID identifies the current
  storage epoch. Reacquisition after restart/restore increments the fence, revokes
  unused old permits and makes unfinished orders uncertain while retaining their
  reservations. Restores use a NEW database and remain blocked pending reconciliation.
  An independently operated future gateway must retain its own maximum fence and
  client-ID history outside a restored ledger backup. M2 does not claim distributed
  fencing across two separately writable database copies or protection against a
  database administrator rewriting all history. In-place logical restore into an
  active authority database is not a supported recovery procedure.
- **Owner authority:** M2 persists authenticated restart attestations/action IDs
  atomically with segments/latches. The deny-by-default M1 authentication boundary
  and local test verifier remain; production login, signing/key custody and an
  external owner-control service remain unresolved. No keys are stored in the journal.
- **Lease/permit durations:** leases accept an explicit duration up to 60 seconds;
  offline permit validity is 30 seconds. These are internal authority-expiry bounds,
  not changes to trading percentages. They require operational review with M3 latency.

## M3A read-only certification decisions and remaining gaps

- **Scope preserved:** sandbox Individual Trading and Market Data only, three
  official sandbox hosts, zero orders/previews/funding/margin/borrowing. M2 core
  and OFFLINE authority are unchanged; no daily-headroom coupling.
- **Authentication resolved:** the owner's authorized local file supplied the
  working pair, imported privately into the same DPAPI store. The exact official
  ApiClient/TradeClient example returns config/account-list 200 with unmodified
  SDK 3.0.0 on Python 3.13.3. Token checking is reported disabled; no token/2FA
  changes. Earlier 401/104/signature diagnostics are historical, not current
  blockers. SHA1/v2 documentation differs from SDK SHA256/v3; no automatic
  fallback/downgrade was added and no signer change is needed for this app.
- **Account designation for observation:** discovery returned one CASH account
  with class INDIVIDUAL_CASH alongside EVENTS_CASH, CRYPTO and margin classes.
  The unique INDIVIDUAL_CASH account was selected under the owner's paper CASH
  identification instruction and its ID saved encrypted. Subsequent detail reads
  require exact local ID and explicit CASH type. This local selection grants
  no ledger bootstrap, funding or execution authority. Sandbox host plus owner
  PaperTrade app context establish environment; no extra environment response
  field is invented.
- **Cash mappings still open:** the USD sample returns $1,000,000 net liquidation,
  cash and settled cash, zero unsettled/market value/P&L, and separate buying-power
  fields. No explicit reserved cash or settlement lots are returned. The flat
  sample does not establish reservations, post-fill settlement, fee liabilities
  or correctness of buying-power-to-cash mapping. No value was imported into M2.
- **Positions/orders still open:** [] and {data: []} establish collection shapes
  only. No populated schemas, FIFO/tax lots, fill/fee/correction fields, terminal
  release proof or multi-page behavior were certified. Current v3 cursors are
  bounded; an empty list never releases a reservation.
- **Instrument mismatch:** AAPL has lot_size string 1.0000000000 (ten decimal
  places), fractionable true and public instrument/session/exchange codes. M1
  accepts eight fractional places; lossless normalization needs an explicit
  mapping, not relaxed core precision. Fractional quantum, executable price
  tick, protection capabilities and code mappings remain uncertified. Subcent
  market prices and broker short/margin/overnight flags grant no new authority.
- **History route mismatch:** official SDK single-history GET returns 404 with
  working keys. Explicit official batch-history POST returns five M1 bars.
  Only that exact bounded read body was added to the transport allowlist; no
  automatic route fallback or order POST capability was added.
- **Data timing observed; entitlement package open:** 22 AAPL HTTP/MQTT timestamps
  are -0.029620 to 0.724623 seconds relative to local receipt, near-real-time for
  this sample. No package/provider-feed/delay flag was returned. Documentation's
  default 15-minute sandbox delay and upgrade rules are not replaced by a global
  entitlement assumption. Negative ages need future trusted clock-health handling;
  descriptive timing thresholds do not modify M1 freshness policy.
- **SDK payload mismatch contained:** SnapshotResult assigns close from open
  although wire Snapshot has no close. Observations use official protobuf decode
  to preserve fields without synthesizing close. Earlier lossy MQTT diagnostic
  fixtures are historical only; final full run 20260911T180624637920Z is the
  payload evidence. No ledger price semantics were changed.
- **Streams connected; event/replay semantics open:** MQTT connected/subscribed
  twice and gRPC acknowledged bit-1 subscriptions twice. Each had one controlled
  reconnect. gRPC deadlines were intentional; no actual order event was produced.
  Sustained availability, delivery ordering, replay/gap repair, expiry recovery
  and execution finality remain later certification work.
- **Rate limits:** documented sandbox endpoint budgets and conservative 3.2-second
  pacing are recorded in M3A_REPORT. No quota/Retry-After headers or 429 were
  observed, and limits were not stress-tested. /openapi/config's limit was not
  found. No empirical capacity claim is made.
- **Containment:** exact sandbox/read-path checks, TLS, no redirects/proxy/netrc,
  bounded responses/timeouts, secret suppression and no automatic token cache.
  Dedicated-process log suppression is not a multithread isolation design.
  SANDBOX_OBSERVATION cannot write the ledger or admit risk; semantic bindings
  and explicit sandbox account setup are required before future reconciliation.
- **Before any first sandbox order:** explicit owner approval for the separate
  order-certification phase, a reviewable bounded plan, necessary account/ledger
  bindings, fee/settlement/calendar/clock inputs, increments and protection/finality
  checks. Earlier economic episode, corporate-action, final owner-authentication
  and gateway-recovery decisions remain open. STOP at M3A.

## M3B first-order certification - incomplete, stopped

The owner explicitly authorized bounded paper-order certification in Step 5C2.
This supersedes M3A's no-order scope only for the separate M3B harness, not the
M3A adapter, M2 OFFLINE authority, or production. All hard limits remain fixed.

- **Observed accepted combination:** one AAPL share, NORMAL/LIMIT/BUY/QTY, DAY,
  CORE, $299.34. REST progressed SUBMITTED to CANCELLED with zero fills. Broker
  and client identifiers were stable across submit/detail/cancel/history. No
  replace, duplicate broker submission, executable buy or sell occurred.
- **Event gate unresolved:** no initial order event was captured. The official
  Individual Trading event guide does not promise a working-order acknowledgment.
  Resolve whether REST acknowledgment plus subsequent required gRPC lifecycle
  evidence satisfies the owner's intended test, rather than silently changing it.
- **Collector defect corrected, empirical retest still required:** SDK order event
  code 1024 is outside the generated protobuf enum-name table. Initial code could
  discard it and stop the collector. This prevents attributing the missing cancel
  event to Webull. The corrected decoder passes synthetic regression tests;
  read-only reconnects did not recover the old application packet. Actual gRPC
  application-event delivery/ordering/replay remains uncertified.
- **Reservations retained:** repeated REST detail/history say CANCELLED/zero fill,
  positions are empty and cash unchanged. Missing cross-channel evidence means
  no automatic release of the local $324.34 full-principal/cost reservation or
  authority for a second first-sequence buy. A reviewed recovery/retest path is
  required; deleting/resetting the private intent journal is not such a path.
- **Cash field mapping narrowed:** while the $299.34 buy was working, settled cash
  stayed $1m but buying power fell to $999,700.66, then recovered on cancellation.
  Do not treat settled_cash as already net of local reservations. No sale occurred;
  settlement timing, receivables and fee liabilities remain unresolved.
- **Fees:** preview estimated_transaction_fee was string zero; actual canceled
  detail/history returned fees [] and commission {}. Empty fields are not a zero
  final-fee guarantee. Incremental/cumulative fees and production schedules remain
  untested. $25 was a disclosed conservative test reserve, not a schedule.
- **Increments/capabilities:** quantity 1 and a cent-aligned price were accepted;
  the minimum tick and fractional quantum were not established. No explicit tick
  metadata was returned. STOP_LOSS/STOP_LOSS_LIMIT/trailing/combos are documented
  but remain empirically uncertified, particularly quantity conservation and OCO.
- **Calendar:** SDK /trade/calendar returned 404. Current official exchange-calendar
  evidence allowed the explicitly finite 2026-09-11 session only. No alternative
  broker endpoint, weekday guess or permanent calendar assumption was added.
- **History mismatch:** two explicit time-filter forms returned 417 parameter
  errors, including the SDK-described millisecond/offset form. The documented
  default seven-day query succeeded. Filter format and multi-page semantics stay
  open; the one-page response had no pagination key.
- **Harness-only implementation choices:** separate SQLite FULL-sync append-only
  intent journal, single first-sequence PLACE, one-use immutable requests, account
  fingerprint binding, full-principal risk, finite date, disclosed quote/account
  freshness bounds and price collar. No M2 schema, latches, broker-account bootstrap
  or risk permits were changed. This is not the final shared PostgreSQL execution
  gateway or distributed fencing solution; treating it as such would bypass the
  architecture and is not authorized.
- **Stopped sequence:** replace/amend, fills, owned-share sell, populated positions,
  protective mechanisms, post-sale settlement and fee progression were not run.
  Existing episode, corporate-action, owner-authentication and gateway questions
  remain. M3B is incomplete; M4 has not begun. See docs/M3B_REPORT.md.


## M3B essential round trip — owner-approved scope revision (2026-09-11)

REST submit plus matching direct REST detail now establishes initial sandbox
acceptance. gRPC capture is observational, not a prerequisite or release gate.
The owner explicitly authorizes release of the prior $324.34 harness-only
reservation using repeated CANCELLED/zero-fill detail, matching identifiers,
flat positions and unchanged cash. Preserve all original evidence and append a
release record; no M1/M2 or production release rule changes.

Only one one-share AAPL marketable LIMIT BUY and its owned-share LIMIT SELL are
authorized. No replace, OCO/brackets, fractional, duplicate/replay or pagination
stress. Persistent per-side claims forbid repeats across restart. Fresh REST
account/detail/positions are required; unknown or conflicting state stops writes.
Conservative internal choices: 0.2% quote collar rounded outward to cents,
maximum 0.2% spread, quote age <=2 seconds, observation bundle <=30 seconds,
final gate <=5 seconds, price $1-$1000, full principal plus $25 reserved as
worst-case risk, no sale proceeds reused. These are finite harness controls,
not assertions of broker minimum tick, fee schedule or production authority.


### Essential M3B result — complete under revised owner scope

One AAPL share bought at 332.76 (limit 333.43), sold at 332.75 (limit 332.07),
both REST FILLED, account flat and no open orders. Two gRPC FINAL_FILLED events
also matched REST, observationally. Prior $324.34 harness reservation released
by append-only owner record; historical evidence retained. This supersedes the
initial-ack/retest blocking questions above for M3B only. No M2 rule changed.

Observed mismatches/remaining limits:
- Sale proceeds 332.73 appeared unsettled while buying power included them.
  Settled cash stayed 999667.24; final cash/equity 999999.97. Never treat buying
  power as eligible settled funds. Actual later settlement remains untested.
- SELL REST fee rows use receivable_value; gRPC uses receivable_fee. Two REST
  actual_value amounts of 0.01 each reconcile cash loss of 0.03 including a
  0.01 price loss. Incremental/cumulative fee behavior and finality are unknown.
- The conservative sanitizer redacted fee type labels, gRPC receivable_fee
  values and new position cost/ratio/day-realized scalars. Shapes and retained
  fee amounts are available; missing scalar certification is explicit. Do not
  invent those values or infer production pricing from an empty commission.
- Position quantity=1, EQUITY/AAPL and cost_price=332.76 were captured. No
  sellable/available quantity field appeared. This one-share exit was bound to
  the immediately preceding full buy fill plus current ownership/no open orders;
  it does not establish general broker lot allocation or settlement semantics.
- No order replacement, protection/OCO, fractional, deduplication/replay or
  pagination stress was performed. These are outside the revised essential
  acceptance scope, not implicitly certified. M4 remains unapproved.


## Minimum autonomous MVP - 2026-09-11
Owner authorized exploratory AAPL paper strategy, configured model/reasoning, DRY_RUN default, explicit SANDBOX, disabled PRODUCTION and synthetic execution proof. No profitability proof or trade quota imposed. Hard limits and approved denominators remain unchanged; daily-headroom coupling remains absent.
Necessary internal representation: initial-only OpeningStatement preserves accepted M3B cash, outstanding receivable, original experiment/high-water and consumed loss. Not a reset/funding operation. Exact due settled/unsettled deltas confirm settlement. General settlement/correction/corporate-action semantics remain unresolved.
MVP bounds: observed whole-share/cent grid, spread <=20bps, quote <=2s/account <=10s at execution; bounded 90s model context. Persistent mvp-aapl episode and $1+$0.02/share per-leg fee allowance plus 10bps stress are conservative internal choices, not universal broker semantics. General episode boundaries and fee finality remain open.
Local stop is synchronous/process-dependent, not broker-native certification. Partial/unknown fill, outage, nonfilled exit or restart with holding requires reconciliation. Automatic monitor recovery, production stop guarantees and final owner authentication remain unresolved. M2 OFFLINE records gain external sandbox transport only via explicit account-bound gateway. See docs/MVP_REPORT.md.


## Owner-authorized unattended monitoring MVP

Owner explicitly authorizes autonomous SANDBOX dispatch after existing validation/risk admission, without per-trade approval. Dashboard stays read-only; hard-risk changes, mode/configuration changes, emergency stop and restarting latched safety halts remain owner authority.

Owner selected Windows signed-in task operation (locked screen/sleep allowed). Signed-out operation remains optional and requires local owner Windows credential entry, not API credentials in the task. Wake settings are supported/enabled; firmware/physical wake was not destructively tested. Denver scheduling uses dual fixed UTC triggers and a Denver-time lower-bound guard; exchange session gates remain authoritative.

Known fully filled holdings now restore their persisted validated plan on restart. The former missing in-memory plan limitation is resolved for that case. Unknown/partial execution, mismatched reconciliation, missing plan and broker-native protection/outage guarantees remain unresolved and fail closed. No incident/loss latch is automatically cleared by runner restart. Operational event/chart records are not a second accounting authority.

## Compact alpha reset — explicit limits retained (2026-09-15)

- Broad opportunity access currently means a bounded 30-name coverage sample
  from the official Webull sandbox `TOP_ACTIVE` response, cached for five
  minutes. It does not certify a complete US exchange security master or every
  liquid US stock. A complete universe/feed contract remains open.
- Market and sector context fields exist but remain empty unless a separately
  certified existing feed provides them. No news, fundamentals, macro feed, or
  research platform was invented for this reset.
- Counterfactual baselines use a five-minute observation horizon and have no
  execution authority. Matching days, survivorship controls, slippage studies,
  and statistical alpha claims remain later evidence work.
- Dashboard net economics include trading P&L, broker fees, and recorded model
  cost. Electricity and other operating costs remain unavailable and are labeled
  incomplete rather than inferred.
- Dynamic protection remains local and therefore depends on process uptime,
  connectivity, and fresh data. Broker-native stop/OCO protection remains
  uncertified; unknown state still fails closed.
- Legacy `TradeDecisionV1` remains for historical replay, recovery, and
  compatibility tests. The active initialized unattended runner uses
  `AlphaDecision`; removing the legacy contract is not required for this reset.
