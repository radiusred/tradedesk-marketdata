
![banner](https://i.ibb.co/Kc5C88gp/tradedesk-banner.webp)

# tradedesk-marketdata

![CI Build](https://github.com/radiusred/tradedesk-marketdata/actions/workflows/ci.yml/badge.svg)
[![PyPI Version](https://img.shields.io/pypi/v/tradedesk-marketdata?label=PyPI)](https://pypi.python.org/pypi/tradedesk-marketdata)

**Market data downloader and candle exporter for use in backtesting your trading strategies.**

This tool downloads raw tick data, converts it into clean, deterministic CSV
candle files, and writes a metadata sidecar describing exactly how the data was
produced. Two tick sources are supported, Dukascopy's public datafeed
(`--source dukascopy`) and HistData.com's monthly tick files
(`--source histdata`); `--source` is required. Both write the same cache, so
the candle files look the same whichever source produced a day (see
[Sources](#sources)).

It is designed to be run once per dataset, not repeatedly during backtests.

> **Renamed from `tradedesk-dukascopy` in 2.0.** The package is now
> `tradedesk_marketdata` and the commands are `tradedesk-md-*`; the old
> `tradedesk-dc-*` command names still work as aliases for this major version.

![loop](https://i.ibb.co/BSz1JSH/tradedesk-dukascopy.gif)

---

## Quick start

Install:

```bash
pip install tradedesk-marketdata
```

Export 5-minute candles for EURUSD:

```bash
tradedesk-md-export --source dukascopy --symbols EURUSD \
  --from 2025-01-01 --to 2025-01-31 \
  --resample 5min \
  --out data \
  --cache-dir ./cache \
  --price-divisor 1000 \
  --workers 1
```

This produces:

```text
data/
  EURUSD_5MIN_bid.csv
  EURUSD_5MIN_bid.csv.meta.json
  EURUSD_5MIN_ask.csv
  EURUSD_5MIN_ask.csv.meta.json
```

You can now point your backtest engine at the bid or ask CSV directly, depending
on which price side you want to replay.

---

## Sources

`--source` picks where the ticks come from. It is required: there is no
default, and leaving it out is a usage error that lists the available sources.
Each source has its own options, listed under its name in `--help`, and they
are refused with any other source.

| | Dukascopy (`--source dukascopy`) | HistData.com (`--source histdata`) |
|---|---|---|
| Unit fetched | one `.bi5` file per instrument-hour | one zip per instrument-month |
| Coverage | varies per instrument | FX majors from 2000, GBPJPY 2002, XAUUSD 2009, the indices from 2010-11 (its symbol map) |
| Prices | bid and ask per tick, with tick volume | bid and ask per tick; **no volume** (candle `volume` is `0.0`) |
| Timestamps | UTC | EST without daylight saving (fixed UTC-5), converted to UTC on decode |
| Scaling | `--price-divisor` | per-symbol scale in the symbol map (`--symbol-map`) |
| Speed and limits | heavily rate-limited; years of history take a very long time | ~190 requests for an index's whole history; sequential, one month at a time |

The first subsection below is what every source shares. Each one after it is
the only place a provider's quirks are documented. To add a provider, see
[Writing a source](docs/writing-a-source.md).

### What every source shares

Every source hands the same framework ticks in UTC, in the cache's raw price
units, and the framework does the rest. A day exported from either source is
therefore stored identically:

- **Day files.** One 1-minute candle file per UTC day and price side, under
  the cache's symbol names:
  `{cache}/{SYMBOL}/{YYYY}/{MM0}/{DD}_bid.csv.zst` and `_ask.csv.zst`, where
  `MM0` is the zero-based month (January is `00`). Each file holds
  `timestamp,open,high,low,close,volume`, is compressed with zstd level 3,
  and is written atomically.
- **First committed wins.** A day that already has both candle files is never
  fetched or rewritten, by any source. To replace a day, delete its two files
  and re-export it.
- **When a day is committed** is the source's decision (its gap rules, below).
  A day with known-permanent gaps is committed as a *partial day* and recorded
  in `{cache}/{SYMBOL}/_partial_days.jsonl` as
  `{day, missing_hours, gap_reason, committed_at}`.
- **Scale sentry.** The exporter refuses to commit a day whose median close
  diverges by more than 3× from the medians of its neighbours already on disk.
  That is the signature of one cache written at two different scales. The
  day's raw files are kept, so it can be retried with the right scale.
- **Range CSVs and sidecars** (`--resample` and `--out`, below) are the same
  whichever source produced the days.

#### Provenance (`_sources.jsonl`)

Every committed day is recorded in the symbol's append-only
`{cache}/{SYMBOL}/_sources.jsonl`:

```json
{"day": "2015-01-02", "source": "histdata", "scale_factor": 1.0, "committed_at": "2026-10-08T22:35:49.508169+00:00", "source_unit": "HISTDATA_COM_ASCII_SPXUSD_T201501.zip"}
{"day": "2020-01-02", "source": "dukascopy", "scale_factor": 0.1, "committed_at": "...", "source_unit": "EURUSD/2020/00/02/00h-23h_ticks.bi5"}
```

`scale_factor` is the multiplier from the source's price to the cache's units
(1/`--price-divisor` for Dukascopy's int32 ticks); `source_unit` names the month
zip(s) or the hour set the day was built from. Days committed before this
manifest existed have no record and were written by Dukascopy.

A source can also *exclude* a day whose data it knows to be wrong. It then
writes no day files and appends a record with `"status": "excluded"` instead
(`{"day", "source", "status", "reason", "decided_at", "source_unit"}`); a
record without `status` is a commit. Another source can still fill the
excluded day. The same source does not fetch it again while it still excludes
it, and fetches it like any other missing day once the exclusion is removed
from its configuration. The end-of-run summary counts `excluded=` (this run)
and `already_excluded=` (recorded earlier).

#### Keeping the raw units (`--keep-raw`)

By default a source deletes its fetched raw units (Dukascopy's hourly `.bi5`,
HistData's month zips) once every day they feed is committed. With
`--keep-raw` they are moved instead to `{cache}/{SYMBOL}/_raw/{source}/`
(`_raw/dukascopy/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5`, `_raw/histdata/{YYYY}{MM}.zip`),
and both sources read a unit from there before making any request for it. When a
source's decode is fixed, delete the affected day files and re-run the export:
the days are rebuilt from the kept units, without downloading years of history
again. The tool deletes nothing under `_raw/`, except a kept unit that no
longer decodes, which is fetched again.

### Dukascopy (`--source dukascopy`)

Dukascopy's public datafeed serves one LZMA-compressed `.bi5` tick file per
instrument and UTC hour:

```text
https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5
```

Each hour is staged as `{cache}/{SYMBOL}/{YYYY}/{MM0}/{DD}/{HH}h_ticks.bi5`
until its day is committed. A 404 is an hour with no data. A 200 with an
empty (or tiny) body is a market-closed hour. A timeout, 429 or 5xx after
every retry is *unavailable*: the hour may exist, so its day is not committed
in this run.

Dukascopy-only options: `--price-divisor`, `--probe`, `--probe-ticks`.

#### Price scaling (`--price-divisor`)

Dukascopy encodes tick prices as float32 for some instruments and as int32
points for others. The format is detected from the first hour with data.
int32 prices are divided by `--price-divisor` once, at export time; float32
prices are stored as they are.

Examples:

| Instrument | Typical divisor |
|----------|-----------------|
| EURUSD   | `1000` |
| USDJPY  | `100000` |
| Indices | `1` or `10` |

If unsure, use probe mode:

```bash
tradedesk-md-export --source dukascopy --symbols GBPSEK \
  --from 2025-07-01 --to 2025-07-01 \
  --probe
```

Probe mode prints sample ticks at different divisors without writing files.
Use `--probe-ticks N` to control how many ticks are printed (default `10`).

```text
GBPSEK: detected tick price format = int
GBPSEK @ 2025-07-01T00:00:00+00:00 (int): first 10 ticks
first tick raw: 2025-07-01T00:00:00.326000+00:00 bid_i 1297675 ask_i 1298619 vol 1.149999976158142
  divisor      1: bid 1297675.000000 ask 1298619.000000
  divisor     10: bid 129767.500000 ask 129861.900000
  divisor    100: bid 12976.750000 ask 12986.190000
  divisor   1000: bid 1297.675000 ask 1298.619000
  divisor  10000: bid 129.767500 ask 129.861900
  divisor 100000: bid 12.976750 ask 12.986190
using --price-divisor 1.0:
2025-07-01T00:00:00.326000+00:00 bid 1297675.0 ask 1298619.0 bid_vol 1.149999976158142
2025-07-01T00:00:01.128000+00:00 bid 1297800.0 ask 1298661.0 bid_vol 0.9200000166893005
2025-07-01T00:00:01.329000+00:00 bid 1297796.0 ask 1298621.0 bid_vol 0.9200000166893005
2025-07-01T00:00:03.335000+00:00 bid 1297796.0 ask 1298591.0 bid_vol 0.9200000166893005
2025-07-01T00:00:03.737000+00:00 bid 1297842.0 ask 1298695.0 bid_vol 1.149999976158142
2025-07-01T00:00:05.340000+00:00 bid 1297850.0 ask 1298655.0 bid_vol 0.9200000166893005
2025-07-01T00:00:06.542000+00:00 bid 1297862.0 ask 1298709.0 bid_vol 0.9200000166893005
2025-07-01T00:00:08.546000+00:00 bid 1297874.0 ask 1298709.0 bid_vol 0.9200000166893005
2025-07-01T00:00:10.556000+00:00 bid 1297877.0 ask 1298724.0 bid_vol 0.9200000166893005
2025-07-01T00:00:12.562000+00:00 bid 1297839.0 ask 1298684.0 bid_vol 1.149999976158142
```

A day refused by the scale sentry keeps its `.bi5` files. Re-run it with the
`--price-divisor` the rest of the cache was written with.

#### Concurrency, timeouts and rate limits

Each symbol export uses two downloader threads. `--workers` (default `4`)
controls how many symbols are exported concurrently, so the total request
concurrency can grow quickly. The datafeed becomes unreliable when too many
requests are in flight; keep `--workers 1` to stay at two concurrent requests.

Under load the datafeed can take 20 s or more to deliver a single hour, so a
run that logs mostly timeouts is usually giving up on answers that were on
their way. Raising `--read-timeout` (and keeping `--workers 1`) recovers them,
and costs nothing when the datafeed is quick. Rate limiting (HTTP 429) is per
client address: more concurrency makes it worse, longer timeouts do not.

```bash
tradedesk-md-export --source dukascopy --symbols USA500IDXUSD \
  --from 2010-01-01 --to 2019-12-31 \
  --cache-dir ~/marketdata --price-divisor 1 \
  --workers 1 --connect-timeout 15 --read-timeout 90 --retries 5
```

Re-running the same command is idempotent, and it is the intended way to fill
gaps that failed hours left in the cache.

#### Committing days with permanent gaps (`--commit-partial-after-days`)

Some historical hours never return tick data: they 404, or hand back a
payload the decoder cannot parse, however many times the export is re-run.
Without intervention those days stay uncommitted forever. Their candles are
never written and their `.bi5` files stay staged.

`--commit-partial-after-days N` lets the exporter commit such a day from the
hours that *did* decode once the day is older than `N` days (default `7`),
recorded in `_partial_days.jsonl` with `gap_reason` `missing_404` and/or
`decode_failed`. Younger gap days are left with their `.bi5` in place for a
retry. `--commit-partial-after-days 0` commits at once, which is useful for
one-off backfill sweeps.

An hour that merely could not be fetched *this run* is not a gap. Its day is
never committed, whole or partial, whatever its age. The hours that did
download keep their `.bi5`, the summary reports
`unavailable=N … days_left_for_retry=M`, and re-running fills them in.

```bash
tradedesk-md-export --source dukascopy --symbols LIGHTCMDUSD \
  --from 2022-01-01 --to 2022-12-31 \
  --out data --cache-dir ./cache \
  --price-divisor 1000 --workers 1 \
  --commit-partial-after-days 7
```

Days refused by the scale sentry are **never** partial-committed, whatever
their age.

#### Staging self-heal

Before its all-cached early exit, a re-export cleans up `.bi5` day
directories left behind in three cases:

1. **Empty day-dirs:** a directory left behind with no staged files.
2. **Already-committed day-dirs:** a run was interrupted after writing a day's
   candle files but before deleting its `.bi5`. The redundant directory is
   removed (with `--keep-raw`, its files are retained under `_raw/`), and the
   candle files are left byte-for-byte intact.
3. **All-empty 0-byte `.bi5` day-dirs:** a market-closed day where every
   fetched hour returned no ticks, so no candle file is ever written but the
   staging directory lingers. The empty files carry no data, so the directory
   is removed once the day is older than `--commit-partial-after-days`.
   Waiting that long leaves a same-day, in-flight export alone.

### HistData.com (`--source histdata`)

[HistData.com](https://www.histdata.com/) publishes free "Generic ASCII" tick
files, one zip per instrument and month, each tick a
`YYYYMMDD HHMMSSNNN,bid,ask,volume` line in EST without daylight-saving
changes. It is a free service run for traders' own research and backtesting,
not a licensed commercial feed: use it for personal backtesting, do not
redistribute its files, and check its site for its current terms. The
exporter treats it accordingly: requests for one instrument are strictly
sequential, one month zip per request with a pause between months, under a
descriptive User-Agent; keep `--workers 1` when exporting several symbols so
the whole run stays one request at a time. Each download loads the month's page
for a fresh download token, then posts the page's download form, honouring
`--connect-timeout`, `--read-timeout` and `--retries` per month.

```bash
tradedesk-md-export --source histdata --symbols USA500IDXUSD \
  --from 2010-11-01 --to 2019-12-31 \
  --cache-dir ./cache --workers 1
```

HistData-only option: `--symbol-map`.

#### The symbol map (`--symbol-map`)

Which cache symbols HistData serves, under which code, at which scale and from
which month is configuration, not code. The package ships
`tradedesk_marketdata/sources/histdata.example.toml`, and uses it when
`--symbol-map` is not given. To change it, copy it, edit the copy and pass
`--symbol-map your-copy.toml`. Each entry looks like this:

```toml
[symbols.USA500IDXUSD]           # the cache symbol the day files are written under
name = "SPXUSD"                  # HistData's instrument code
scale = 1                        # cache raw price = HistData decimal price x scale
first_month = "2010-11"          # earliest month HistData serves
scale_verified = true            # the scale was confirmed against another source
scale_evidence = "Dukascopy median close ~4782"
exclude = [                      # optional: days never to commit from HistData
  { from = 2020-06-17, to = 2020-06-19, reason = "the series carries another index" },
]
```

A symbol that is not in the map is refused before anything is downloaded, and
a file that does not follow this schema is a usage error.

- **Scales.** The cache stores the raw units of the Dukascopy datafeed, so
  HistData's decimal prices are multiplied by the symbol's scale on decode:
  EURUSD `1.10366` is stored as `11036.6`, GBPJPY `179.601` as `17960.1`,
  XAUUSD `$2063.625` as `206362.5`, and indices stay in points. The scale is
  recorded with every day and in the sidecar (`price_divisor` = 1/scale,
  `params.scale_factor` = scale).
- **Verification.** An export of a symbol whose `scale_verified` is false
  logs a warning. Check a few committed days against another source before
  trusting them.
- **Exclusion spans.** A day inside a span gets no day files and an excluded
  record in `_sources.jsonl` (see Provenance above).

The shipped map:

| Cache symbol | HistData | Scale | First month | Verified |
|---|---|---|---|---|
| USA500IDXUSD | SPXUSD | 1 | 2010-11 | yes |
| DEUIDXEUR | GRXEUR | 1 | 2010-11 | yes |
| GBRIDXGBP | UKXGBP | 1 | 2010-11 | yes |
| JPNIDXJPY | JPXJPY | 1 | 2010-11 | yes |
| AUSIDXAUD | AUXAUD | 1 | 2010-11 | yes |
| BRENTCMDUSD | BCOUSD | 1 | 2010-11 | **no**: the datafeed's Brent unit is cents, so 100 is expected |
| XAUUSD | XAUUSD | 100 | 2009-03 | yes |
| EURUSD, GBPUSD, USDCHF | same | 10000 | 2000-05 | yes |
| AUDUSD, USDCAD | same | 10000 | 2000-06 | yes |
| USDJPY | USDJPY | 100 | 2000-05 | yes |
| GBPJPY | GBPJPY | 100 | 2002-05 | yes |
| AUDJPY, CHFJPY | same | 100 | 2002-08 | yes |
| EURCHF, EURGBP | same | 10000 | 2002-03 | yes |
| GBPCHF | GBPCHF | 10000 | 2002-08 | yes |
| EURCAD | EURCAD | 10000 | 2007-03 | yes |
| AUDCAD | AUDCAD | 10000 | 2007-07 | yes |
| AUDNZD, GBPAUD | same | 10000 | 2007-09 | yes |
| NZDCAD | NZDCAD | 10000 | 2008-03 | yes |
| EURSEK | EURSEK | 10000 | 2008-08 | **no** |

#### How a HistData run fills the cache

- **Months.** A HistData month covers UTC 05:00 on the 1st to 05:00 on the
  1st of the next month, so the 1st of a month also needs the previous month's
  file. A month whose days are all committed already is skipped without a
  request; a month before the instrument's first month is skipped with a log
  line. A month's zip is kept under `{cache}/{SYMBOL}/_histdata/{YYYY}{MM}.zip`
  while any day it covers is uncommitted, so the next run reuses it, and is
  deleted (or, with `--keep-raw`, retained) once they all are. A corrupt zip
  is fetched again.
- **Settled months.** HistData updates the running month's file from time to
  time, so a month's file is treated as final only once the month ended at
  least `--commit-partial-after-days` days ago (default 7). Until then a day is
  committed only if ticks after it exist, and the zip is not kept. A zip that
  was downloaded before its month settled is fetched again.
- **Empty and partial days.** A day without ticks (weekend, holiday, outage)
  is committed as an empty day when the file shows ticks on both sides of it,
  or ticks before it and a settled month. Days before an instrument's first
  tick are never committed. A day that needs a month HistData has no file for
  is retried until that month has settled, then committed from the ticks it
  has and recorded in `_partial_days.jsonl` with `gap_reason`
  `histdata_month_unavailable` and the missing UTC hours. A month that fails
  to download after `--retries` is not a gap: its days stay uncommitted for
  the next run, and the month is listed in the end-of-run summary.

### Splicing sources

Different sources are different liquidity providers. A series that is one
source up to some day and another after it carries the basis between the two
at the join, and the two can differ in tick granularity and session coverage,
so check the basis before trusting a spliced series:
`scripts/histdata_splice_check.py` (below) compares daily closes of both
sources over an overlap.

---

## Repairing a cache

### Normalizing prices into their expected band (`tradedesk-md-normalize`)

If you already populated `--cache-dir` with the wrong price scale, the package
ships a repair command:

```bash
tradedesk-md-normalize --cache-dir ./cache --dry-run
tradedesk-md-normalize --cache-dir ./cache --symbols EURUSD USDJPY
```

`tradedesk-md-normalize` rewrites cached daily candle files in place when a
day's median price lies outside the expected natural-unit band for its symbol.
It picks the power-of-ten factor in `[1e-5, 1e5]` whose result sits closest
to the geometric midpoint of the band, so it corrects both **over-scaled** and
**under-scaled** days. Days already inside the band are left untouched.

The bands are configuration. The package ships
`tradedesk_marketdata/price_bands.example.toml`, which holds a `default` band,
`[[contains]]` substring rules and per-symbol bands, and uses it unless you
pass `--bands your-copy.toml`. A band must be wide enough to hold the
instrument's whole history in the cache, and narrower than a factor of ten.
The shipped bands are in natural units. If your cache stores raw units (as
the HistData symbol map does), use a bands file in those units, or
`tradedesk-md-rescale` below.

The normalizer only updates the cached daily candle files under `--cache-dir`.
If you already wrote range-level CSVs with `--out`, rerun your export command
after normalizing so those output files are regenerated from the corrected
cache.

### Rescaling a cache that drifted off its own dominant scale

Use `tradedesk-md-rescale` when the bulk of a symbol's cache is at one scale
and some days drifted off it. It finds the symbol's dominant cache scale
(median of per-day medians) and snaps every off-scale day back onto it by a
power-of-ten factor:

```bash
tradedesk-md-rescale --cache-dir ./cache --dry-run
tradedesk-md-rescale --cache-dir ./cache --symbols USDJPY
```

Days whose median cannot be reconciled to a power of ten of the dominant
scale are reported as `unfixable`; delete and re-export those with the
matching scale.

---

## Data-quality audit scripts

The repository also ships four maintainer-oriented audit scripts under
`scripts/` for checking whether an existing local candle cache still looks
healthy after exporter changes or upstream Dukascopy drift.

`scripts/dukascopy_audit.py` is a read-only local audit. It inspects the
cached 1-minute bid/ask candles for each instrument and emits JSON covering:

- session-gap counts and longest intraday gap
- DST-transition day bar-count anomalies
- spread sanity percentiles
- stale-price runs

Example:

```bash
python scripts/dukascopy_audit.py \
  --cache ./cache \
  --instruments EURUSD GBPUSD USDJPY XAUUSD \
  --year-start 2024 \
  --year-end 2025 \
  --out /tmp/dukascopy_audit.json
```

`scripts/dukascopy_cross_provider.py` is a cross-provider check. It compares
the local Dukascopy daily close series against ECB/Frankfurter reference rates
for FX and Yahoo Finance reference closes for indices, metals, and commodity
proxies.

Example:

```bash
python scripts/dukascopy_cross_provider.py \
  --cache ./cache \
  --instruments EURUSD GBPUSD USDJPY XAUUSD USA500IDXUSD \
  --start 2024-01-01 \
  --end 2025-12-31 \
  --out /tmp/dukascopy_cross_provider.json
```

`scripts/audit_fx_scale.py` is a focused FX scale-corruption audit. For each
``DD_{bid,ask}.csv.zst`` under ``<cache_dir>/<SYMBOL>``, it flags day files
whose median close falls outside an explicit FX-rate envelope (e.g.
`[0.30, 2.00]` for NZDUSD-style 4-decimal FX), so caches that were exported
with the wrong `--price-divisor` show up immediately. It reports per-year
and day-of-week histograms, or with `--print-dates` emits one ISO date per
line for shell pipelines.

Example:

```bash
python scripts/audit_fx_scale.py NZDUSD --cache-dir ./cache --min 0.30 --max 2.00
python scripts/audit_fx_scale.py NZDUSD --cache-dir ./cache --print-dates
```

`scripts/histdata_splice_check.py` measures the basis between HistData and
Dukascopy before you splice them. A cache holds one source per day, so the
overlap comes from a second cache that a `--source histdata` run filled over
dates the main cache has from Dukascopy (HistData covers 2020 onwards too).
Per day it compares the mid close of both sources at the last minute at or
before 21:00 UTC that both have, and reports bias, mean and median absolute
difference, the largest divergence and Pearson r, in cache units and percent,
plus the days in the main cache where the recorded source changes.

```bash
tradedesk-md-export --source histdata --symbols USA500IDXUSD \
  --from 2020-01-01 --to 2020-12-31 --cache-dir ./cache-histdata --workers 1
python scripts/histdata_splice_check.py \
  --cache ./cache --histdata-cache ./cache-histdata \
  --instruments USA500IDXUSD \
  --start 2020-01-01 --end 2020-12-31 \
  --out /tmp/histdata_splice.json
```

These scripts are intended for maintainers validating cached data quality, not
for the normal export path. `dukascopy_cross_provider.py` performs live HTTP
requests to external reference feeds, so it requires internet access in
addition to a populated local cache.

---

## Intended workflow

This tool is intended to be used as a **data preparation step**, not as part of
your backtest runtime loop:

1. Download and export historical data once
2. Commit or archive the output CSV + metadata if applicable
3. Run fast, deterministic backtests against local files

---

## Running an export

### The cache (`--cache-dir`)

The exporter fetches only what the cache does not already hold, builds each
day's candle files once every unit it needs has arrived, and then discards
the raw downloads, unless you pass `--keep-raw`. Re-running the same export is
safe and cheap: it fills in gaps and finishes quickly where days are already
cached.

`--cache-dir` defaults to `.cache/marketdata` (relative to the current working
directory). Pass `--no-cache` to disable caching entirely and always
re-download. Treat the cache as a permanent store of market data that grows
over time, not a scratch directory: point `--cache-dir` at the same place
(for example `~/marketdata`) wherever you run the tool.

### Interrupting a run

One Ctrl-C cancels: queued fetch units are dropped, each in-flight request
ends at its next timeout, retry or download chunk, and the exporter exits 130
once those have returned (at most one read timeout). A second Ctrl-C exits 130
immediately without waiting. Every cache write is atomic, so an interrupted run
leaves nothing half-written and the next run picks up where it stopped.

### Timeouts and retries (`--connect-timeout`, `--read-timeout`, `--retries`)

Every request waits `--connect-timeout` seconds (default `2`) for a connection
and `--read-timeout` seconds (default `10`) for the answer. A fetch unit (a
Dukascopy hour, a HistData month) is attempted `--retries` times (default `3`,
with exponential backoff) before it is skipped for this run. Each source's
subsection above says what a skipped unit means for its days.

### Logging verbosity (`--log-level`)

`--log-level` controls how much the exporter prints (default `info`). Accepted
values, from quietest to noisiest, are `fatal`, `error`, `warn`, `info`,
`debug`, and `trace`. `fatal` and `trace` are convenience aliases mapped onto
the standard library's `CRITICAL` and `DEBUG` levels, so `trace` behaves
identically to `debug` rather than erroring out.

```bash
tradedesk-md-export --source dukascopy --symbols EURUSD \
  --from 2025-01-01 --to 2025-01-31 \
  --out data --cache-dir ./cache --workers 1 \
  --log-level debug
```

Use `--log-level debug` (or `trace`) when diagnosing slow or failing fetch
units; the extra detail shows each unit's fetch and decode decisions.

### Resampled CSV using `--out`

If you resample to an `--out` location, the tool writes separate bid and ask
OHLCV CSV files with UTC timestamps that include an explicit `+00:00` offset:

```text
timestamp,open,high,low,close,volume
2025-01-01 00:00:00+00:00,1.10342,1.10361,1.10311,1.10355,1234.0
```

- Timestamps are always **UTC**
- Prices are floats in the cache's units, **after the source's scaling**
  (`--price-divisor`, or the symbol map's scale)
- Volume is derived from tick volume (`0.0` for HistData, which has none)

#### Metadata sidecar (`.meta.json`)

Every output CSV is accompanied by a metadata file describing how it was generated:

```json
{
  "data_type": "candles",
  "generated_at": "2026-03-06T16:58:50.397630Z",
  "params": {
    "date_from": "2026-01-05",
    "date_to": "2026-01-06",
    "price_side": "bid",
    "resample": "15MIN"
  },
  "price_divisor": 10.0,
  "schema_version": "1",
  "source": "dukascopy",
  "symbol": "GBPUSD",
  "timestamp_format": "iso8601_utc"
}
```

This ensures datasets are **self-describing and reproducible**, even months later.

`--resample` requires `--out`. If you run the tool without `--resample`, it will
populate the `--cache-dir` with the cached source data and daily candles but it
will not emit the final range-level output CSVs in `--out`.

---

## Requirements

- Python 3.11+
- Internet access to the Dukascopy datafeed or HistData.com

## Credentials and Release Automation

Normal exporter usage, local development, and CI do not require repository
secrets or broker credentials.

Maintainers running `.github/workflows/prepare-release.yml` need these
repository secrets configured:

- `RELEASE_APP_ID`
- `RELEASE_APP_PRIVATE_KEY`

The release workflow uses those secrets to mint a GitHub App token for
checkout, version bumping, pushing the release commit, and creating the GitHub
release. `.github/workflows/publish.yml` uses PyPI trusted publishing via
GitHub OIDC (`id-token: write`), so no PyPI API token secret is expected in
this repository.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk)

## Contributing

See CONTRIBUTING.md for guidelines on contributing to tradedesk-marketdata.
