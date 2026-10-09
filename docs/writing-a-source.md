# Writing a source

A *source* is one market-data provider behind the contract in
`tradedesk_marketdata/source.py`. Everything that is specific to the provider
lives in its own module under `tradedesk_marketdata/sources/`:
- what one fetch unit is;
- how a unit is fetched (URLs, tokens, pauses, rate limits);
- how it is decoded (file format, timestamp convention, price units);
- its known defects;
- when a day it fed may be committed.

Everything every source shares is the framework's, in
`tradedesk_marketdata/export.py`. That covers:
- the day-file cache and its layout;
- tick to 1-minute candles, split per UTC day;
- the scale sentry and the atomic commit;
- the `_sources.jsonl` and `_partial_days.jsonl` manifests;
- the range CSVs;
- the fetch pool, progress and cancellation.

The framework never imports a source. The CLI looks one up by name in the
registry (`tradedesk_marketdata/sources/__init__.py`) and hands the configured
object to the framework. `cli.py` builds `--source`, the source's options and
their help from the registry, so it names no provider.

## The contract

A source is a `Source` subclass:

| Member | What it is |
|---|---|
| `name` | the `--source` value, and the `source` of every provenance record |
| `summary` | one line for `--help`: what the source is and what it covers |
| `unit` | what one fetch unit is ("hour", "month file"), for the `--retries` help |
| `partial_commit_rule` | what `--commit-partial-after-days` means for this source |
| `options` | `SourceOption`s: the flags only this source understands. The CLI adds them under the source's name in `--help` and refuses them with any other source. A flag declared by two sources is refused at registration |
| `__init__(options)` | receives `{dest: value}` for its options (`None` when not given; apply your own default). Raise `SourceError` for unusable configuration; the CLI reports it as a usage error |
| `check_symbol(symbol)` | raise `SourceError` for a symbol you cannot serve. It is called before any request |
| `describe(symbol)` | a `SourceDescription(price_divisor, params)` for the `.meta.json` sidecar, where cache price = source price / `price_divisor` |
| `open(ctx)` | starts one symbol's export and returns a `SourceRun`. `ctx` is a `RunContext`: symbol, range, cache directory, `commit_partial_after_days`, timeout, retries, `keep_raw` |
| `action(request)` | optional: a command that replaces the export (Dukascopy's `--probe`). Return an exit status, or `None` to export |

The framework drives the `SourceRun` for one symbol:

1. **`plan(pending_days)`** returns the units to fetch for the days that are
   not yet committed, in processing order. Each unit has a `days` attribute:
   the pending UTC days it holds ticks for, or is needed by. A day is decided
   once every unit that names it has been decoded.
2. **`fetch(unit)`** returns the raw unit, in whatever form `decode` expects.
   It runs on a worker thread when `fetch_workers` is above one, so keep it
   free of shared state. It must not raise for a unit that is merely
   unavailable; return something `decode` recognises instead. Honour
   `ctx.timeout` and `ctx.retries`, and call `cancel.check_cancelled()` before
   each request (use `cancellation.wait(delay)` for pauses and backoff).
3. **`decode(unit, raw)`** runs in plan order on the main thread. It returns
   the unit's ticks as a `DataFrame` indexed by UTC time, in time order, with
   columns `bid`, `ask`, `bid_vol` and `ask_vol` in the cache's raw price
   units, or `None` when the unit has no ticks. `ticks_frame([Tick, ...])`
   builds one. This is where your timestamp convention and your scale belong.
   Use it to record each unit's status (data, gap, unavailable) for `decide`.
4. **`decide(day, has_data)`** returns one of:
   - `Commit()`: write the day now;
   - `Commit(Partial(missing_hours, gap_reason, note))`: write the day now and
     record its known-permanent gap in `_partial_days.jsonl`;
   - `Leave(reason)`: not in this run; a later run tries again;
   - `Wait(reason)`: ask again after the next unit. If the units run out
     first, the day is treated as `Leave(reason)`;
   - `Exclude(reason, source_unit)`: this source's data for the day is known
     to be wrong. No day files are written, and an excluded record is
     appended to `_sources.jsonl`. Override `excludes(day)` to answer from the
     same configuration, so a recorded exclusion is not fetched again while
     it stands.
5. **`committed(day, empty)`** is called after the framework wrote the day. It
   returns `Provenance(scale_factor, source_unit)` for `_sources.jsonl`. Here
   the source disposes of raw units no longer needed, with
   `ctx.retire_raw(path, relpath)`: that deletes them, or with `--keep-raw`
   moves them to `{cache}/{SYMBOL}/_raw/{source}/{relpath}`. Before
   requesting a unit, look for a kept copy with `ctx.kept_raw(relpath)`.
6. **`finish()`** is called once every unit is processed. Log your unit-level
   summary and clean up staging.

Day-level counting, the commit itself, the scale sentry and both manifests are
the framework's. Do not write day files or manifests from a source.

## Registering it

Add the class to the registry in `tradedesk_marketdata/sources/__init__.py`:

```python
from .mysource import MySource

register(MySource)
```

`register` refuses a name or an option flag another source already holds.

## Tests

- **Conformance.** Add a feed for your source to `tests/source_feeds.py`: a
  function that serves the shared synthetic ticks in your provider's own wire
  format, stubbing only your network call, and returns a `Feed` with the
  options, CLI arguments and scale factor your source needs.
  `tests/test_source_conformance.py` runs every registered source through the
  framework on those ticks. It asserts byte-identical day files and range
  CSVs (equal to golden digests), provenance records of one shape, sidecars,
  and `--keep-raw`. A registered source without a feed fails the suite.
- **Provider behaviour.** Your timestamp convention, units, gap rules and
  known defects are tested in your own `tests/test_<source>_*.py` modules,
  and nowhere else.
- **The boundary.** `tests/test_source_boundary.py` checks that no framework
  module names or imports a source.
- **No test touches the network.**

## Documentation

Give the source its own subsection under "Sources" in the README. That
subsection is the one place its quirks are documented. Anything every source
does belongs in "What every source shares".
