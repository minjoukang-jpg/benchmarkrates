# Benchmark Rates Database

A local database and dashboard for the base interest rates of our key markets.

| Market | Currency | Benchmark | Tenors held | History from |
|---|---|---|---|---|
| Philippines | PHP | BVAL | 1M, 3M, 6M, 1Y, 2Y, 3Y, 4Y, 5Y, 7Y, 10Y, 20Y, 25Y | 15 Aug 2022 |
| Malaysia | MYR | KLIBOR | 1M, 3M, 6M (plus discontinued 2M, 9M, 12M) | 5 Jan 2007 |
| Malaysia | MYR | MYOR | O/N, 1M, 3M, 6M | 24 Sep 2021 |
| Malaysia | MYR | MYOR-i (Islamic) | O/N, 1M, 3M, 6M | 25 Mar 2022 |
| Malaysia | MYR | MGS | 3Y, 5Y, 7Y, 10Y | Jan 2022 |
| Malaysia | MYR | MGII (Islamic) | 3Y, 5Y, 7Y, 10Y | Jan 2022 |
| United States | USD | SOFR | Overnight | 2 Apr 2018 |

**MYOR is not a drop-in replacement for KLIBOR, and the gap is large.** On
13 Aug 2026 the 3M rates were MYOR 2.761% against KLIBOR 3.460% - about **70
basis points apart**, and 6M was similar. That is structural, not noise: MYOR is
computed from actual overnight transactions and tracks the policy rate, while
KLIBOR is submission-based and embeds bank credit and term premium. So a facility
that switches its reference from KLIBOR to MYOR needs its margin re-cut by
roughly that much, or the economics change materially. Both are held here so the
spread can be measured on any date rather than assumed. The 1M, 3M and 6M MYOR
figures are **backward-looking compounded averages**, not forward-looking term
rates, so they are not directly comparable to a KLIBOR fixing even before the
level difference.

**MYOR-i** is the Shariah-compliant equivalent, drawn from Islamic money market
transactions. It tracks MYOR closely: 1M was 2.755% against 2.753% on the same
date. For Sukuk floating-rate pricing this is the correct overnight benchmark,
as MGII is for the yield curve.

**MGS vs MGII, and which to use for Sukuk.** Both are Malaysian government
paper published by BNM on the same page. MGS is conventional; **MGII
(Government Investment Issues) is the Shariah-compliant one and is the correct
benchmark for Sukuk.** They track each other closely, usually within a few
basis points, and MGS is often used as a proxy because it is more liquid. Both
are held here so you can price against the right one and see the spread. The
stored figure is the **closing trading yield**.

Everything runs on this machine. The fetcher, database and local dashboard use
the Python standard library only, so there is nothing to `pip install` and no
admin rights are needed.

The **hosted version** at
<https://github.com/minjoukang-jpg/benchmarkrates> is the one colleagues use.
`streamlit_app.py` runs on Streamlit Cloud, with GitHub Actions doing the daily
fetch, so no laptop needs to be on. It reads the same database and reuses the
same data-access code as the local app, so the two cannot drift apart. See
[DEPLOY.md](DEPLOY.md) for how it is wired together.

**Do not delete `streamlit_app.py` or `requirements.txt`.** Streamlit Cloud runs
the hosted site directly from those two files.

Streamlit is not installed on this machine. It does not need to be: Streamlit
Cloud installs `requirements.txt` itself. To preview hosted changes locally
before uploading them, install it temporarily with
`pip install --user -r requirements.txt` and run
`python -m streamlit run streamlit_app.py`.

## Daily use

The hosted dashboard is the everyday one. The local app below is an offline
fallback that keeps working with no internet and no external service.

Double-click **`open_app.cmd`**, or run:

```bash
python serve.py
```

The dashboard opens at <http://127.0.0.1:8765>. It binds to loopback only, so
it is not reachable from the network. Close the console window to stop it.

The dashboard gives you:

- **Latest rates per market** with the change in basis points against the
  previous publication date, and an age badge that turns red if a source stops
  updating.
- **History** - any tenor over 1M to Max, hover for values on a date, with a
  table view and CSV export (wide format, one column per tenor, paste-ready).
- **Term structure** - the latest curve against an earlier date, to show how
  the shape has moved.
- **Refresh now** - runs the same update the scheduled job runs.

## Automatic daily updates

**Updates run on GitHub, not on this laptop.** The workflow in
`.github/workflows/daily-update.yml` runs at **09:30 Manila and Kuala Lumpur
time on weekdays**, fetches the three sources, runs the contract tests, and
commits the refreshed `rates.db` back to the repository. Streamlit Cloud
redeploys on that commit, so the hosted dashboard picks it up automatically.

Nothing needs to be switched on here. The Windows scheduled task that used to
do this was removed once GitHub Actions took over, so that only one thing
writes to the database.

To watch a run or trigger one by hand, use the **Actions** tab of the
repository.

The update is **idempotent** - running it twice writes nothing the second time,
and it re-checks the previous 45 days each run so a late publication or a
revision is picked up rather than missed permanently.

### Running the update locally instead

Still possible, and useful for testing or if you ever stop hosting. This
updates the local `rates.db` only; it does not touch GitHub.

```bash
python cli.py update
```

To bring the Windows scheduled task back:

```bash
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Time 09:00
```

Do not run both at once. Two schedules writing the same database is how you get
conflicting copies.

## Command line

```bash
python cli.py status                   # coverage and last-run summary
python cli.py doctor                   # diagnose a source that stopped working
python cli.py update                   # same job the scheduler runs
python cli.py update --curve SOFR      # one source only
python cli.py backfill --curve BVAL    # re-pull full history (slow for BVAL)
python cli.py export                   # write data/*.csv from the database
python cli.py rebuild                  # rebuild the database from data/*.csv
python tests\test_contract.py          # check nothing has drifted
```

## How the data is stored

Two representations, and it matters which is which:

- **`data/*.csv` is the source of truth in the repository.** One file per curve,
  wide format, the same format the download button produces.
- **`rates.db` is a local build artefact.** It is gitignored and rebuilt from the
  CSVs on demand. A fresh checkout has no database until you run
  `python cli.py rebuild`.

The hosted app builds the database in memory at startup from the CSVs, which
takes about a quarter of a second for 49,000 observations.

**Why not just commit `rates.db`?** It was tried and it broke. A 9 MB SQLite
binary pushed through GitHub's web uploader arrived corrupted, and the app died
with `sqlite3.DatabaseError` on its first query. Text survives git and browser
uploads intact, is a twentieth of the size (0.4 MB against 8.6 MB), and can be
diffed so you can see what a daily update actually changed. The round trip is
covered by tests that assert it is exactly lossless, including that an empty cell
stays empty rather than becoming a zero.

After changing data locally, run `python cli.py export` before committing, or the
repository will still hold the old numbers.

## When a source changes or breaks

These are public websites and APIs. They will change without warning, so the
app is built to fail in a way you can act on rather than to fail silently.

### Empty is not the same as broken

This is the distinction the whole design turns on. Every fetch ends in one of
three states:

| State | Meaning | Alerts? |
|---|---|---|
| OK | Data returned and passed validation | No |
| EMPTY | Source answered correctly, published nothing | No |
| FAILED | Unreachable, unparsable, or failed validation | Yes |

BVAL is legitimately EMPTY every weekend, on every PH public holiday, and each
morning before PDEx releases. Treating that as an error would cry wolf daily,
so it is logged as `no_publication` and stays silent.

To catch a source that has genuinely gone quiet without needing a holiday
calendar, the app counts **consecutive weekdays with no data** and only alarms
past a per-source tolerance (3 for BVAL and KLIBOR, 4 for SOFR because it
publishes a day in arrears). A weekend never counts. A two-day holiday does not
breach the tolerance. A source that has actually stopped does, within a week.

### Fallback paths

If the usual route fails, each source tries alternatives before giving up, and
the log records which route was used, so a silent fallback is never invisible.

| Curve | Primary | Fallbacks |
|---|---|---|
| SOFR | `search.json` for the date range | `last/N.json`, then `all/latest.json` |
| KLIBOR | Date-range query | Full table, filtered locally |
| BVAL | PDEx GraphQL | None available (see below) |

BVAL has no fallback because PDEx requires an API key that is not exposed on
any public page, so it cannot be rediscovered automatically. Instead the key
lives in `config.json` and the app tells the difference between causes: HTTP
401 means the key rotated and prints the exact steps to get a new one, while a
missing `bvalRates` field means the GraphQL schema changed.

### Guards against wrong data

A source that breaks loudly is easy. The dangerous case is one that returns
HTTP 200 with the wrong numbers. Three defences, in order of importance:

1. **Header-driven parsing.** KLIBOR columns are mapped from BNM's own
   `<thead>` labels, not from position. If BNM reorders, adds, or removes a
   tenor column, values still land on the right tenor. If the header vanishes
   the app falls back to the documented order but flags the run as degraded.
2. **Validation.** Every row is checked before it is stored: the tenor must be
   one this curve actually has, the rate must be a plausible percentage
   (0 to 30), and the date must parse and not be in the future. A source that
   switched from percent to basis points is refused, not stored.
3. **Anomaly guard.** Each run re-fetches the previous 45 days, so most
   incoming rows should already match what is stored. If more than 30% of that
   overlap disagrees by more than 25bp, the write is blocked and reported
   rather than overwriting good history. Override with `--force` once you have
   confirmed the new values are right. Thresholds are in `config.json`.

**Known limit of the anomaly guard:** it cannot detect a swap between two
tenors whose rates are nearly equal. KLIBOR 3M and 6M currently sit 3bp apart,
so swapping those columns would move the numbers too little for any threshold
to notice. Defence 1 is what actually prevents that, which is why parsing is
driven off the header. This is deliberate and is covered by a test.

### If something does break

The dashboard shows a red banner naming the source, the last error, and the
command to run. Nothing is hidden and stored data is never discarded, so the
last good values keep showing while a source is down.

```bash
python cli.py doctor
```

`doctor` probes each source live, reports which strategy worked, what came
back, and for a failure prints a specific fix rather than a stack trace.

`cli.py update` also exits non-zero on failure, so a broken source turns the
**GitHub Actions run red**. Watch the Actions tab, or turn on GitHub's
notifications for failed workflows to get an email. That is now the earliest
warning that something upstream has changed.

### The output format will not change

`tests/test_contract.py` pins the CSV export byte-for-byte against golden
fixtures in `tests/golden/`, along with the database columns, the primary key,
and the API response keys. If a future change would alter the CSV structure the
tests fail. Run them after any edit:

```bash
python tests\test_contract.py
```

## Where the data comes from

| Curve | Source | How |
|---|---|---|
| BVAL | PDEx, `pds.com.ph` | GraphQL API, one request per trade date |
| KLIBOR | Bank Negara Malaysia FMIP | Server-rendered table, parsed from HTML |
| MYOR / MYOR-i | Bank Negara Malaysia FMIP | Separate pages, one request per date window |
| MGS / MGII | Bank Negara Malaysia FMIP | Benchmark yields page, one request per trade date, serving both curves |
| SOFR | Federal Reserve Bank of New York | Public JSON API |

MGS and MGII come from the same page, so one request fills both curves. The
response is memoised per date to avoid fetching it twice.

Rates are stored exactly as published - no interpolation, no adjustment, no
business-day rolling. The number in the database is the number the source
printed.

## Things to know about the data

- **KLIBOR 2M and 12M were discontinued by BNM in January 2023**, and 9M is not
  currently published either. The dashboard marks these tenors `· past` - the
  history is there, but they are not live. Malaysia is also transitioning from
  KLIBOR toward **MYOR**; if MYOR becomes the reference for our deals it can be
  added as a fourth curve (see below).
- **BVAL history starts 15 Aug 2022** because that is as far back as the PDEx
  API serves. Earlier BVAL data exists in the SharePoint year files used by the
  `bval-tracker` skill (2022–2025) and could be imported if you need it.
- **PDEx returns nothing on PH holidays**, which the fetcher treats as "no
  publication" and skips. That is why `update.log` reports weekdays with no
  publication - it is normal, not an error.
- **SOFR is published one business day in arrears**, so a 2-day age on the USD
  card is expected, not stale.
- **MYOR and MYOR-i are published one business day after the reference date.**
  The stored date is the reference date, not the publication date, which is why
  the parser reads that column by name - the table carries both, and taking the
  wrong one would shift every rate by a day. Their alarm tolerance is 4 weekdays
  rather than 3 to allow for the lag.
- **MYOR's table contains a compounding Index around 1.14 and a volume in the
  tens of thousands.** Neither is a rate. The index in particular sits inside the
  plausible range for a percentage, so the rate validator cannot catch it if the
  columns are misread - only the header mapping prevents it. There are tests for
  exactly this.
- **A starred MGS or MGII yield means no trade occurred that day.** BNM still
  publishes the close, so the value is stored and the marker stripped. It is an
  indicative level rather than a traded one, which matters if you are quoting a
  precise spread off a thin tenor.
- **Only four MGS and MGII tenors are published** as official benchmarks: 3Y,
  5Y, 7Y and 10Y. BNM's `ytm-matrix` and `indicative-yield-to-maturity` pages
  carry a fuller curve if you ever need points in between; the schema already
  allows 15Y, 20Y and 30Y in case BNM adds those benchmarks.
- **MGS, MGII and KLIBOR carry the previous close on Malaysian public
  holidays.** BNM returns a row for every weekday, repeating the last traded
  level rather than leaving a gap. Verified: 1 May 2026 (Labour Day) returns
  30 April's yields unchanged. So a flat stretch in the history may be a holiday
  rather than a genuinely unchanged market, and MGS or MGII will never look
  stale on a Malaysian holiday the way BVAL does on a Philippine one.
- **CME Term SOFR (1M/3M/6M/12M) is deliberately not included.** Those are the
  forward-looking rates that appear in loan documentation, but they are
  licensed by CME and redistributing them in an internal tool likely needs a
  licence. The schema will hold them if that gets cleared.

Treat these figures as an internal reference. Confirm against the primary
source before using a number in documentation or pricing.

## Adding another benchmark

1. Add an entry to `CURVES` in `db.py`.
2. Add a fetcher in `sources.py` returning `(date_iso, tenor, rate)` tuples.
3. Add a branch in `update_curve()` in `cli.py`.

No schema migration is needed - the tables are keyed on curve name.

## Files

| File | Purpose |
|---|---|
| `data/*.csv` | **The committed data**, one file per curve. This is the source of truth in the repository |
| `rates.db` | Local SQLite build, rebuilt from `data/` with `cli.py rebuild`. Not committed |
| `db.py` | Schema, curve registry, storage helpers |
| `sources.py` | One fetcher per benchmark |
| `cli.py` | `update` / `backfill` / `status` |
| `serve.py` | Local web server and JSON API |
| `dashboard.html` | The dashboard UI |
| `streamlit_app.py` | The hosted dashboard (Streamlit Cloud) |
| `DEPLOY.md` | How to publish it and share it with colleagues |
| `.github/workflows/daily-update.yml` | Daily fetch on GitHub's servers, replaces the Windows task once hosted |
| `requirements.txt` | Dependencies for the hosted version only |
| `config.json` | Tolerances and thresholds. No secrets - safe to commit |
| `run_daily.cmd` | Local daily update, if you ever re-enable the Windows task |
| `install_task.ps1` | Registers/removes the Windows task (currently not registered) |
| `open_app.cmd` | Starts the local dashboard |
| `update.log` | Rolling log of daily runs |
| `tests/test_contract.py` | Format and resilience tests |
| `tests/golden/` | Reference CSV exports the tests compare against |
