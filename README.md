
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
| Timestamps | UTC | local time with daylight saving (New York, then Zurich − 6 h; see below), converted to UTC on decode |
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
`YYYYMMDD HHMMSSNNN,bid,ask,volume` line in a local time
([Timestamps](#timestamps) below). It is a free service run for traders' own research and backtesting,
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

#### Timestamps

HistData describes its stamps as "EST without daylight saving", a fixed UTC-5.
**That is wrong** for about seven months of every year, so the exporter does
not use it. It decodes each stamp under the rule of the provider HistData
took the data from:

- **Before 2018-12-16:** `America/New_York` local time, US daylight saving
  included.
- **From 2018-12-16:** `Europe/Zurich` local time minus six hours, so EU
  daylight saving.

Both rules are UTC-5 in winter and UTC-4 in their own summer. The two eras
differ in the weeks between the US and EU clock changes.

The evidence: against a Dukascopy cache of the same instruments, HistData's
winter days match minute for minute at shift 0, and its summer days match
only an hour earlier, switching on the US calendar before 2018-12-16 and on
the EU calendar after. The switch itself is where SPXUSD's 0.25-point price
grid and its session times change, between Friday 2018-12-14 and Sunday
2018-12-16. A stamp in a local hour that is skipped (spring forward) moves
forward an hour, and one in a repeated hour (fall back) reads as its first
occurrence. Both fall in the weekend close.

**HistData days written before this rule was added** carry a one-hour shift
on every day under daylight saving: their candles are stamped an hour late.
To repair such a cache, delete the affected days' files and re-run the
export. Units kept with `--keep-raw` are re-decoded locally, and the rest are
downloaded again. Each re-committed day gets a new `_sources.jsonl` record.

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
scale_evidence = "Dukascopy 2024-01-10: raw 4786.2"
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
- **Verification.** An entry is verified only when its `scale_evidence`
  records the other source's figure that confirmed the scale; the loader
  refuses `scale_verified = true` without it. An export of an unverified
  symbol logs a warning. Check a few committed days against another source
  before trusting them, then record the figure in your copy of the map.
- **Brent.** BRENTCMDUSD's scale is 100: Dukascopy stores Brent in cents
  (9712.8 on 2022-02-21 where HistData quotes 97.128). Earlier versions of the
  map had 1. **Existing HistData BRENTCMDUSD days are 100× too small**: delete
  them and run the export again. Units kept with `--keep-raw` are re-decoded
  locally, and the rest are fetched again.
- **Exclusion spans.** A day inside a span gets no day files and an excluded
  record in `_sources.jsonl` (see Provenance above).

The shipped map:

| Cache symbol | HistData | Scale | First month | Verified (Dukascopy evidence) |
|---|---|---|---|---|
| USA500IDXUSD | SPXUSD | 1 | 2010-11 | yes: 2024-01-10: raw 4786.2 |
| DEUIDXEUR | GRXEUR | 1 | 2010-11 | yes: 2024-01-10: raw 16734.2; 2020-06-17..2023-12-05 excluded (below) |
| GBRIDXGBP | UKXGBP | 1 | 2010-11 | yes: 2024-01-10: raw 7660.91 |
| JPNIDXJPY | JPXJPY | 1 | 2010-11 | yes: 2024-01-10: raw 34928.6 |
| AUSIDXAUD | AUXAUD | 1 | 2010-11 | yes: 2024-01-10: raw 7494.43 |
| BRENTCMDUSD | BCOUSD | 100 | 2010-11 | yes: 2022-02-21: raw 9712.8 for 97.128 |
| XAUUSD | XAUUSD | 100 | 2009-03 | yes: 2024-01-10: raw 202698 |
| EURUSD | EURUSD | 10000 | 2000-05 | yes: 2024-01-10: raw 10974.3 |
| GBPUSD | GBPUSD | 10000 | 2000-05 | yes: 2024-01-10: raw 12745.4 |
| USDCHF | USDCHF | 10000 | 2000-05 | yes: 2024-01-10: raw 8506.1 |
| AUDUSD | AUDUSD | 10000 | 2000-06 | yes: 2024-01-10: raw 6702.6 |
| USDCAD | USDCAD | 10000 | 2000-06 | yes: 2024-01-10: raw 13380.8 |
| USDJPY | USDJPY | 100 | 2000-05 | yes: 2024-01-10: raw 14573.2 |
| GBPJPY | GBPJPY | 100 | 2002-05 | yes: 2024-01-10: raw 18574.1 |
| AUDJPY | AUDJPY | 100 | 2002-08 | yes: 2024-01-10: raw 9767.2 |
| CHFJPY | CHFJPY | 100 | 2002-08 | yes: 2024-01-10: raw 17134.1 |
| EURCHF | EURCHF | 10000 | 2002-03 | yes: 2024-01-10: raw 9335.1 |
| EURGBP | EURGBP | 10000 | 2002-03 | yes: 2024-01-10: raw 8611.3 |
| GBPCHF | GBPCHF | 10000 | 2002-08 | yes: 2024-01-10: raw 10841.4 |
| EURCAD | EURCAD | 10000 | 2007-03 | yes: 2024-01-10: raw 14683.6 |
| AUDCAD | AUDCAD | 10000 | 2007-07 | yes: 2024-01-10: raw 8968 |
| AUDNZD | AUDNZD | 10000 | 2007-09 | yes: 2024-01-10: raw 10761 |
| GBPAUD | GBPAUD | 10000 | 2007-09 | yes: 2024-01-10: raw 19018.4 |
| NZDCAD | NZDCAD | 10000 | 2008-03 | yes: 2024-01-10: raw 8334.8 |
| EURSEK | EURSEK | 10000 | 2008-08 | no (no Dukascopy day to compare with) |

The map's `[histdata]` section holds settings for every instrument:
`max_month_join_step` (default `0.7`) is the threshold of the month-join level
check below.

#### Known defects and the provider's guards

- **GRXEUR is not the DAX from 2020-06-17 to 2023-12-05.** Over that span
  HistData's `GRXEUR` runs at about 0.26× the DAX, its session ends an hour
  earlier, and its levels track the Euro Stoxx 50. A daily close ratio against
  a Dukascopy cache is 1.00 up to 2020-06-16, 0.26 from 2020-06-17, and 1.00
  again from 2023-12-06. The shipped map excludes the span for DEUIDXEUR, so
  those days get no day files from HistData, only `excluded` records, and
  another source can fill them. **Existing HistData caches of DEUIDXEUR** hold
  Euro Stoxx 50 levels on those days. Delete the day files from 2020-06-17 to
  2023-12-05 and refill them from another source, for example
  `--source dukascopy`.
- **The month-join level check.** When a month file is decoded, its first
  trading day's close is compared with the last accepted close before it: the
  previous month's in the same run, or the nearest committed day in the
  cache, as long as that close is at most seven days before. A join after a
  longer gap (a missing month, a holiday run) is not checked, because over
  weeks of missing data a real market can move further than any threshold
  that still catches a substitution. If the larger is more than
  `1 + max_month_join_step` times the smaller, the month is refused. The
  refusal is named in the log and the end-of-run summary (`refused=N`), and
  the days the month feeds stay uncommitted. A refused month is never a
  reference, but it does count as data for the gap: a substitution lasting
  several months is compared with the last accepted level until the level
  returns. Excluded days are left out on both sides.

  The check is HistData's own guard, distinct from the framework's scale
  sentry. The sentry compares a day with its neighbours already in the
  cache, so it cannot see a substitution that fills a fresh cache
  consistently. If a refused move is real, raise `max_month_join_step` in
  your copy of the map. If it is a substitution, add an exclusion span.

#### How a HistData run fills the cache

- **Months.** A HistData month covers its own stamps' month: UTC 05:00 on the
  1st (04:00 under daylight saving) to the same time on the 1st of the next
  month, so the 1st of a month also needs the previous month's file. A month whose days are all committed already is skipped without a
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

## Verifying a cache against a reference

`tradedesk-md-verify` compares the day files of a cache with those of a
reference cache of the same symbols, day by day. It reads both caches (with
the exporter's own day-file reader), writes nothing, and works in parallel over
the days; Ctrl-C stops it at once.

```bash
tradedesk-md-verify --reference ./cache-trusted --cache-dir ./cache-new \
  --symbols EURUSD DEUIDXEUR BRENTCMDUSD --from 2020-01-01 --to 2024-12-31
```

For every day that has a file on either side it reports:

- **Status:** one of `same`, `shifted`, `different`, `only-left`
  (reference only), `only-right` (cache only), `empty` or `unreadable`. An
  `unreadable` day file is a file that does not decode. It is logged and
  reported as a finding, never mistaken for a missing day.
- **Minute-level identity:** for every whole-minute shift in ±180 min (30-min
  steps), the fraction of the reference's minutes whose open, high, low and
  close are identical in the cache at that shift. Volume is excluded unless
  you pass `--include-volume`, because sources differ on it. A day that
  matches only at a non-zero shift is `shifted`.
- **Daily bars:** the largest relative difference of the day's
  open/high/low/close. Above `--tolerance` (default `0.002`) the day is
  `different`.

Per symbol and year it prints the counts by status and the worst and median
daily difference. It then prints the **close-ratio regimes**: runs of days
whose reference/cache close ratio stays within 5% of one value.
`--format json` gives the same as one document, with every day's record.
`--side ask` compares the ask files instead of the bid ones.

The findings are one line each. Here, with illustrative dates and counts, is
how the three HistData defects showed before they were repaired:

```text
EURUSD 2024-06-03..2024-06-28: 20 day(s) match the reference only shifted by +60 min (the cache's timestamps are 60 min late)
DEUIDXEUR 2020-06-17..2023-12-05: reference/cache close ratio 3.846 over 1214 day(s): another level or scale than the reference
BRENTCMDUSD 2010-11-01..2024-12-31: reference/cache close ratio 100 over 4340 day(s): another level or scale than the reference
```

The exit status is:
- `1` when any day is shifted, any day differs beyond the tolerance, any
  regime is other than 1.0, any day file is unreadable, or a symbol has no
  day with data on both sides (`no-overlap`: a mistyped symbol or cache
  directory must not pass as a clean result). The output always states how
  many days were compared, per symbol and in total;
- `0` otherwise. Days present on one side only are counted, but do not fail
  the check;
- `2` for a usage error;
- `130` after Ctrl-C.

**Recommended practice:** after any backfill from a new source, verify the
overlap against the source you already trust, before you rely on the new
days. A cache holds one source per day ("first committed wins"), so fill the
overlap into a second cache with the new source and verify that cache against
the main one.

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
