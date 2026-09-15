# gridworks-alerter

The GridWorks house alerter (gwalerter): a gwbase actor on the fleet's
RabbitMQ broker that watches what the house scadas send and raises
alerts. It is the successor to gwalert (`gridworks-alerts`), which polls
the journal database; the two run side by side while detectors move
over one at a time.

**Inputs ride the broker.** The actor consumes the audit exchange like
JournalKeeper does and keeps the message types it tracks. It is an
Orchestrator-tier gwbase actor of transport class `Alerter`, so its alert
words broadcast on its own `alertsmic_tx` exchange, which fans into the
ear exchange for the manager and JournalKeeper. Tracked types:

- `report.event` — each scada's periodic readings.
- `layout.lite` — the scada's hardware layout, sent on boot; the source
  of every channel's unit and role.
- `g.node.forest` — the registry's topology broadcasts, projected to a
  local `g_nodes` table by immutable id. At boot the alerter also asks
  the registry's HTTP read (`GWALERTER_GNR_URL`) for the forest under
  its fleet roots.

**Which houses it pages for is a registry subtree.** `GWALERTER_FLEET_ROOTS`
(comma-separated root aliases, no default) names the fleet; every Active
TerminalAsset under those roots is tracked and nothing else is. A house
that must not page lives outside the roots. A scada heard from that
reports for no tracked house is logged once per boot.

**State lives in sqlite**, in the service's XDG data dir, behind
SQLAlchemy with the schema applied by the alembic chain under
`src/gwalerter/migrations/` when the store opens (never `create_all`):
the registry projection, the latest layout per scada, a rolling window of
readings, and alert state. Across a restart the projection, layouts and
alert state survive; the readings window refills from live traffic. To
change the schema: edit `db_models.py`, then
`uv run alembic revision --autogenerate -m "..."` and review the file.

Message types are governed by **Sema** — the versioned vocabulary of
JSON-Schema contracts for all GridWorks message boundaries, canonical at
[`thegridelectric/sema`](https://github.com/thegridelectric/sema). This
repo carries a vendored snapshot at `src/gwalerter/sema` — generated,
never hand-edit; regenerate with `scripts/regen_sema_snapshot.sh` from
the seed `src/gwalerter/sema_seed_request.yaml`.

## Quick start (dev)

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a running
RabbitMQ broker with the GridWorks fabric (for solo work, start the dev
broker from the sibling `gridworks-base` repo: `./arm.sh` or `./x86.sh`).

```sh
uv sync                       # install deps
cp template.env .env          # then fill in the values
uv run gwalerter rabbit       # run the actor
```

## Tests

`./ci.sh` runs what CI runs: ruff, pyright, pytest. Tests marked
`broker` need the dev broker on `localhost:5672` and self-skip without it.

## Deploy

One login user (`alerter`), the repo cloned in its home directory at a
pushed SHA, `.env` at the repo root, `uv sync --frozen`, and the unit in
`service/` copied to `/etc/systemd/system/`. Logs go to
`~/.local/state/gridworks/alerter/log/<alias>.log`.
