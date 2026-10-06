# Spot Testnet adapter — bounded integration stage

This module is isolated from the paper engine and the HTTP application. Production trading remains
hard-disabled. There is no strategy-driven trader or UI activation for Testnet. The diagnostic CLI offers
credential setup, read-only account preflight, local status, and reconciliation. A separate
explicitly confirmed CLI runs one bounded virtual-funds entry and manages its protection. The transport and OCO lifecycle are exercised with mocked HTTP responses.
Authenticated exchange execution has **not** been verified. Read-only authenticated account
and quote preflight passed for BTCUSDT and SOLUSDT on 2026-10-06 using local credentials.

## Local credential setup

Create HMAC credentials in the official [Binance Spot Test Network](https://testnet.binance.vision/).
Use Testnet credentials only. Do not paste either value into chat, issue bodies, browser forms in
this application, or shell command arguments. From the repository root:

```bash
.venv/bin/python -m spotlab.testnet_cli configure
.venv/bin/python -m spotlab.testnet_cli preflight --symbol BTCUSDT
.venv/bin/python -m spotlab.testnet_cli preflight --symbol SOLUSDT
.venv/bin/python -m spotlab.testnet_cli status
```

The setup prompts hide both inputs. They write `data/testnet-credentials.json`, excluded from Git,
with owner-only permissions (600); this file is **not encrypted**. The backend CLI reads it only
for that invocation. Existing files are never overwritten; loading rejects symbolic links or
files accessible to other local users. Keep the computer account and disk protected. An external
secret manager can instead inject `SPOTLAB_TESTNET_API_KEY` and `SPOTLAB_TESTNET_API_SECRET`.
Production `BINANCE_API_KEY` variables are not read. Active third-asset commission discounts
(such as BNB) block new operations until that fee mode can be accounted for; the tool does not
change the exchange account setting. Testnet may return an explicit null discount asset, even
with enabled flags and a zero discount rate. Read-only diagnostics preserve that unknown value;
an enabled discount with an unknown asset still blocks submissions. Missing or malformed fields
remain rejected. No balance or credential is printed by preflight.
A blocked result prints an exception category only; do not enable HTTP debug logging with secrets.

Reconciliation requires an existing durable intent, not an arbitrary exchange account order:

```bash
.venv/bin/python -m spotlab.testnet_cli reconcile --symbol BTCUSDT --intent EXISTING_INTENT_ID
```

The default ledger is `data/testnet-orders.sqlite`, separate from the paper database. Preserve it
across process restarts. Testnet periodically resets exchange data; a missing exchange order is
not proof of cancellation and will remain UNKNOWN. Manual reconciliation of a reset is still an
operational blocker; do not delete the ledger to silence an unknown order.

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
third-asset/BNB commission blocks reconciliation rather than guessing its quote value.

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

## Remaining live blockers

Authenticated Testnet order/failure/restart evidence remains outstanding after read-only preflight.
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
and prove that enabled unknown fee assets still prevent any order POST, even at a zero rate.

Protocol references: [official Testnet REST API](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md)
and [general information](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md).
