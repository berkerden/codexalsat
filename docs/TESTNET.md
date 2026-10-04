# Spot Testnet adapter — bounded integration stage

This module is isolated from the paper engine and the HTTP application. Production trading remains
hard-disabled. There is no entry command, background trader, or UI activation for Testnet yet.
The CLI offers credential setup, read-only account preflight, local status, and reconciliation of
already-persisted intents. The transport and OCO lifecycle are exercised with mocked HTTP responses.
Authenticated exchange execution has **not** been verified; no account keys were supplied.

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
Production `BINANCE_API_KEY` variables are not read. No balance or credential is printed by preflight.
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

## Current submission gate and remaining work

Local validation covers PRICE_FILTER, LOT_SIZE and NOTIONAL. Unknown filter types and applicable
filters whose constraints are not locally implemented block new submission. Dynamic percentage
price bands, order-count/position limits, and stop-market lot constraints require additional
implementation before the current exchange filter set can be submitted. Metadata and recovery
queries remain available even when these entry gates block or the symbol stops trading.
This is intentionally an integration foundation, **not a completed A6 automatic trader**.

Next implementation steps are full applicable-filter validation, bounded Testnet entry tooling,
partial-entry-to-protection orchestration, ongoing stop coverage monitoring and orphan/restart
recovery, followed by authenticated exchange failure scenarios. The API/paper UI is not wired to
these adapters until that control flow is complete. Live authorization, 30 calendar days of forward
paper observation, economic evidence and operations approval remain separate blockers.

## Evidence

On 2026-10-02, unauthenticated Testnet exchangeInfo queries returned TRADING, OCO support and
LIMIT_MAKER/STOP_LOSS support for BTCUSDT and SOLUSDT. This confirms public connectivity and metadata
only. No signed account request or Testnet order was performed. Mock tests cover signatures,
timeouts, cooldown, response identity, fee inventory, shared reservations, restart reconciliation,
partial exits, incomplete/malformed child observations and secret-file protection.

Protocol references: [official Testnet REST API](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md)
and [general information](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md).
