# gridworks-alerter

The alerter is the production system's alert generator: the primary place
that decides a condition anywhere in the fleet or the platform needs a
person, and says so. It listens to everything on the production rabbit
broker, every message every service and every house sends, the moment
it is sent, the same traffic the journal records. It keeps what it
needs in a store of its own (what exists, the latest state of each
thing, when each was last heard from, a few hours of readings, which
alerts are open), so it reads nobody else's database and no analysis
query can slow it down or raise an alert by mistake. Its kinds are
about houses and the platform, and they grow with the platform.

A set of rules runs over that store every few seconds. Each rule is one
alert kind, named for the condition a person acts on: no data from a
house, a zone at freeze risk, a heat pump commanded on and drawing no
power. When a condition starts to hold the alerter raises one alert;
when it stops, it resolves that same alert. It never repeats itself.
Which houses it watches is the registry's business: every house under
the fleet's roots that is pending or active. A house in service that
should not page right now, during a heat pump install or a visit, is
silenced by name; its alerts are still recorded, just not paged.

It runs as three services on its own box, apart from the journal, so a
journaling problem and an alerting problem are never one outage: the
listener, which hears the broker, keeps the store and runs the rules;
the tap, the only one that talks to the alert manager, which reads the
store and nothing else; and the prober, which checks the broker's doors
from outside, because the alerter cannot hear a broker outage on the
broker. The tap needs no broker, so "broker down" still reaches a
person. The box, who may log in and how it is operated are in
`gridworks-infra/alerts/instance-README.md`.

The alerter raises and resolves. It does not decide who is woken: that
is an alert manager's job, who is on call, how often an open alert
re-notifies, how it escalates, when a person acknowledges it. The
alert manager is Opsgenie. The alerter creates an alert there when a
condition starts, closes it when the condition ends, and the person on
call acknowledges in between. Every alert record is also published on
the broker, so the journal keeps it and the alerts page can show it.

## The kinds it raises

**`ScadaSilent`, `LtnSilent`, `NoData`** (house): three kinds for one
condition, no readings arriving from a tracked house's scada for longer
than the silence threshold (`GWALERTER_NO_DATA_SILENCE_S`, 600 s). The
readings come in report.events, and a scada stops sending report.events
(but keeps sending snapshots) when it is not communicating with its
LTN. The kind says which party has gone quiet: `ScadaSilent` when
nothing has been heard from the scada; `LtnSilent` when the scada is
speaking and its LTN is not, so the reports are safe on the scada's
disk; `NoData` when both are speaking and no reports arrive, a fault
between two live parties. Last-heard is floored at the alerter's own
boot, so a fresh alerter gives every house one threshold to speak
before it pages. An open alert whose cause moves keeps its id and
changes kind. A report back from the scada resolves the alert on the
next tick, with that report's readings as the evidence; a layout is
stored for its meaning and is no sign of life.

**`BrokerUnreachable`** (platform): a door of the fleet's broker has
failed the prober's check three probes running
(`GWALERTER_PROBE_FAILURES_TO_RAISE`), one probe a minute
(`GWALERTER_PROBE_INTERVAL_S`). The AMQPS door is checked with a round
trip (connect, declare an exclusive queue, publish one message to it,
receive it back); the MQTT door the scadas use with a TLS connect that
must get an accepted CONNACK, since what refuses the prober at the gate
refuses every scada. One alert per door, the door as the alert's
`Subject`; the first success resolves it. While the fleet is being
heard the probe's own failures do not count: the fault is then the
prober's login, cert or network, and the summary says so. While a
`BrokerUnreachable` alert is open no silence alert is raised, and when it
resolves every house gets one silence threshold to reconnect.

## How it is put together

**The store is one sqlite file**, shared by the three processes: the
registry projection, the latest layout per scada, the latest arrival
from every alias under the fleet roots, a rolling window of readings,
the silenced houses and every alert's `Firing` and `Resolved` record.
Every transition is written here before it is broadcast or paged, and a
restart of any process neither re-raises an open alert nor forgets one.
The alerter keeps its own file rather than reading the journal database
so that a heavy query elsewhere can neither slow a page nor fake one.

**The listener** (`gwalerter rabbit`) is an Orchestrator-tier gwbase
actor of transport class `Alerter`. It taps the audit exchange and keeps
three message types, each decoded as the sema word it is at the version
it was sent: `report.event` (each scada's readings), `layout.lite` (the
scada's hardware layout) and `g.node.forest` (the registry's topology
broadcasts, projected by immutable id; at boot it also asks the
registry's HTTP read, `GWALERTER_GNR_URL`, for the forest under its
roots). A body that does not decode is logged and dropped. Its alert
words broadcast on its own `alertsmic_tx` exchange, keyed by the house
the alert is about.

**Which houses** is a registry subtree: `GWALERTER_FLEET_ROOTS`
(comma-separated root aliases, no default) names the fleet, and every
Pending or Active terminal asset under those roots is tracked. A scada
heard from with no tracked house behind it is logged once per boot.
**Silences** are one `gw.hack.alert.silences` file at
`GWALERTER_SILENCES_PATH`, read at boot and on every tick and broadcast
whenever it changes: a missing file silences nothing, an unreadable one
leaves the last list standing. A silenced house's alerts are recorded
and broadcast but not paged.

**The tap** (`gwalerter tap`) reads the store's open alerts every few
seconds, lists what Opsgenie holds open from this alerter, and
reconciles the two. A house alert's Opsgenie headline is
`[house] [Kind] summary`, the house by its short name. It creates an
alert for each open one about an unsilenced house that Opsgenie does
not hold, closes the one Opsgenie holds for a house since silenced, and
closes each one Opsgenie holds that the store does not hold open. The
alerter owns open and closed; a person acknowledges and never has to
close, and a close made by hand while the condition holds is undone on
the next pass. When the listing
cannot be read the tap works from what its own creates and closes left,
and Opsgenie deduplicates on the `AlertId`. Who is paged, how often an
open alert re-notifies and how it escalates are Opsgenie's policy.

**Message types are governed by Sema**, the versioned vocabulary of
JSON-Schema contracts for every GridWorks message boundary, canonical at
[`thegridelectric/sema`](https://github.com/thegridelectric/sema). This
repo has a vendored snapshot at `src/gwalerter/sema`: generated, never
hand-edited; regenerate with `scripts/regen_sema_snapshot.sh` from the
seed `src/gwalerter/sema_seed_request.yaml`. The seed takes published
words only.

## Quick start (dev)

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a running
RabbitMQ broker with the GridWorks fabric (for solo work, start the dev
broker from the sibling `gridworks-base` repo: `./arm.sh` or `./x86.sh`).

```sh
uv sync                       # install deps
cp template.env .env          # then fill in the values
uv run gwalerter rabbit       # the listener; `tap` and `probe` likewise
```

The store's schema is applied by the alembic chain under
`src/gwalerter/migrations/` when the store opens. To change it: edit
`db_models.py`, `uv run alembic revision --autogenerate -m "..."`, and
review the file.

## Tests

`./ci.sh` runs what CI runs: ruff, pyright, pytest. Tests marked
`broker` need the dev broker on `localhost:5672` and self-skip without it.

## Deploy

One login user (`alerter`), the repo cloned in its home directory at a
pushed SHA, `.env` at the repo root, `uv sync --frozen`, and the three
units in `service/` (`alerter-rabbit`, `alerter-tap`, `alerter-probe`)
copied to `/etc/systemd/system/`. Logs go to
`~/.local/state/gridworks/alerter/log/<alias>.log`. The box itself is
described in `gridworks-infra/alerts/instance-README.md`.
