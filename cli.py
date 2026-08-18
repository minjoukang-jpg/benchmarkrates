"""Command line entry point for the rates database.

    python cli.py update            # daily job - fills any gap up to today
    python cli.py update --force    # write even if the anomaly guard trips
    python cli.py backfill          # one-off historical load, all curves
    python cli.py backfill --curve BVAL
    python cli.py status            # coverage and last-run summary
    python cli.py doctor            # diagnose a source that has stopped working
    python cli.py export            # write data/*.csv, the committed form
    python cli.py rebuild           # rebuild rates.db from data/*.csv
"""

import argparse
import datetime
import sys

import db
import sources

CFG = sources.CONFIG
LOOKBACK_DAYS = int(CFG.get("lookback_days", 45))

BVAL_HISTORY_START = datetime.date(2022, 1, 1)
SOFR_HISTORY_START = datetime.date(2018, 4, 1)
# BNM's benchmark-yields page serves a date parameter at least this far back.
BENCHMARK_HISTORY_START = datetime.date(2022, 1, 1)

# Curves fetched one trade date at a time, so a backfill walks weekdays.
PER_DAY_CURVES = ("BVAL", "MGS", "MGII", "THOR")


def _out(msg=""):
    print(msg, flush=True)


def _alarm_threshold(curve):
    return int(CFG.get("missed_weekdays_before_alarm", {}).get(curve, 3))


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _fetch(conn, curve, full):
    """Returns (Outcome, skipped_reason_or_None)."""
    today = datetime.date.today()

    if curve == "SOFR":
        start = SOFR_HISTORY_START if full else today - datetime.timedelta(days=LOOKBACK_DAYS)
        return sources.fetch_sofr(start, today), None

    if curve == "KLIBOR":
        if full:
            return sources.fetch_klibor(), None
        return sources.fetch_klibor(today - datetime.timedelta(days=LOOKBACK_DAYS), today), None

    if curve in sources.BNM_MYOR_URLS:
        start = (sources.MYOR_HISTORY_START if full
                 else today - datetime.timedelta(days=LOOKBACK_DAYS))
        return sources.fetch_myor(curve, start, today), None

    # THOR's daily path uses BOT rather than ThaiBMA: two requests cover the
    # overnight rate and the averages for every business day of two months,
    # where ThaiBMA would need one call per day and still return only today's
    # averages. ThaiBMA is still used for the deep overnight backfill.
    if curve == "THOR" and not full:
        return sources.fetch_thor_recent(today), None

    if curve in PER_DAY_CURVES:
        if full:
            start = {"BVAL": BVAL_HISTORY_START,
                     "THOR": sources.THOR_HISTORY_START}.get(curve, BENCHMARK_HISTORY_START)
        else:
            last = db.latest_date(conn, curve)
            start = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)) if last \
                else today - datetime.timedelta(days=LOOKBACK_DAYS)
        if start > today:
            return None, "already current"

        def progress(day, n, status):
            if full:
                _out(f"    {day} -> {n} tenors ({status})")

        if curve == "BVAL":
            return sources.fetch_bval_range(start, today, on_progress=progress), None
        if curve == "THOR":
            return sources.fetch_thor_range(start, today, on_progress=progress), None
        return sources.fetch_benchmark_range(curve, start, today, on_progress=progress), None

    raise ValueError(f"unknown curve {curve}")


def update_curve(conn, curve, full=False, force=False, quiet=False):
    """Fetch and store one curve. Returns rows written, or -1 on failure."""
    try:
        outcome, skipped = _fetch(conn, curve, full)
    except sources.FetchError as exc:
        db.log_fetch(conn, curve, "error", 0, str(exc)[:500])
        _out(f"  {curve:<8} FAILED: {exc}")
        return -1

    if skipped:
        db.log_fetch(conn, curve, "no_new_data", 0, skipped)
        if not quiet:
            _out(f"  {curve:<8} already current through {db.latest_date(conn, curve)}")
        return 0

    # -- the source could not be reached or could not be understood ---------
    if outcome.failed:
        db.log_fetch(conn, curve, "error", 0, outcome.detail[:500])
        _out(f"  {curve:<8} FAILED: {outcome.detail}")
        _out(f"           Run 'python cli.py doctor' for a full diagnosis.")
        return -1

    # -- the source answered fine but published nothing --------------------
    if outcome.status == sources.EMPTY:
        missed = db.missed_weekdays(conn, curve)
        limit = _alarm_threshold(curve)
        if missed is not None and missed > limit:
            msg = (f"nothing published for {missed} consecutive weekdays "
                   f"(tolerance {limit}). {outcome.detail}")
            db.log_fetch(conn, curve, "error", 0, msg[:500])
            _out(f"  {curve:<8} STALE: {msg}")
            _out(f"           Run 'python cli.py doctor' for a full diagnosis.")
            return -1
        db.log_fetch(conn, curve, "no_publication", 0, outcome.detail[:500])
        if not quiet:
            _out(f"  {curve:<8} {0:>6} new/changed   latest: {db.latest_date(conn, curve)}"
                 f"   (no publication - weekend or holiday)")
        return 0

    # -- data came back; make sure it has not changed shape ------------------
    anomaly = None if force else db.detect_anomaly(
        conn, curve, outcome.rows,
        min_overlap=int(CFG.get("anomaly_min_overlap", 20)),
        change_bp=float(CFG.get("anomaly_change_bp", 25)),
        max_fraction=float(CFG.get("anomaly_max_fraction", 0.30)))

    if anomaly:
        db.log_fetch(conn, curve, "error", 0, anomaly[:500])
        _out(f"  {curve:<8} BLOCKED - nothing written")
        _out(f"           {anomaly}")
        return -1

    written = db.upsert_rates(conn, curve, outcome.rows)
    note = f"{len(outcome.rows)} observations via {outcome.strategy}"
    if outcome.detail:
        note += f" ({outcome.detail})"
    db.log_fetch(conn, curve, "ok" if written else "no_new_data", written, note[:500])

    if not quiet:
        _out(f"  {curve:<8} {written:>6} new/changed   latest: {db.latest_date(conn, curve)}")
        # _try_strategies prefixes the detail with "recovered after" only when an
        # earlier strategy failed, which is a reliable signal. Guessing from the
        # strategy name is not - it flagged primary paths as fallbacks.
        if outcome.detail.startswith("recovered after"):
            _out(f"           note: primary path failed, recovered via "
                 f"'{outcome.strategy}'. {outcome.detail}")
        if outcome.degraded:
            _out(f"           WARNING: {outcome.detail}")
    return written


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_update(args):
    conn = db.ensure_database()
    curves = [args.curve] if args.curve else list(db.CURVES)
    _out(f"Rates update  {datetime.datetime.now():%Y-%m-%d %H:%M}")
    total, failed = 0, 0
    for curve in curves:
        n = update_curve(conn, curve, full=False, force=args.force)
        if n < 0:
            failed += 1
        else:
            total += n
    _out(f"Done. {total} row(s) written, {failed} source(s) failed.")
    return 1 if failed else 0


def cmd_backfill(args):
    conn = db.ensure_database()
    curves = [args.curve] if args.curve else list(db.CURVES)
    _out("Historical backfill (this takes a few minutes for BVAL)")
    for curve in curves:
        _out(f"\n{curve}:")
        update_curve(conn, curve, full=True, force=args.force)
    _out("\nBackfill complete.")
    return 0


def cmd_export(args):
    """Write the database out to the CSV files that get committed."""
    conn = db.ensure_database()
    written = db.write_csv_files(conn)
    _out(f"Wrote CSV seed files to {db.DATA_DIR}")
    for curve, n in written.items():
        _out(f"  {curve:<8} {n:>6} date row(s)")
    total = sum(written.values())
    _out(f"Done. {total} row(s) across {len([n for n in written.values() if n])} curve(s).")
    return 0


def cmd_rebuild(args):
    """Rebuild rates.db from the committed CSV files.

    Used on a fresh checkout, and by the daily workflow, because rates.db is not
    committed - the CSVs are. Rebuilding first means the anomaly guard still has
    history to compare incoming data against.
    """
    conn = db.ensure_database()
    if not db.csv_files_present():
        _out(f"No CSV files found in {db.DATA_DIR}. Nothing to rebuild from.")
        return 1
    loaded = db.read_csv_files(conn)
    _out(f"Rebuilt from {db.DATA_DIR}")
    for curve, n in loaded.items():
        _out(f"  {curve:<8} {n:>6} observation(s)")
    conn.close()          # checkpoint needs an exclusive lock, so let go first
    db.checkpoint()
    return 0


def cmd_status(args):
    conn = db.ensure_database()
    today = datetime.date.today()
    _out(f"{'Curve':<9}{'Tenors':>7}{'Rows':>9}  {'First':<12}{'Last':<12}{'Age':<7}Last run")
    _out("-" * 78)
    for curve in db.CURVES:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT tenor) t, MIN(rate_date) a, MAX(rate_date) b "
            "FROM rates WHERE curve=?", (curve,)).fetchone()
        log = conn.execute(
            "SELECT run_at, status FROM fetch_log WHERE curve=? ORDER BY id DESC LIMIT 1",
            (curve,)).fetchone()
        if not row["n"]:
            _out(f"{curve:<9}{'-':>7}{'0':>9}  {'(empty)':<12}")
            continue
        age = (today - datetime.date.fromisoformat(row["b"])).days
        last_run = f"{log['run_at'][:16]} {log['status']}" if log else "never"
        _out(f"{curve:<9}{row['t']:>7}{row['n']:>9}  {row['a']:<12}{row['b']:<12}"
             f"{str(age) + 'd':<7}{last_run}")

    problems = [c for c in db.CURVES if db.recent_failures(conn, c)]
    if problems:
        _out()
        _out(f"{len(problems)} source(s) failing: {', '.join(problems)}. "
             f"Run 'python cli.py doctor'.")
    return 0


def cmd_doctor(args):
    """Diagnose sources that have stopped working."""
    conn = db.ensure_database()
    curves = [args.curve] if args.curve else list(db.CURVES)
    _out(f"Rates doctor  {datetime.datetime.now():%Y-%m-%d %H:%M}")
    _out("=" * 78)

    bad = 0
    for curve in curves:
        meta = db.CURVES[curve]
        _out(f"\n{meta['label']}  ({meta['source']})")
        _out("-" * 78)

        last = db.latest_date(conn, curve)
        missed = db.missed_weekdays(conn, curve)
        limit = _alarm_threshold(curve)
        _out(f"  stored      : latest {last or 'nothing'}"
             + (f", {missed} weekday(s) since (tolerance {limit})" if missed is not None else ""))

        _out(f"  probing     : {meta['url']}")
        try:
            out = sources.probe(curve)
        except Exception as exc:                       # noqa: BLE001 - doctor must never crash
            _out(f"  result      : CRASHED - {type(exc).__name__}: {exc}")
            bad += 1
            continue

        if out.ok:
            dates = sorted({r[0] for r in out.rows})
            tenors = sorted({r[1] for r in out.rows}, key=db.tenor_sort_key)
            _out(f"  result      : OK via '{out.strategy}'")
            _out(f"  returned    : {len(out.rows)} observations, "
                 f"{len(dates)} date(s) {dates[0]} to {dates[-1]}")
            _out(f"  tenors      : {', '.join(tenors)}")
            if out.degraded or out.detail:
                _out(f"  note        : {out.detail}")
            if out.degraded:
                bad += 1
        elif out.status == sources.EMPTY:
            verdict = "reachable, nothing published"
            if missed is not None and missed > limit:
                verdict += f" - but {missed} weekdays with no data exceeds the tolerance"
                bad += 1
            else:
                verdict += " (normal at a weekend, on a public holiday, or before release)"
            _out(f"  result      : {verdict}")
            if out.detail:
                _out(f"  detail      : {out.detail}")
        else:
            _out(f"  result      : FAILED")
            _out(f"  detail      : {out.detail}")
            _out(f"  fix         : {_hint(curve, out.detail)}")
            bad += 1

        fails = db.recent_failures(conn, curve)
        if fails:
            _out(f"  recent log  : {len(fails)} consecutive failure(s), "
                 f"last at {fails[0]['run_at']}")
            _out(f"                {fails[0]['message']}")

    _out()
    _out("=" * 78)
    if bad:
        _out(f"{bad} source(s) need attention. See the fix line under each.")
    else:
        _out("All sources healthy.")
    return 1 if bad else 0


def _hint(curve, detail):
    d = (detail or "").lower()
    if curve == "BVAL":
        if "401" in d or "api key" in d:
            return "The PDEx key has rotated. Follow the steps in config.json (_pdex_api_key_help)."
        if "schema" in d or "bvalrates" in d:
            return "PDEx changed its GraphQL schema. Update _BVAL_QUERY in sources.py."
        return ("Check the network and proxy, then open "
                "https://www.pds.com.ph/wd-mp/php-bval-reference-rate-benchmark-tenors "
                "in a browser to confirm the site is up.")
    if curve == "KLIBOR":
        if "no table" in d or "header" in d:
            return ("BNM changed the page layout. Open the URL above, view source, and check "
                    "the <thead>/<tr> structure against parse_klibor_html() in sources.py.")
        return "Open the BNM URL above in a browser. If it loads, the parser needs updating."
    if curve in ("MYOR", "MYORI"):
        if "header" in d or "cannot be read by position" in d:
            return ("BNM changed the MYOR table layout. It has two date columns plus a "
                    "compounding index, so parse_myor_html() maps columns by header name "
                    "and refuses to guess. Open the URL above and compare the headers.")
        return "Open the BNM URL above in a browser. If it loads, the parser needs updating."
    if curve in ("MGS", "MGII"):
        if "header" in d or "no mgs or mgii" in d:
            return ("BNM changed the benchmark yields layout. Open the URL above and check the "
                    "table headers against parse_benchmark_yields() in sources.py. It reads the "
                    "Tenor and Close columns by name, so a renamed header is the likely cause.")
        return ("Open the BNM URL above in a browser with ?date=YYYY-MM-DD. If it loads, the "
                "parser needs updating.")
    if curve == "SOFR":
        return ("All three NY Fed endpoints failed, which usually means the network or the "
                "corporate proxy rather than the API. Check "
                "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json in a browser.")
    return "Open the source URL in a browser to see whether it is the site or the parser."


def main():
    p = argparse.ArgumentParser(description="Benchmark rates database")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in (("update", cmd_update), ("backfill", cmd_backfill),
                     ("status", cmd_status), ("doctor", cmd_doctor),
                     ("export", cmd_export), ("rebuild", cmd_rebuild)):
        sp = sub.add_parser(name)
        sp.set_defaults(func=fn)
        if name in ("update", "backfill", "doctor"):
            sp.add_argument("--curve", choices=list(db.CURVES),
                            help="limit to one curve (default: all)")
        if name in ("update", "backfill"):
            sp.add_argument("--force", action="store_true",
                            help="write even if the anomaly guard trips")

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
