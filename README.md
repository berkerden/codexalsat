# SpotLab

SpotLab is a local-first research, signal, and controlled spot-trading application for one crypto pair at a time. It uses a Python/FastAPI backend, a React/TypeScript interface, PostgreSQL for operational records, and Parquet for historical market data.

The application starts in **recommendations-only** mode. Live order submission is disabled by default and will remain unavailable until later risk controls, reconciliation, and an explicit scoped activation flow are implemented. No API key, account balance, personal trade record, or downloaded market archive belongs in Git.

## Status

This is the A0 foundation. It contains development configuration, container orchestration, CI, and a secret scan. The trading, research, data, and interface implementations are being added in later stages. See `SPEC.md` and `STATUS.md` when they are introduced for the current design and work list.

## Local development

1. Copy the safe template: `cp .env.example .env`.
2. Leave `LIVE_TRADING_ENABLED=false`. Do not put Binance credentials in the frontend or commit `.env`.
3. Start PostgreSQL: `docker compose up -d db`.
4. When the backend and frontend packages are present, start the development stack with `docker compose up --build`.

The Compose services bind only to `127.0.0.1`. A local bot stops when its host sleeps or shuts down; continuous operation requires a separately designed and explicitly approved deployment.

## Checks

Run the local checks after installing the relevant Python and Node dependencies:

```sh
python3 -m pip install ".[dev]"
./scripts/check.sh
```

The script runs the secret scan, Python lint/type/test checks when the backend exists, and frontend lint/type/build checks when the frontend exists. CI runs the complete project checks and never needs live credentials.

## Security baseline

- Keep exchange withdrawal permission disabled; later API credentials must be backend-only and least-privileged.
- `LIVE_TRADING_ENABLED` must stay `false` outside an explicit, audited activation workflow.
- Docker publishes services only on loopback by default.
- The pre-commit hook and CI reject committed secrets and non-template `.env` files.

If a credential is exposed, revoke or rotate it immediately. Removing it from the working tree is insufficient; then assess Git history and any remote copies before performing a controlled cleanup.
