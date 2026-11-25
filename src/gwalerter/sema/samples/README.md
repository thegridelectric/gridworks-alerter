# Samples

Canonical JSON instances, one per seeded **type** version that carries
an `examples:` block. Generated from the authored examples (never edited
by hand) and consumed by `roundtrip.py`. A type version without a sample
is silently untested by the round-trip, so its absence is recorded here.

Coverage: **39 of 43** seeded type versions have a sample.

Seeded type versions lacking a sample (no `examples:`):

- `fsm.full.report.001`
- `glitch.000`
- `gw.opsgenie.alert.000`
- `machine.states.000`
