# Spot Testnet adapter — bounded integration stage

This module is isolated from the paper engine and the HTTP application. Production trading remains
hard-disabled. There is no strategy-driven trader or UI activation for Testnet. The diagnostic CLI offers
credential setup, read-only account preflight, local status, and reconciliation. A separate
explicitly confirmed CLI runs one bounded virtual-funds entry and manages its protection. The transport and OCO lifecycle are exercised with mocked HTTP responses.
An authenticated, bounded BTCUSDT LIMIT submission, timed cancellation and restart reconciliation
passed on 2026-10-13 without a fill. A second bounded BTCUSDT trial filled its entry and installed
an exchange-side OCO; both children were verified open. A fresh manager process resumed protection
observation without another BUY. On 2026-10-17, authenticated reconciliation confirmed the stop
filled and the target expired; the cycle was EXITED with no remaining inventory or dust. A fresh
process repeated reconciliation without adding intents or fills. Read-only authenticated preflight
passed for BTCUSDT and SOLUSDT.
On 2026-10-17, another bounded entry verified recovery after locally discarding accepted BUY
and cancellation responses, with a fresh process after each interruption. It ended NO_FILL.

## Local credential setup

Create HMAC credentials in the official [Binance Spot Test Network](https://testnet.binance.vision/).
Use Testnet credentials only. Do not paste either value into chat, issue bodies, browser forms in
this application, or shell command arguments. From the repository root:

```bash
.venv/bin/python -m spotlab.testnet_cli configure
.venv/bin/python -m spotlab.testnet_cli preflight --symbol BTCUSDT
.venv/bin/python -m spotlab.testnet_cli preflight --symbol SOLUSDT
.venv/bin/python -m spotlab.testnet_cli audit
.venv/bin/python -m spotlab.testnet_cli status
```

The setup prompts hide both inputs. They write `data/testnet-credentials.json`, excluded from Git,
with owner-only permissions (600); this file is **not encrypted**. The backend CLI reads it only
for that invocation. Existing files are never overwritten; loading rejects symbolic links or
files accessible to other local users. Keep the computer account and disk protected. An external
secret manager can instead inject `SPOTLAB_TESTNET_API_KEY` and `SPOTLAB_TESTNET_API_SECRET`.
Production `BINANCE_API_KEY` variables are not read. Active third-asset or unknown commission
discount assets block submissions unless a fresh, complete account response proves all twelve
standard/tax/special maker/taker/buyer/seller rates are exactly zero. This narrow exception applies
only to the pinned Testnet transport, and is rechecked before actual submissions, including exits.
A zero discount rate alone is never proof of zero fees. Missing, malformed or nonzero components
cannot activate the exception. The tool does not change the exchange account setting.
Testnet may return an explicit null discount asset even with enabled flags; read-only diagnostics
preserve that unknown value. No balance or credential is printed by preflight.
A blocked result prints an exception category only; do not enable HTTP debug logging with secrets.

Reconciliation requires an existing durable intent, not an arbitrary exchange account order:

```bash
.venv/bin/python -m spotlab.testnet_cli reconcile --symbol BTCUSDT --intent EXISTING_INTENT_ID
```

The default ledger is `data/testnet-orders.sqlite`, separate from the paper database. Preserve it
across process restarts. Testnet periodically resets exchange data; a missing exchange order is
not proof of cancellation and will remain UNKNOWN. Manual reconciliation of a reset is still an
operational blocker; do not delete the ledger to silence an unknown order.

## Open-order audit before a new entry

`audit` reads an authenticated account-wide open-order snapshot and compares it with all intents
in the selected local ledger. Its output contains aggregate counts and reason codes, not account
balances, client/order IDs or raw exchange responses. A blocked result exits nonzero. The selected
BTCUSDT/SOLUSDT transport does not limit the account-wide open-order check to that pair.

New bounded entries require this check, including a fresh check at the final send boundary.
Unresolved local intents, incomplete terminal trade records, remaining bot-owned inventory,
known terminal orders still open, identity inconsistencies or untracked IDs matching the app's
generated namespace block entry. For OCO records, target and stop child IDs are included even
when the parent is terminal. Only the exact unsent candidate's local record can be exempted;
a matching remote order always blocks another submission. A missing open order never proves
cancellation. Unrelated untracked orders remain untouched and still count in venue capacity
checks; their manual or automated provenance is not inferred.

The audit does not submit, cancel, adopt or erase exchange orders. On a verified bound account,
existing-entry reconciliation, cancellation and protective exits remain available while the
open-order audit blocks new entry. An unverified or different account blocks these operations too:
the application's old inventory cannot authorize a sale on another account. Use the original
cycle/intent and ledger to investigate; do not delete records or invent replacement identifiers
to bypass a blocker. The final audit is followed by the entry deadline and persistent stop checks.
Preflight age/clock drift and account-wide capacity observations are checked again after the
audit's network calls. Local evidence is read consistently under the writer lock, then compared
again at send authorization. A proven-unsent refusal retains an allowlisted reason; a fresh
manager closes that local attempt as ABORTED without querying a nonexistent exchange order.
Any original audit blocker still has to clear before a new entry can pass its own checks.

This is a point-in-time open-order/ledger comparison, not complete historical account or trade
reconciliation. Account binding establishes the observed account identity, not a reset generation.
An empty snapshot cannot prove that a Testnet reset was reconciled. Arbitrary untracked client IDs
cannot reliably be attributed to this app. The separate legacy-ledger verifier is described below;
orphan/reset recovery and continuous unattended monitoring remain separate work.

## Account binding and reset boundaries

The ledger's account binding is immutable and uses the fixed Spot Testnet venue and the positive
integer `uid` returned by the signed [account endpoint](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md#account-information-user_data).
API keys are not account identifiers: rotating credentials for the same UID does not require
replacing the binding. UID values remain in the local database and are not diagnostic output.

Initial binding is permitted only when the ledger contains no orders, fills, protection payloads
or cycles. The check and binding write share the ledger writer lock. A historical ledger without
a binding requires migration; a successful account request alone cannot establish that those
old records belong to the currently selected account. Existing ledgers from earlier versions must
be preserved, including closed cycles. Do not erase them, create replacement identifiers or choose
a fresh database to work around this blocker. A separate verified migration handles only closed,
fully acknowledged history with zero remaining bot inventory; see below.

Account identity must be verified before order queries, submissions, cancellations and OCO
management. Missing or malformed identity, account-query failure and UID mismatch fail closed.
Local status and account preflight remain available for inspection. Successful binding does not
authorize trading or restore an expired entry deadline.

For an initial installation with an empty ledger, explicitly record the observed account:

```bash
.venv/bin/python -m spotlab.testnet_cli bind-account --confirm-testnet
.venv/bin/python -m spotlab.testnet_cli audit
```

`bind-account` writes only the local binding and sends no exchange order. Repeating it with the
same bound account is idempotent; a different account cannot replace the binding. It is not the
migration procedure for a historical unbound ledger.

The [official Testnet reset FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md#all-my-data-has-disappeared-what-happened)
states that resets remove pending and executed orders, replenish virtual balances and preserve API
keys. Therefore an unchanged key or UID is not reset-resolution evidence. Missing historical
orders must not be converted into cancellations, zero inventory or permission to replace an order.
No automatic rebind, incident clearance, inventory write-off or reset adoption is provided.

When a query reports `-2013` for an order/list previously acknowledged in the ledger,
`account_history_missing` is latched persistently. It blocks **all order mutations**, including
cancellation and new protection, before their local claims are created. Verified read-only queries
remain available; later successful queries do not automatically clear the incident. Existing
exchange-side orders are not canceled by this local block. A temporarily absent unacknowledged
submission remains query-only without this history-loss latch, so later confirmed fills can still
receive protection after their ambiguity is resolved.

Account verification happens before local lifecycle transitions and again at the HTTP boundary.
An attached transport cannot be reassigned to another store object. Time, preflight freshness and
entry authority are checked after the last awaited identity call. An OCO claim proven unsent due
to this last freshness check may return to INTENDED with the same payload and reservation;
ambiguous or acknowledged submissions never take that retry path.

On 2026-10-18, the existing authenticated local ledger correctly returned `migration_required`.
Its historical order, fill, protection and cycle rows were compared before and after and remained
unchanged. No exchange order was submitted or canceled, and no binding was assigned to that legacy
ledger. The migration below is required before that legacy ledger can pass the binding gate.

## Verified migration of a historical unbound ledger

`migrate-legacy` uses a separate fixed-host, GET-only reader. Normal order routes retain their
binding checks throughout verification. Preview reads historical evidence and reports aggregate
reason codes; it does not assign an account or modify existing order, fill, protection or cycle rows.

```bash
.venv/bin/python -m spotlab.testnet_cli migrate-legacy
```

The supported ledger must have no binding or unresolved account incident, acknowledged terminal
orders with complete fills, structurally consistent closed cycles, and exactly zero remaining
inventory per scope and symbol. Each ordinary order is matched to its authenticated exchange
identity and terms. OCO verification checks the parent, both derived child identities and terminal
states, prices and quantities. Complete authenticated trade sets must match local trade IDs,
amounts and commissions exactly; a matching aggregate alone is insufficient. The commission-asset
field must be present even for a zero fee; explicit null/empty is accepted only for zero. Persisted
LIMIT/OCO exchange IDs must use their canonical numeric spelling. A cycle entry's exchange creation
time must not exceed its stored entry deadline plus the existing 1000 ms clock tolerance. Missing history,
unresolved submissions, unmatched protection payloads, residual dust or inconsistent evidence block
the migration. Account-wide open orders are checked for known or suspicious bot identities;
unrelated orders remain untouched.

After a successful preview, preserve a consistent database backup before applying. The apply
command performs the verification again; it does not trust a previous preview as authorization:

```bash
.venv/bin/python -m spotlab.testnet_cli migrate-legacy --confirm-testnet
```

The authenticated account identity must remain consistent throughout verification. Under the
ledger writer lock, the entire relevant local snapshot is compared again before account binding
and a verification receipt are inserted atomically. Historical rows, inventory, cycle stop flags
and expired deadlines remain unchanged. The receipt records verification provenance locally; UID,
private identifiers and trade data are not diagnostic output or Git artifacts.

This establishes that all supported local history matched the currently authenticated account at
verification time. It is not proof of a reset generation or complete history outside this ledger.
It cannot recover history already removed by Testnet, clear a history-loss incident, migrate an
active position or adopt unrelated orders. Successful migration does not start a trader, renew an
entry authorization or enable production. A subsequent ordinary `audit` must still pass.

On 2026-10-19, authenticated preview verified the existing local ledger: 4 orders, 2 fills,
3 closed cycles and no unrelated open orders. A consistent SQLite backup was created with mode
600. Apply repeated remote verification, then inserted the binding and receipt; every historical
order, fill, protection and cycle row remained identical to the backup. No exchange mutation was
sent. The subsequent ordinary startup audit passed with `account_binding_verified: true` and
`reset_resolution_verified: false`. This is evidence for this closed ledger only, not continuous
trading or reset recovery.

## Transport and identity

The client owns its HTTP session and is pinned to `https://testnet.binance.vision`. There is no
endpoint override, injected real HTTP client, proxy environment, or redirect following. HMAC signs
the exact percent-encoded payload. Method/path combinations are explicitly allowed; account-wide
cancel and withdrawal operations are absent. Secrets use redacted values and transport errors do
not retain raw response bodies, signed URLs, or chained HTTP exceptions in the order ledger.

Requests have a 5-second receive window, server-time synchronization, bounded network timeouts,
and per-transport Retry-After cooldown for 429/418. Network errors, 5xx, timeout codes, malformed
responses and ledger validation failures preserve UNKNOWN. No mutation is retried. Restart
reconciliation checks immutable symbol, client ID, quantity, side, order type, price and GTC terms.
Order and trade identifiers include the symbol, since exchange numeric IDs are not globally unique.
Authenticated trades are paginated and deduplicated. Base and quote commission are supported;
positive third-asset/BNB commission blocks reconciliation rather than guessing its quote value.
An authenticated fill with exactly zero commission and an explicit null/empty asset uses the
empty-string ledger marker for no fee denomination, with both base and quote fees zero. Missing
or malformed asset fields remain rejected; positive fees cannot use this marker. Actual fills
remain authoritative if exchange fees change after preflight.

## One OCO reservation

A protective SELL uses one synthetic `OCO` parent in the durable order ledger. It reserves the
quantity once for the mutually exclusive target and stop legs. A separate durable payload stores
stop price and deterministic child IDs before any network submission. Normal SELL orders compete
for the same writer-serialized inventory. Only confirmed bot BUY fills, minus base commissions,
create sellable inventory; manual account holdings do not.

The target is LIMIT_MAKER and the stop is STOP_LOSS (market execution after triggering). Local
validation requires target > last traded price > stop and aligned quantity/prices. A stop trigger
cannot guarantee a fill price. A POST acknowledgement is insufficient to report protection: query
the list and both children, verify their exact terms, and fetch authenticated trades. ALL_DONE alone
never releases inventory. Only complete child executions and terminal states release the remainder.
Partial execution, a missing child, unpriced fees or missing trade details cannot silently free a
reservation. An unknown submit is queried with its original list ID; no replacement is created.
`both_legs_open` is a timestamped observation, not a lasting guarantee or a UI "protected" badge.
Generic single-order lifecycle operations reject OCO parents so they cannot accidentally cancel
protection through the ordinary order endpoint.

## Applicable filters and one-cycle entry

New submissions evaluate symbol, exchange and account-specific filters from exchangeInfo and
myFilters. Validation includes enabled price/lot steps, both notional rules, dynamic price bands,
open-order/algo/list counts, position and asset limits. Reference prices take precedence over the
exact required weighted-average interval. An unavailable/mismatched reference, unknown rule,
clock drift over one second or snapshot older than five seconds blocks the operation.
Quote-asset MAX_ASSET on a market/OCO order is blocked because a reference price cannot
bound its eventual fill notional. MAX_POSITION conservatively counts original open BUY quantity
until the exact partial-fill semantics are independently verified. OCO counts
two orders, one algorithmic order and one list; BUY preflight leaves that capacity available.
Manual orders count towards account limits but are never canceled. Exchange rejection remains
possible because account state and prices can change after a snapshot.

The separate `spotlab.cycle_cli` manages one immutable cycle at a time. It allows **at most 100
virtual USDT of entry notional**, with a user-supplied smaller cap and at most ten minutes of entry
authority. This is an integration test limit, not production capital or loss authorization. It does
not select prices, signal an economic opportunity, guarantee a stop fill price or loop into new
trades. Supply current, exchange-aligned values instead of the placeholders below:

```bash
.venv/bin/python -m spotlab.cycle_cli start --cycle UNIQUE_ID --symbol BTCUSDT \
  --quantity QUANTITY --entry LIMIT_PRICE --target TARGET_PRICE --stop STOP_PRICE \
  --max-notional VIRTUAL_USDT_CAP --entry-seconds 60 --watch-seconds 90 --confirm-testnet
```

Plan and BUY intent are persisted atomically before any send. Before BUY, the full-size future
OCO types, target/stop steps, lot and dynamic/notional constraints are preflighted using a
hypothetical acquired balance. The same checks repeat immediately before sending the BUY;
actual protection is checked again after fees and fills are known. On partial execution, the remainder
of the BUY is canceled once. Protection waits for terminal entry status and complete authenticated
fills/fees, including fills arriving during cancellation. The net base quantity is rounded down to
both enabled lot increments. Exact protection quantity and residual dust are persisted before OCO
creation. Tiny quantities or stale/moved stop/target levels remain visibly unprotected; the bot does
not silently shift prices, borrow manual holdings or invent a replacement exit.

Resume after interruption using the **same cycle ID and database**:

```bash
# Read-only observation of the existing cycle:
.venv/bin/python -m spotlab.cycle_cli manage --cycle UNIQUE_ID --symbol BTCUSDT
# Confirmed exit management; this never creates a BUY:
.venv/bin/python -m spotlab.cycle_cli manage --cycle UNIQUE_ID --symbol BTCUSDT \
  --confirm-testnet --monitor --watch-seconds 600
# Stop the remaining entry while keeping exit management:
.venv/bin/python -m spotlab.cycle_cli manage --cycle UNIQUE_ID --symbol BTCUSDT \
  --confirm-testnet --stop-entry
```

A repeated start cannot renew its saved deadline or change terms. A persisted stop is checked again
immediately before sending, after preflight, and prevents an unsent BUY even after restart. Only
INTENDED orders, for which no send was claimed, may be claimed once; SUBMITTING becomes UNKNOWN on
restart and is query-only. Ambiguous cancellation is not retried and may need manual investigation.
A terminal locally aborted intent is never looked up as if it had reached the exchange.

The manager is bounded and stops when its observation time expires. Its final JSON includes the
cycle ID, remaining inventory, dust, unprotected quantity, whether the protection observation is
fresh, and whether resumption is required. PROTECTED is only a recent observation of both legs;
a stale or UNKNOWN ledger cannot retain that label. `--monitor` keeps checking existing protection
until the bounded observation deadline or an error/terminal condition. No process is installed as
a background service. Computer sleep, shutdown or an unavailable venue can leave exposure requiring
resumption. A leftover dust position is explicitly reported and blocks a new cycle; no automatic
dust disposal or ledger reset is provided.

## Foreground watcher for an existing cycle

`watch` supervises one already persisted cycle. It never calls `start`, creates a BUY,
chooses a strategy or renews the stored entry deadline. Keep the terminal and computer awake;
this command does not install a background service or restart itself after the process exits.

```bash
# Observe an existing cycle for 60 seconds; no exchange mutations:
.venv/bin/python -m spotlab.cycle_cli watch --cycle UNIQUE_ID --symbol BTCUSDT --run-seconds 60
# Manage the existing entry/protection until closure, interruption or an attention condition:
.venv/bin/python -m spotlab.cycle_cli watch --cycle UNIQUE_ID --symbol BTCUSDT --confirm-testnet
```

Observation is the default. It can update local reconciliation records, but cannot send or cancel
an exchange order or set the persistent entry-stop flag. Confirmed management requests the
persistent entry stop after account verification, before further management. It may cancel the
remaining entry and install the existing deterministic SELL OCO once confirmed fills and fees
permit it. An accepted but unanswered submission remains query-only; no replacement IDs or
automatic OCO resubmission are introduced.

A healthy PROTECTED observation keeps the watcher running. Closed cycles end successfully;
incomplete protection or a non-retryable blocker requires attention. Only explicitly classified
transient transport/timeouts retry, using capped exponential backoff and a consecutive-failure
limit. Successful observations reset the counter. The exact `account_identity_unavailable` reason
also retries within that bound because account transport outages are normalized to this reason;
every attempt must verify identity again before management. Identity mismatch, missing binding,
history-loss incidents, invariant failures and arbitrary API errors require attention. Venue
cooldown still applies.

Polling defaults to 2 seconds (`--poll-seconds`, range 1–5). Consecutive failures default to 5
(`--max-consecutive-failures`, range 1–100); retry delay doubles up to 30 seconds. `--run-seconds 0`
means no process time limit, while a finite positive value stops further passes at that deadline.

Health output distinguishes the running process from confirmed coverage. Errors suppress cached
coverage claims; successful protection observations still expire. The final record reports that
the manager has stopped and whether the existing cycle needs resumption. Interrupting the process
does not perform extra network requests; restart uses the same database and cycle ID. Exchange-side
OCO orders can remain active after the process exits, but local monitoring has ended.

Cooperative cycle managers share an exclusive lock. File-backed SQLite uses an adjacent canonical
`.manager.lock` file; PostgreSQL holds a dedicated session advisory lock. A second watcher or
legacy cycle manager fails before exchange access. The lock has no timeout takeover; process/session
exit releases it. Lock ownership is checked before every pass; detected loss stops the next pass.
An in-flight pass may finish after a PostgreSQL session disconnect. Durable order/cancel claims
remain the duplicate-prevention mechanism; this is not exchange fencing or high-availability
failover. The lock does not cover manual orders, other Binance applications or arbitrary database writers.

## Remaining live blockers

Authenticated Testnet partial-fill and adverse failure scenarios remain outstanding, including
actual network disconnects during submission/cancellation, missing child observations and venue resets.
The controlled accepted-response discard below verifies a narrower interruption boundary. Successful
bounded trials do not establish unattended or production reliability.
Broader unattended entry selection, UI controls, account-wide orphan/reset recovery, third-asset
commission pricing and production operations remain unfinished. The paper UI is not wired to
these adapters. Live authorization, 30 calendar days of forward paper observation, economic
evidence and independent production approval remain separate blockers.

## Evidence

On 2026-10-02 and 2026-10-05, unauthenticated Testnet exchangeInfo queries returned TRADING, OCO support and
LIMIT_MAKER/STOP_LOSS support for BTCUSDT and SOLUSDT. This confirms public connectivity and metadata
only. On 2026-10-05, referencePrice and avgPrice also returned HTTP 200 for both pairs;
the reported average interval was five minutes. No signed account request or Testnet order was performed. Mock tests cover signatures,
timeouts, cooldown, response identity, fee inventory, shared reservations, restart reconciliation,
partial exits, incomplete/malformed child observations, bounded entry/protection cycles, persisted
stop races, local abort, stale coverage and secret-file protection. All cycle scenarios also run
against PostgreSQL in CI. None of these mock outcomes is an authenticated exchange fill.

On 2026-10-06, signed account and commission queries plus public metadata and quotes passed for
BTCUSDT and SOLUSDT. No order was submitted. Regression tests cover explicit null discount assets
and proved that a zero discount rate alone does not permit an order POST.

On 2026-10-09, signed account commission queries for both pairs returned all twelve components
as exact zero. Signed `POST /api/v3/order/test` with `computeCommissionRates=true` independently
returned all six order-specific standard/tax/special maker/taker rates as zero. These requests
validate parameters and return commission information without reaching the matching engine;
they are not executed trades or proof of OCO lifecycle behavior.

On 2026-10-13, a single BTCUSDT LIMIT entry using the bounded cycle controller reached NEW,
then CANCELED after its 25-second entry authority expired. The cycle ended NO_FILL with no bot
inventory, dust or unprotected quantity. A fresh CLI process reconciled the same cycle and
remained NO_FILL; no second BUY was created. The explicit test cap was 20 virtual USDT.
No production request was made. This did not exercise a fill, base-fee deduction, OCO creation
or a filled-position restart. Private order IDs, credentials and ledger files remain outside Git.

A second bounded BTCUSDT trial on 2026-10-13 reached FILLED for the entry, confirmed the trade
and zero fees, and created one OCO parent. Both LIMIT_MAKER target and STOP_LOSS children were
queried and confirmed NEW. A new CLI process resumed the same cycle as PROTECTED without a
second BUY or duplicate protection intent. The bot-owned quantity was fully covered with no
dust. The explicit cap remained 20 virtual USDT; this was a protocol test, not a strategy result.
On 2026-10-17, authenticated reconciliation confirmed the closing trade and the OCO parent as
FILLED. Separate child queries confirmed STOP_LOSS FILLED and LIMIT_MAKER EXPIRED. The cycle
reached EXITED with zero owned, dust and unprotected quantity; no manager resumption was required.
A fresh process reconciled the same cycle again, preserving EXITED and unchanged intent/fill
counts. This date records closure verification, not the execution timestamp. No new BUY or exit
was submitted during these checks. This completes one virtual entry/protection/exit round trip;
it does not establish profitability, partial-fill recovery or unattended reliability.

On 2026-10-17, one additional bounded BTCUSDT entry used a 20 virtual USDT cap and 30-second
entry authority. A private runner retained the pinned Testnet transport and all normal preflight
checks. After a successful entry POST returned, it discarded that response locally and raised a
process-interruption exception before order parsing. The lifecycle persisted UNKNOWN; the runner
persisted stop-entry before ending. A fresh, unmodified read-only manager queried the same order
and found NEW without another BUY. A separate process then canceled that same entry and discarded
the successful DELETE response at the same boundary. UNKNOWN and cancel_requested were durable.
A fresh normal manager reconciled CANCELED/NO_FILL with zero inventory, dust and unprotected
quantity. Subsequent replay preserved one intent and zero fills; a guarded replay allowing ordinary
management confirmed no mutating request was attempted. No OCO was needed in this no-fill trial.

This was an intentionally discarded accepted response and process interruption, not an actual
socket failure or exchange outage. The local runner, trace and identifiers remain outside Git.
No runtime fault-injection option was added to the application. The stateful MockTransport tests
exercise the real transport/parser and durable controller with query outages, partial fills,
late cancellation fills, delayed trade details, net base-fee sizing and ambiguous OCO recovery.
Those deterministic partial-fill outcomes are simulated evidence, not authenticated exchange fills.

On 2026-10-18, the authenticated read-only startup audit passed against the existing local ledger
and current Testnet account-wide open-order snapshot. No new order, cancellation or account setting
change was made. Mock tests cover candidate collisions, orphan-looking IDs outside BTC/SOL,
incomplete records, residual inventory, OCO child identity, late ledger/capacity changes, stale
preflight, clock drift, expiry, stop requests and sanitized CLI failure output. This observation
does not verify account binding, reset recovery or future snapshot consistency.

Protocol references: [official Testnet REST API](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md)
and [general information](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md).
