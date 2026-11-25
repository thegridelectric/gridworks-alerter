# Sema vocabulary snapshot (GENERATED)

This directory is a **vendored Sema snapshot**: a self-contained, generated
subset of the Sema vocabulary. **Never hand-edit it.** To change it, edit the
seed (`../sema_seed_request.yaml`) or the definitions in the sema repo, then
re-run `uv run sema snapshot regen <seed>` in the sema repo. `check_staging.py`
here is the consumer's CI check, run as `python -P check_staging.py <seed>`
(`-P` keeps this directory's `types/` package from shadowing the standard
library): it fails when this snapshot holds a staging word the seed does not
allow, or when the seed changed since the build. With `--deploy-branch <name>`
it also fails on that branch whenever this snapshot holds any staging word.

## What Sema is

Sema is a vocabulary registry for structured messages exchanged between
independent systems. It defines versioned types, enums, and formats
expressed as JSON Schema; these act as boundary contracts, making the
structure and semantics of serialized messages explicit and mechanically
verifiable. Sema applies only at system boundaries — it governs the JSON
exchanged between applications, not runtime architecture, database design,
or internal object models. Every schema `$id` lives under the
`https://schemas.electricity.works` namespace; the canonical registry and
spec live in the sema repo (https://github.com/thegridelectric/sema — start
at `spec/primary.md`).

## Working with Sema-typed data (rules that fight default idiom)

- Construct and decode instances through the generated classes in this
  snapshot — never hand-built dicts.
- Dict/JSON keys are the PascalCase wire form; produce and consume them via
  `to_dict()` / `from_dict()`, never by spelling keys inline.
- Dispatch on decoded messages with `isinstance`, never `hasattr`.
- Narrow every codec decode (`expect=` or `assert isinstance`).
- Where a value is vocabulary-shaped, use its property format type, never
  bare `str`.
