
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
  fetched or rewritten, by any source. To replace one day, re-export it with
  `--reexport` (see [Re-exporting one committed day](#re-exporting-one-committed-day---reexport)).
- **Earliest-date floor.** A symbol with a floor in
  `{cache}/{SYMBOL}/_floor.json` gets no day before it, from any source (see
  [Retiring a symbol's oldest days](#retiring-a-symbols-oldest-days-tradedesk-md-floor)).
- **Bad-tick rule.** Before candles are built, a tick's bid or ask is dropped
  when its own other side and its neighbouring ticks contradict it (see
  [Dropped ticks](#dropped-ticks-the-bad-tick-rule)).
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

#### Dropped ticks (the bad-tick rule)

A source can deliver one tick whose price on one side is corrupt while the
other side is correct: for example an ask with one extra digit (ten times too
large) next to a correct bid. Copied into the candles, it makes a high or a low
that never traded. The export applies one generic rule, `one_side_outlier`, to
each fetched unit's ticks before it builds candles. For each tick and side:

- the side's price is **isolated** when it differs by more than
  `--bad-tick-max-jump` (relative; default `0.1`) from the median of that side
  over the `--bad-tick-window` ticks before it (default `5`), and from the
  median over the ticks after it (only one of the two at the edge of a unit);
- the price is dropped when it is isolated, differs by more than the same
  fraction from the tick's other side, and that other side is not isolated.

A dropped price goes into neither its side's candles nor its volume; the other
side of the tick is kept. A fast move on both sides, or a step the next ticks
follow, is kept. The rule holds no value per instrument, and with the same
input it always drops the same prices. A day with nothing dropped is written
exactly as before.

Every dropped price is listed in the day's commit record in `_sources.jsonl`,
in time order:

```json
{"day": "2024-01-02", "source": "histdata", "scale_factor": 1.0, "committed_at": "...", "source_unit": "...", "dropped_ticks": [{"ts": "2024-01-02T10:20:00+00:00", "side": "ask", "price": 11.0012, "rule": "one_side_outlier"}]}
```

A record without `dropped_ticks` is a day the rule removed nothing from. The
example values are synthetic.

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
`--side ask` compares the ask files instead of the bid ones. A symbol with an
[earliest-date floor](#retiring-a-symbols-oldest-days-tradedesk-md-floor) in
`--cache-dir` is compared from its floor on: the days before it are not
compared, counted or reported as missing, and the report names the floor.

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

### Re-exporting one committed day (`--reexport`)

A day committed before the bad-tick rule, or with another setting of it, is not
rebuilt by a normal export ("first committed wins"). To rebuild one day for one
symbol under the rule, with no hand edit:

```bash
tradedesk-md-export --source histdata --symbols EURUSD \
  --from 2024-01-02 --to 2024-01-02 --cache-dir /path/to/cache --reexport
```

`--reexport` takes exactly one symbol, `--from` equal to `--to`, and a cache
(not `--no-cache`); the day must already be committed. The source builds the
day again from its units (a kept unit under `_raw/` first, else a new
download), and the bad-tick rule runs as in any export. When the source commits
the day, both day files are replaced atomically and a new record is appended
to `_sources.jsonl` with `"reexport": true` (and `dropped_ticks` when the rule
removed anything); for that day, the later record wins. When the source does
not commit it (a failed download, a gap rule, an exclusion, or the scale
sentry), the day files and `_sources.jsonl` stay as they were and the export
exits `1`.

Then check the day as usual, for example against a reference cache:

```bash
tradedesk-md-verify --reference /path/to/reference --cache-dir /path/to/cache \
  --symbols EURUSD --from 2024-01-02 --to 2024-01-02 --side ask
```

### Retiring a symbol's oldest days (`tradedesk-md-floor`)

When a symbol's source is not reliable before some date, set that date as the
symbol's earliest-available day (its *floor*). No hand edit is needed:

```bash
tradedesk-md-floor --cache-dir /path/to/cache --symbol SYMBOL \
  --floor YYYY-MM-DD --backup-dir /path/to/retired --dry-run
tradedesk-md-floor --cache-dir /path/to/cache --symbol SYMBOL \
  --floor YYYY-MM-DD --backup-dir /path/to/retired
```

- **Nothing is deleted.** Every day file before the floor moves to
  `{backup-dir}/{SYMBOL}/{YYYY}/{MM0}/{DD}_{bid,ask}.csv.zst`, the store's own
  layout. Each file is copied and its bytes checked before the live file is
  removed. The `_sources.jsonl` and `_partial_days.jsonl` lines of those days
  move to the same file names under `{backup-dir}/{SYMBOL}/`. The days on and
  after the floor are not touched.
- **The floor** is written to `{cache}/{SYMBOL}/_floor.json`:

  ```json
  {"floor": "2024-01-15", "set_at": "...", "tool_version": "2.0.0"}
  ```

- **The record.** Every change appends one line to the live `_sources.jsonl`:

  ```json
  {"status": "floor", "action": "set", "floor": "2024-01-15", "previous": null, "days_moved": 14, "backup_dir": "/path/to/retired", "tool_version": "2.0.0", "decided_at": "..."}
  ```

  `action` is `set`, `lower` or `remove`; `previous` is the floor before the
  change. A record with a `status` is not a commit, so the manifest stays a
  manifest of the days that remain.
- **After the floor is set**, an export does not plan, fetch or write a day
  before it, `--reexport` of such a day is refused, and `tradedesk-md-verify`
  does not check one.
- **The same floor again** with no day before it left in the store writes
  nothing. A run stopped part way is completed by running it again.
- **A later floor** moves the next days the same way. **A lower floor** is
  refused unless you add `--lower`. `--remove` removes the floor. Both are
  recorded. Neither brings the moved days back; export them again if you
  want them.
- **Refusals** (exit `1`, nothing changed): a symbol not in the cache, a
  lower floor without `--lower`, a `--backup-dir` inside the cache, a backup
  file that exists with other bytes, or a `_floor.json` that does not parse.
  An export or a verify also stops on a `_floor.json` that does not parse.

The sha256 of `_sources.jsonl` changes with every floor change, like with any
other change to the store.

### Refreshing a store (`tradedesk-md-refresh`)

One command keeps a store current: it re-checks a window of days, adds the new
days, re-fetches the empty or partial ones, backs up every change first and
writes a report. It is made to run once a week.

This example runs as written. It installs the package, makes an empty store
and does a dry run on it. The dry run plans the work, writes the report and
changes nothing in the store:

```bash
pip install tradedesk-marketdata
mkdir -p ./cache
tradedesk-md-refresh --cache-dir ./cache --since 2026-10-01 --until 2026-10-09 \
  --backup-dir ./backups --report refresh.json --dry-run
```

On an empty store it prints `would add 0, recheck 0 and send 0 day(s) to the
fallback`. On a real store, use your own paths and start day (template; replace
the values in angle brackets). Run the dry run first, read the report, then run
it again without `--dry-run`:

```bash
tradedesk-md-refresh --cache-dir <cache-dir> --since <YYYY-MM-DD> \
  --backup-dir <backup-dir> --report refresh.json --dry-run
tradedesk-md-refresh --cache-dir <cache-dir> --since <YYYY-MM-DD> \
  --backup-dir <backup-dir> --report refresh.json
```

The command is an orchestrator over the export: every day file and manifest
line is written by the same code as a `tradedesk-md-export` run, with the same
scale sentry and the same atomic commit. Per symbol (`--symbols`, or every
symbol in the store that the HistData symbol map holds):

1. **Scan** the window, `--since` to `--until` (default: today, UTC). A day
   with no day files is *missing*. A weekday (Monday to Friday) is *empty* when
   its day files hold no candle, and *partial* when its latest
   `_partial_days.jsonl` record is not older than its latest commit record in
   `_sources.jsonl`. An empty or partial weekday on a market-closure day
   (25 December, 1 January, and each `--closure MM-DD`) is skipped.
2. **HistData** (the primary source): one export of the window, with the empty
   and partial weekdays rechecked. Each month file is fetched once (from
   HistData itself, not from a staged copy, when the month holds a rechecked
   day) and serves every missing and rechecked day in it. Missing days are
   added up to the newest day HistData has published; a later day is left for
   the next run. A complete day is never planned or rewritten. A rechecked day
   is re-exported only when HistData now has ticks for it and they differ from
   the stored day; a day whose fresh data is identical stays as it is and gets
   no `_sources.jsonl` line.
3. **Fallback** (only with `--fallback-source dukascopy`): each weekday up to
   the newest day with candles that is still missing or empty, and not a
   closure, is exported from Dukascopy, one day per run. The fallback must
   write the store's units, so each symbol needs its price divisor:
   `--price-divisor SYMBOL=N` (repeatable) or `--price-divisors FILE`, a TOML
   file of `SYMBOL = N` lines (the flag wins). A symbol without one is refused
   before any request. The scale sentry refuses a day at another scale; it is
   reported under `refused`. `--fallback-only FILE` (one `YYYY-MM-DD` per line,
   `#` comments) limits the fallback to the days named there. The
   `--connect-timeout`, `--read-timeout` and `--retries` options apply to both
   sources. Without `--fallback-source`, no Dukascopy request is made.

**Raw units (`--keep-raw`).** By default each source deletes its fetched raw
units once every day they feed is committed. With `--keep-raw`, the primary
source and the fallback move them to `{cache}/{SYMBOL}/_raw/{source}/` instead,
as `tradedesk-md-export --keep-raw` does (see
[Keeping the raw units](#keeping-the-raw-units---keep-raw)). A unit kept there,
by an export or by an earlier refresh, is not deleted or moved by a refresh
without the option: a re-fetched copy of that unit is deleted, and the kept one
stays as it is. The one exception is the same as for an export: a kept unit
that no longer decodes is fetched again. The report's `keep_raw` field records how the run was made.

**Backups.** A run that may change the store needs `--backup-dir`, outside the
store; without one it is refused before any request. Each run writes under
`{backup-dir}/{YYYYMMDDTHHMMSSZ}/{SYMBOL}/`, in the store's layout: before the
first change to a symbol, its `_sources.jsonl` and `_partial_days.jsonl`; before
each day is written, that day's current bid and ask files. Every copy is
checked against its source bytes. A new day has no files to copy.

**Dry run.** `--dry-run` makes no request and writes nothing (no backup
either). Its report lists, per symbol, the days the run would add
(`would_add`), recheck (`would_recheck`) and send to the fallback
(`would_fallback`); whether a rechecked day changes is known only after the
fetch.

**The report** (`--report PATH`, JSON, `schema_version` 1):

| Field | Meaning |
|-------|---------|
| `since`, `until` | the window |
| `dry_run`, `primary_source`, `fallback_source`, `keep_raw` | how the run was made |
| `backup_dir` | this run's backup directory (`null` in a dry run) |
| `changed` | `true` when any symbol's `_sources.jsonl` changed |
| `skipped_symbols` | store symbols not refreshed, with the reason |
| `symbols[]` | one entry per symbol, below |

| Per-symbol field | Meaning |
|------------------|---------|
| `added` | days that were missing and are now committed, each `{day, source}` |
| `reexported` | empty or partial days written again, each `{day, source}` |
| `unchanged` | rechecked days whose fresh data was identical |
| `still_empty` | weekdays up to `newest_day`, not closures, still missing or empty after the run |
| `skipped_closures` | empty or partial weekdays on a closure day, not rechecked |
| `refused` | days not written, each `{day, source, reason}` (`scale_sentry`, `write_failed`) |
| `excluded` | days the source's exclusion spans recorded as excluded, each `{day, source}` |
| `newest_day` | the newest day with candles in the window after the HistData step |
| `sources_sha256_before`, `sources_sha256_after` | sha256 of `_sources.jsonl` (`null` when absent) |
| `changed` | `true` when the two digests differ |
| `error` | why the symbol's refresh stopped, or `null` |

Example (synthetic: the symbol `TESTSYM` and its days are made up):

```json
{
  "schema_version": 1,
  "tool_version": "2.0.0",
  "generated_at": "2015-01-25T06:00:04+00:00",
  "since": "2014-12-29",
  "until": "2015-01-25",
  "dry_run": false,
  "primary_source": "histdata",
  "fallback_source": "dukascopy",
  "keep_raw": false,
  "backup_dir": "/path/to/backups/20150125T060000Z",
  "changed": true,
  "symbols": [
    {
      "symbol": "TESTSYM",
      "changed": true,
      "added": [{"day": "2015-01-17", "source": "histdata"}, {"day": "2015-01-18", "source": "histdata"}],
      "reexported": [{"day": "2015-01-14", "source": "dukascopy"}],
      "unchanged": ["2015-01-09"],
      "still_empty": [],
      "skipped_closures": ["2015-01-01"],
      "refused": [],
      "excluded": [],
      "newest_day": "2015-01-23",
      "sources_sha256_before": "3f1c…",
      "sources_sha256_after": "9a0e…",
      "error": null
    }
  ],
  "skipped_symbols": {}
}
```

The last line of output is the run's summary, `tradedesk-md-refresh: no
change: …` when nothing changed. Exit status: `0` when the run completed (days
still empty are reported, not an error); `1` when a day was refused, a
symbol's refresh failed, or the run was refused before any change (for
example a `--backup-dir` inside the store); `2` on a usage error.

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

## Reference data: central-bank daily rate series (`tradedesk-md-rates`)

`tradedesk-md-rates` fetches public central-bank daily rate series into a
reference-data area of the store, next to the candle cache, and verifies that
the kept raw downloads reproduce the parsed files.

The store directory must already exist:

```bash
mkdir -p ./cache
tradedesk-md-rates fetch --store ./cache --config my-series.toml
tradedesk-md-rates verify --store ./cache
```

### The series to fetch (`--config`)

The package ships no list of series. You name the series in a TOML file of
`[[series]]` tables; `tradedesk_marketdata/rates.example.toml` documents every
key. Each table has an `id` (the series' directory name in the store), a
`source` (the published format) and a `key` (the source's own code for the
series):

| `source` | Publisher and format | `key` |
|---|---|---|
| `fred` | FRED (St. Louis Fed), `fredgraph.csv` | the FRED series id |
| `ecb` | ECB Data Portal, `format=csvdata` | `{flow}/{series key}` |
| `boe` | Bank of England database (IADB), CSV | the series code |
| `boj` | Bank of Japan time-series data search API, CSV | `{database}/{series code}` |
| `rba` | RBA statistical table, CSV | `{table}/{series ID}` |
| `rba-xls` | RBA statistical table, `.xls` workbook (the historical files under `tables/xls-hist/`) | `{file}/{series ID}` |
| `rbnz` | RBNZ statistical table, `.xlsx` workbook | `{file}/{Series Id}` |
| `snb` | Swiss National Bank data portal (`data.snb.ch`), cube CSV | `{cube}/{dimension selection}`, one item per dimension: `D0(item)` or `D0(item),D1(item)` |
| `boc` | Bank of Canada Valet API, JSON | the series name |
| `swea` | Sveriges Riksbank SWEA API, JSON | the series id |
| `swestr` | Sveriges Riksbank SWESTR API, JSON | the SWESTR id |
| `norges` | Norges Bank SDMX data API, SDMX-CSV (`format=sdmx-csv&labels=id`) | `{flow}/{series key}`, one series: a code for every dimension of the flow (no wildcard, no `+`, no short key) |
| `bundesbank` | Deutsche Bundesbank time-series database, SDMX web service (`api.statistiken.bundesbank.de`), SDMX-CSV (`format=sdmx_csv&lang=en`) | `{flow}/{series key}`, one series: a code for every dimension of the flow (no empty part, no `+`, no `*`, no short key) |

The workbook formats read the publisher's table layout: `rba-xls` the same
metadata rows as the RBA CSV (`Frequency`, `Units`, `Publication date`,
`Series ID`) on the `Data` sheet, with date cells; `rbnz` the `Data` sheet's
`Unit` and `Series Id` rows and the `Table Description` sheet's
`Published Date`. The `file` part of the key is the workbook's name without
its extension: `rba-xls` asks for
`https://www.rba.gov.au/statistics/tables/xls-hist/{file}.xls`, and `rbnz`
derives its URL from a file name such as `hb2-daily` (`.../statistics/series/b/b2/hb2-daily.xlsx`);
set `url` for any other file. A `.xls` number is stored as a binary double
with no text, so its `value` is the shortest decimal that reads back as the
same double (`4.4`, `3`); a `.xlsx` number keeps its stored text.
Reading `.xls` needs `xlrd`, which is optional:
`pip install 'tradedesk-marketdata[xls]'` (the `rbnz` `.xlsx` reader needs
nothing more).
A workbook is one whole file, so for `rba-xls` and `rbnz` the `start` key
drops the rows dated before it: they are not read, and they are not gaps. A
cell that is not a decimal number (for example a range such as `1.00 to
1.50`) refuses the series, with its line; a `start` after that row lets the
rest of the column in.

**The Bundesbank format.** `bundesbank` asks for
`https://api.statistiken.bundesbank.de/rest/data/{flow}/{series key}?format=sdmx_csv&lang=en`,
the series key as the time-series database shows it after the flow (the part
of the time-series code after the first `.`). The service reads an empty key
part as a wildcard and `+` as a list of codes, so the format refuses such a
key before it asks: one fetch is one series. Each row must name the flow
(`DATAFLOW` `BBK:{flow}(version)`), the key's codes and a daily frequency (the
frequency dimension, such as `BBK_STD_FREQ`, `D` or `B`; a `TIME_FORMAT`
other than `P1D` is refused). The service lists weekends and holidays with
`OBS_VALUE` `.` (`OBS_STATUS` `K`, "No value available"): each such day is a
gap `missing_in_source`, never a value. The unit is `BBK_UNIT`, as the
service writes it. An unknown series answers HTTP 404, and nothing is
written. Terms: the Bundesbank applies the ESCB
[terms of use regarding the reuse of ESCB statistics](https://www.bundesbank.de/en/homepage/user-information/terms-of-use-regarding-the-reuse-of-escb-statistics-621188)
(read 2026-10-23): published statistics may be reused free of charge, also
for commercial use, on the condition that the source is quoted and the
statistics (including metadata) are not modified; third-party data need the
originator's permission. The store keeps the source's values unchanged.

**A local file.** `url` may be a `file:` URL, for a publisher that refuses
scripted downloads (the RBNZ site does from some networks). Download the
workbook in a browser and name it in the configuration:

```toml
[[series]]
id = "EXAMPLE-RBNZ"
source = "rbnz"
key = "hb2-daily/EXAMPLE.SERIES"
url = "file:///home/me/downloads/hb2-daily.xlsx"
```

The fetch keeps the file as a raw file exactly as a download, and its
`_fetches.jsonl` line has `http_status` `null`.

Optional keys: `url` (instead of the format's default URL), `unit` (for a
source that states none, such as `fred`, `boe`, `snb`, `swea` and `swestr`; a unit the source states
wins, and a different `unit` is an error), `calendar` (`weekdays`, the
default, or `all`) and `start` (the first date to ask for, where the format
allows it). Only daily series are accepted: a download whose dates or stated
frequency are not daily is refused, and the refusal names the source and the
series key. A format that states no frequency (`fred`, `boe`, `rbnz`, `snb`,
`swea`, `swestr`, a `boc` series without a `dateFormat`, a `bundesbank` flow
without a frequency dimension, or an `rba` or `rba-xls` column without a
`Frequency` row) is judged by its dates: the median
step between dated rows must be 4 days or fewer (a weekend plus a holiday),
and one dated row is not enough to tell; an `snb` date that is not a day
(`YYYY-MM`) is refused at once. A download that states a daily frequency
(`ecb`, `boj`, `norges` `FREQ` `B` or `D`, `bundesbank` frequency dimension
`B` or `D`, `boc` `dateFormat` `daily`, or an
`rba` or `rba-xls` column with `Frequency` `Daily`) is accepted with one dated
row. A weekly or monthly series is refused before anything is written.

### The store layout

Each series lives in `{store}/_reference/rates/{id}/`. The candle tools skip
directories that start with `_`, so they do not see this area.

| File | Content |
|---|---|
| `_raw/{sha256}.{ext}` | every downloaded body, kept unchanged under its sha256 with the format's extension (`csv`; `json` for `boc`, `swea` and `swestr`; `xls` for `rba-xls`; `xlsx` for `rbnz`); never deleted |
| `_fetches.jsonl` | one line per kept download: `fetched_at` (UTC), `url`, `http_status` (the response's HTTP status; `null` for a `file:` URL), `sha256`, `bytes`, `raw` |
| `observations.csv` | `date,value,unit,published,revised`, one row per date with a value |
| `gaps.csv` | `date,reason`, one row per day without a value |
| `manifest.json` | the series' configuration, the parser and its version, every fetch, and the sha256, row count and span of the two CSV files |

- **`value`** is the source's decimal text, unchanged. For a JSON source it
  is the number as the download writes it (`3.250` stays `3.250`).
- **`published`** is the publication date as the source states it, written
  `YYYY-MM-DD` (`boj`: `LAST_UPDATE`; `rba`, `rba-xls`: the table's
  `Publication date`; `rbnz`: the `Published Date`; `snb`: the cube's
  `PublishingDate`; `swestr`: the UTC date of each day's `publicationTime`).
  It is empty when the source states none (`fred`, `ecb`, `boe`, `boc`,
  `swea`, `norges`, `bundesbank`); the fetch
  time in `_fetches.jsonl` is then the only time on record.
- **`revised`** is `true` when an earlier fetch held another value for the
  date.
- **Gaps are never filled.** `gaps.csv` lists each expected day (by
  `calendar`) between the first and last observation that has no value, and
  each row the source lists without a value. The reasons are
  `missing_in_source` (the source lists the date with no value: an empty
  cell, `.`, `NaN`, `null`, a JSON `null`, a workbook error cell such as
  `#N/A`, a `boc` observation without the series, or an `OBS_STATUS` of `M`),
  `no_row` (the source
  does not list the date) and `withdrawn` (an earlier fetch had a value, the
  latest fetch that covers the date has none).
- **The files are rebuilt from every fetch, in order.** A later fetch wins for
  each date it lists. Dates outside the later fetch's span keep their earlier
  rows.
- **The manifest pins the series.** It holds no write time and no tool
  version: it is a pure function of the fetches, so its sha256 changes only
  when the series' data or configuration changes. A fetch whose body is the
  same as the latest fetch's adds no line; with the same configuration it
  changes no file either (`unchanged`).
- **Nothing is written for a download that does not parse.** Before a fetch
  writes, every earlier raw file is checked against its sha256, and the
  series' `source` and `key` must match the stored manifest's.

Only `tradedesk-md-rates` writes this area.

### Verifying (`verify`)

`tradedesk-md-rates verify` re-hashes each raw file against its line in
`_fetches.jsonl`, parses the raw files again and rebuilds `observations.csv`,
`gaps.csv` and `manifest.json`. Each must be byte for byte the same as the
stored file. It writes nothing. Without `--series` it checks every series in
the area. A raw file that has no fetch line is a warning, not a failure.

Both commands take `--format text|json`. Exit status: `0` when every series
fetched or verified; `1` when a series failed (a download that did not arrive
or did not parse, a missing or changed raw file, a file the raw files do not
reproduce); `2` on a usage error, including a configuration that is not valid.

---

## Reference data: index-dividend series (`tradedesk-md-dividends`)

`tradedesk-md-dividends` fetches public index-dividend series into the same
reference-data area as the rate series, and verifies that the kept raw
downloads reproduce the parsed files. It works like `tradedesk-md-rates`: the
same raw files, fetch log, merge rule, manifest and `verify`.

Before the first fetch, create `my-dividends.toml`. Copy
`tradedesk_marketdata/dividends.example.toml`, keep only the series you need,
and replace its placeholder `example.invalid` URLs with real ones (see
[The series to fetch](#the-series-to-fetch---config-1)). Without this file,
`fetch` exits with status 2. The store directory must already exist:

```bash
cp tradedesk_marketdata/dividends.example.toml my-dividends.toml  # in a checkout; then edit it
mkdir -p ./cache
tradedesk-md-dividends fetch --store ./cache --config my-dividends.toml
tradedesk-md-dividends verify --store ./cache
```

The `shiller` format reads `.xls` files and needs the `xls` extra:
`pip install 'tradedesk-marketdata[xls]'`.

### Terms of use

Read a publisher's terms of use before you keep its data. The package ships a
preset only for a publisher whose terms do not forbid keeping a copy, and it
does not decide for you that your use is permitted:

| Format | Publisher | Terms (read 2026-10-21) |
|---|---|---|
| `nikkei` | Nikkei Inc. index CSVs, daily from 2023-01-04 | [terms of use](https://indexes.nikkei.co.jp/nkave/archives/file/terms_of_use_en.pdf) (2019-08-30): use beyond "personal use" or "quotation" under copyright law needs a licence; each CSV says copying needs Nikkei's permission |
| `shiller` | Robert J. Shiller, `ie_data.xls` from [shillerdata.com](https://shillerdata.com/), monthly from 1871 | no licence is stated; the dividend data derive from S&P data |
| `csv` | any | yours to check |

S&P Dow Jones Indices, FTSE Russell / LSEG and STOXX / DAX have no preset:
their terms of use forbid keeping their data and scripted download. Use the
`csv` format for such a source only with the publisher's written consent.

### The series to fetch (`--config`)

The package ships no list of series. You name the series in a TOML file of
`[[series]]` tables; `tradedesk_marketdata/dividends.example.toml` documents
every key. Each table has an `id` (the series' directory name in the store)
and a `kind`:

| `kind` | Inputs | `value` |
|---|---|---|
| `points` | `[series.input]`: a daily dividend-points index | the source's decimal text, unchanged |
| `tr_pr` | `[series.tr]` and `[series.pr]`: daily total-return and price-return index levels | `(TR_t / TR_p) / (PR_t / PR_p) - 1`, where `p` is the previous date on which both levels are published |
| `monthly` | `[series.input]`: a monthly dividend series | the source's value, unchanged; a **coarse fallback** |

Each input names a `source` (the format), a `key` and, where the format has
no default, a `url`:

| `source` | `key` | Roles |
|---|---|---|
| `nikkei` | `dividend_points`; `total_return` or `net_total_return`; `price` | `points`; `tr`; `pr` |
| `shiller` | the column header to read (default `D`); `url` is required | `monthly` |
| `csv` | none; the options `date_column`, `value_column` (required), `date_format`, `published_column`, `period_column`, `encoding`, `delimiter`, `skip_lines`, `footer_prefix`; `url` is required | any |

Optional series keys: `unit` (required where the format implies none;
`nikkei` implies `index points` or `index level`, and `tr_pr` defaults to
`fraction`) and `calendar` (`weekdays`, the default, or `all`; not for
`monthly`, where every month is expected). A daily input must step by a day
(a median step of 4 days or fewer). A monthly input's rows are dated the first
of their month, and must step by a month.

### The store layout

Each series lives in `{store}/_reference/dividends/{id}/`, beside
`_reference/rates/`. The candle tools skip directories that start with `_`.

| File | Content |
|---|---|
| `_raw/{sha256}.{csv,xls}` | every downloaded body, kept unchanged under its sha256; never deleted |
| `_fetches.jsonl` | one line per kept download: `fetched_at` (UTC), `url`, `http_status` (`null` for a `file:` URL), `sha256`, `bytes`, `raw`, `input` (`points`, `monthly`, `tr` or `pr`) |
| `observations.csv` | `points`: `date,value,unit,period,published,revised`; `monthly`: the same and `coarse` (always `true`); `tr_pr`: `date,value,unit,span_from,published,revised` |
| `gaps.csv` | `date,reason`, one row per expected day (or month) without a value |
| `manifest.json` | the configuration, `frequency` (`daily` or `monthly`), `coarse`, `unit`, the parser of each input and its version, for `tr_pr` the `derivation` (formula, 12 decimal places, half-even rounding), every fetch, and the sha256, row count and span of the two CSV files |

- **`period`** is the period the source states for the value, empty when it
  states none (`nikkei` `dividend_points`: the `Year` its points accumulate
  for).
- **`published`** is the publication time as the source states it, unchanged,
  empty when it states none (`nikkei`, `shiller`). For `tr_pr` it is the later
  of the two inputs' texts.
- **`span_from`** (`tr_pr`) is the date `p` of the previous levels. When it is
  not the previous expected day, the days between are in `gaps.csv`.
- **`revised`** is `true` when an earlier fetch held another value for an
  input of the row (for `tr_pr`: either level on either date).
- **`shiller` values** are numbers in the workbook. They are written as the
  shortest decimal text that reads back as the same number.
- **Gaps are never filled.** The reasons are those of the rate series
  (`missing_in_source`, `no_row`, `withdrawn`) and, for `tr_pr`, `missing_tr`
  and `missing_pr` (one level is published on the date, the other is not).
  For `tr_pr`, only the days between the first and last date with both levels
  are listed.

The merge rule, the manifest, `unchanged` and "nothing is written for a
download that does not parse" are as for the rate series. A `tr_pr` series
downloads both inputs and parses both before it writes; an input whose body
equals its latest fetch adds no line. The series' `kind` and inputs (all but
their URLs) must match the stored manifest's.

Only `tradedesk-md-dividends` writes this area.

### Verifying (`verify`)

As for the rate series: `tradedesk-md-dividends verify` re-hashes each raw
file, rebuilds the three files and compares them byte for byte. It writes
nothing. Both commands take `--format text|json`, and the exit status is the
same: `0` when every series fetched or verified; `1` when a series failed; `2`
on a usage error, including a configuration that is not valid.

---

## Reference data: release calendars (`tradedesk-md-calendars`)

`tradedesk-md-calendars` fetches publishers' release calendars (dated events
such as a policy decision or a statistics release) into the same
reference-data area as the rate and dividend series, and verifies that the
kept raw downloads reproduce the parsed calendar. It works like
`tradedesk-md-rates`: the same raw files, fetch log, manifest, `unchanged`
and `verify`.

Before the first fetch, create `my-calendars.toml`. Copy
`tradedesk_marketdata/calendars.example.toml`, keep only the calendars you
need, and replace its placeholder URLs with real ones (see
[The calendars to fetch](#the-calendars-to-fetch---config)). Without this
file, `fetch` exits with status 2. The store directory must already exist:

```bash
cp tradedesk_marketdata/calendars.example.toml my-calendars.toml  # in a checkout; then edit it
mkdir -p ./cache
tradedesk-md-calendars fetch --store ./cache --config my-calendars.toml
tradedesk-md-calendars verify --store ./cache
```

### Terms of use

The package ships no list of publishers and no preset for one. Read each
publisher's terms of use before you keep its calendar, and keep it only
where they permit both a stored copy and a scripted download. Some
publishers permit scripted access only when the request names a contact:
`fetch --user-agent TEXT` replaces the package's User-Agent header
(`tradedesk-marketdata/<version>`) for every download of the run. For a site
that refuses scripted requests, download the file by hand and give its
`file:` URL; the fetch line then has no HTTP status.

### The calendars to fetch (`--config`)

You name the calendars in a TOML file of `[[calendar]]` tables;
`tradedesk_marketdata/calendars.example.toml` documents every key. Each table
has:

| Key | Content |
|---|---|
| `id` | the calendar's directory name in the store (required) |
| `institution` | the publisher, written in every row of `events.csv` (required) |
| `timezone` | an IANA time-zone name (required): the zone of an event whose source states none |
| `source` | the format (required): `ics` or `csv`, below |
| `url` | the file to download (required) |
| `pages` | publisher pages to keep raw beside the file (optional) |
| `tzids` | (`ics` only) a table that maps the file's own `TZID` names to IANA names, for example `"Example-Eastern" = "America/New_York"` (optional) |
| `type`, `[[calendar.types]]` or `type_column` | the event type: exactly one of them, below |

| `source` | Reads |
|---|---|
| `ics` | an iCalendar (RFC 5545) file that the publisher offers. Each `VEVENT` with a `DTSTART` and a `SUMMARY` (the title) is one event. Folded lines are joined and escaped text is unescaped. `STATUS:CANCELLED` events are not listed. An event with `RRULE` or `RDATE` is refused: the tool does not expand a recurrence, so a calendar must list each date. |
| `csv` | a curated file: a person writes the events from the publisher's pages into a delimited file with a header row. The options `date_column`, `title_column` (required), `time_column`, `timezone_column`, `type_column`, `date_format` (default `%Y-%m-%d`), `time_format` (default `%H:%M`), `encoding` (default `utf-8-sig`), `delimiter` (default `,`) and `skip_lines` (default 0) name its columns. Name the pages the file was written from in `pages`, so their raw copies are kept with it. |

The event type is one of:

- `type = "..."`: one type for every event.
- `[[calendar.types]]` tables, each with a `type` and a `match` (a regular
  expression searched for in the title). The first rule that matches wins.
  An event that no rule matches is not kept; the manifest counts them
  (`events.unmatched`).
- `type_column` (`csv` only): the column that holds each event's type.

A type, like an id, is letters, digits, `.`, `_` and `-`.

### The store layout

Each calendar lives in `{store}/_reference/calendars/{id}/`, beside
`_reference/rates/` and `_reference/dividends/`. The candle tools skip
directories that start with `_`.

| File | Content |
|---|---|
| `_raw/{sha256}.{ext}` | every downloaded body, kept unchanged under its sha256; never deleted. The events file is `.ics` or `.csv`; a page takes its URL's extension when it is a known one, else `.html` or `.pdf` from its first bytes, else `.bin` |
| `_fetches.jsonl` | one line per kept download: `fetched_at` (UTC), `url`, `http_status` (`null` for a `file:` URL), `sha256`, `bytes`, `raw`, `input` (`events` for the parsed file, `page` for a kept page) |
| `events.csv` | `date,time,timezone,institution,type,title,source_url,raw_sha256,revised`, one row per event, sorted by date, time, type and title |
| `withdrawn.csv` | `date,time,timezone,type,title,listed_sha256,dropped_sha256`: the events that a later download no longer lists |
| `manifest.json` | the configuration, the parser and its version, every fetch, and the sha256, row count and span of `events.csv` (with its `revised` and `unmatched` counts) and of `withdrawn.csv` |

- **`time`** is `HH:MM` (`HH:MM:SS` when the seconds are not zero), as the
  source states it. It is empty when the source states no time of day (an
  all-day iCalendar event, an empty csv cell).
- **`timezone`** is the IANA zone of the date and time: the `TZID` of an
  iCalendar time (through `tzids` when the file uses its own name), `UTC`
  for an iCalendar time that ends in `Z`, the row's zone for a csv
  `timezone_column`, else the calendar's `timezone`. A `TZID` that is not an
  IANA name and is not in `tzids` is refused. The tool does not convert a
  time to another zone.
- **`title`** is the source's title with its white space collapsed.
- **`source_url`** and **`raw_sha256`** name the download the row was last
  read from: its URL and the sha256 of its raw file in `_raw/`.
- **`revised`** is `true` when an earlier download gave the event another
  time or zone.

An event's identity is its date, type and title. Two listings of the same
event in one download are one event when they give the same time and zone;
otherwise the download is refused.

**The merge rule.** The events files merge in the order of their fetches.
A later file wins for the dates of its span (its first to its last listed
date, including events that no type rule matches). An event an earlier file
listed, inside that span but not in the later file, moves to
`withdrawn.csv`, with the sha256 of the last file that listed it and of the
file that dropped it; when a later file lists it again, it leaves
`withdrawn.csv`. Events outside the later file's span keep their rows, so a
publisher page that shows only the coming year does not remove the past.

A fetch downloads the events file and then each page. The events file is
parsed before anything is written; a download that does not parse, or that
holds no event, writes nothing. A body equal to the latest kept body of the
same input (for a page: of the same URL) adds no line, and a fetch that
changes no file reports `unchanged`. The calendar's configuration (all but
its `url` and `pages`) must match the stored manifest's: another calendar
cannot be written under the same id.

Only `tradedesk-md-calendars` writes this area.

### Verifying (`verify`)

As for the rate series: `tradedesk-md-calendars verify` re-hashes each raw
file, rebuilds `events.csv`, `withdrawn.csv` and `manifest.json` from the raw
files and compares them byte for byte. It writes nothing, and it warns about
a raw file with no fetch line. Both commands take `--format text|json` and
select calendars with `--calendar ID ...`. The exit status is that of the
other reference-data tools: `0` when every calendar fetched or verified; `1`
when a calendar failed; `2` on a usage error, including a configuration that
is not valid.

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
