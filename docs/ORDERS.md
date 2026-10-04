# Durable order-intent core

`spotlab.orders` is the durable ledger shared by the isolated Testnet transport and protective
OCO lifecycle. The ledger itself contains no HTTP client or credentials. The application/paper UI
does not route orders through these adapters. See [Testnet scope and gates](TESTNET.md).

The caller supplies an `OrderRequest`, a current `ExchangeFilters` snapshot, and an implementation
of the async `OrderTransport` protocol. `OrderLifecycle.submit()` validates tick size, lot size,
quantity, and min/max notional using finite positive `Decimal` values. It then writes an `INTENDED`
row to the application's SQLAlchemy database, atomically claims it as `SUBMITTING`, and only then
calls the transport. Intent IDs and exchange client-order IDs are independently unique. Repeating
the same intent returns its stored record and never sends it again.

SQLite writers use `BEGIN IMMEDIATE`; PostgreSQL writers take one transaction-scoped advisory lock
before taking intent `FOR UPDATE` locks. The global writer lock is required because two new SELL
intents do not yet have rows that a per-intent lock could serialize.
No database transaction remains open during an awaited transport call. A timeout or server-side
ambiguous error becomes `UNKNOWN`. On startup, an interrupted `SUBMITTING` row also becomes
`UNKNOWN`. Neither path retries submission: `OrderLifecycle.reconcile()` queries the exchange by
the durable client-order ID and applies that response.

The lifecycle states are `INTENDED`, `SUBMITTING`, `UNKNOWN`, `NEW`, `PARTIALLY_FILLED`, `FILLED`,
`CANCELED`, and `REJECTED`. Confirmed fills are deduplicated by exchange trade ID and store only
finite `Decimal` base quantity, quote quantity, quote-valued commission and base commission. Reused trade IDs with conflicting values, mismatched order IDs, impossible cumulative
quantities, incomplete `FILLED` events, and third-asset fees are rejected transactionally. Base fees are valued at the fill's effective
quote/base price and deducted from scoped inventory once; original gross fills remain unchanged. Stale or
out-of-order updates cannot reduce confirmed totals. A fill received after `CANCELED` is still
accounted; the state remains canceled unless confirmed fills reach the full intended quantity.

Inventory is isolated by the pair `(inventory_scope, symbol)`. Only confirmed BUY fills minus base-asset commission create
inventory. A SELL intent reserves its full unfilled amount, and creation fails if confirmed
inventory minus existing SELL fills and reservations is insufficient. A canceled order continues
to reserve any exchange-reported cumulative quantity whose trade details have not arrived yet.
Exchange fill application also checks that a SELL can never take the scoped symbol below zero.

## Adapter contract

An adapter must implement:

```python
class OrderTransport(Protocol):
    async def submit(self, order: OrderRequest) -> ExchangeUpdate: ...
    async def query(self, client_order_id: str) -> ExchangeUpdate: ...
    async def cancel(self, client_order_id: str) -> ExchangeUpdate: ...
```

Adapters must classify timeouts and exchange 5xx responses as `RetryableTransportError` (the name
describes transport retry semantics, not permission to resubmit the order). The core records these
as `UNKNOWN`; recovery must use `query`. `deterministic_client_order_id()` is available when a venue
requires a compact stable client ID.

## Remaining live blockers

There is no evidence of testnet execution in this repository, and none is claimed. Before any live
use, the isolated adapter still needs complete applicable-filter validation, automatic entry/protection
orchestration, startup reconciliation across all open orders and trades, and authenticated testnet
evidence for disconnects and exchange maintenance. Production use also requires explicit capital/risk
authorization, independent review, monitoring and emergency controls. The required 30-day forward
observation cannot be completed within this implementation task and remains an external time-based
blocker.
