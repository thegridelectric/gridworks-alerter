# gridworks-alerter

The GridWorks house alerter (gwalerter): a gwbase actor on the fleet's
RabbitMQ broker that watches what the house scadas send and raises
alerts. It is the successor to gwalert (`gridworks-alerts`), which polls
the journal database; the two run side by side while detectors move
over one at a time.

**Inputs ride the broker.** The actor consumes the audit exchange like
JournalKeeper does and keeps the message types it tracks:

- `report.event` — each scada's periodic readings.
- `layout.lite` — the scada's hardware layout, sent on boot; the source
  of every channel's unit and role.

**State lives in sqlite**, in the service's XDG data dir: the latest
layout per house, a rolling window of readings, and when each house was
last heard from. Across a restart the layouts survive; the readings
window refills from live traffic.

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
