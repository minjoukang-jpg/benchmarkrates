"""Local web app for the benchmark rates database.

    python serve.py            # http://127.0.0.1:8765
    python serve.py --port 9000

Binds to loopback only - nothing is exposed to the network.
"""

import argparse
import datetime
import io
import csv
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import db
import sources

HERE = os.path.dirname(os.path.abspath(__file__))

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "finished_at": None, "output": None}


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def get_meta(conn):
    out = []
    today = datetime.date.today()
    for curve, meta in db.CURVES.items():
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(rate_date) a, MAX(rate_date) b "
            "FROM rates WHERE curve=?", (curve,)).fetchone()
        tenors = [r["tenor"] for r in conn.execute(
            "SELECT DISTINCT tenor FROM rates WHERE curve=?", (curve,)).fetchall()]
        tenors.sort(key=db.tenor_sort_key)

        # Tenors still being published. BNM discontinued 2M and 12M KLIBOR in
        # Jan 2023, so the all-time tenor list overstates what is live today.
        active = [r["tenor"] for r in conn.execute(
            "SELECT DISTINCT tenor FROM rates WHERE curve=? AND rate_date=?",
            (curve, row["b"])).fetchall()] if row["b"] else []
        active.sort(key=db.tenor_sort_key)
        log = conn.execute(
            "SELECT run_at, status, message FROM fetch_log WHERE curve=? "
            "ORDER BY id DESC LIMIT 1", (curve,)).fetchone()

        age = None
        if row["b"]:
            age = (today - datetime.date.fromisoformat(row["b"])).days

        # A source is "failing" only when the daily job actually errored. A
        # quiet weekend logs 'no_publication', which is not a problem.
        fails = db.recent_failures(conn, curve)
        missed = db.missed_weekdays(conn, curve)
        tolerance = int(sources.CONFIG.get("missed_weekdays_before_alarm", {}).get(curve, 3))

        out.append({
            "curve": curve, "label": meta["label"], "currency": meta["currency"],
            "market": meta["market"], "description": meta["description"],
            "source": meta["source"], "url": meta["url"],
            "rows": row["n"], "first_date": row["a"], "last_date": row["b"],
            "tenors": tenors, "active_tenors": active, "age_days": age,
            # Counted in weekdays against a per-source tolerance, so a weekend
            # or a public holiday never reads as stale.
            "stale": missed is not None and missed > tolerance,
            "last_run": log["run_at"] if log else None,
            "last_status": log["status"] if log else None,
            "last_message": log["message"] if log else None,
            "failing": bool(fails),
            "fail_count": len(fails),
            "last_error": fails[0]["message"] if fails else None,
            "missed_weekdays": missed,
            "missed_tolerance": tolerance,
        })
    return out


def get_latest(conn, curve):
    """Newest published term structure, with the change vs the prior
    publication date for that same tenor."""
    last = db.latest_date(conn, curve)
    if not last:
        # Same keys as the populated case. A curve with no data yet is normal -
        # one just added, or one whose source has never published - and callers
        # should not have to special-case the shape.
        return {"curve": curve, "date": None, "rows": [], "headlines": []}

    def _change(tenor, rate, before):
        """Change against the previous publication of this same tenor."""
        prev = conn.execute(
            "SELECT rate_date, rate FROM rates WHERE curve=? AND tenor=? AND rate_date<? "
            "ORDER BY rate_date DESC LIMIT 1", (curve, tenor, before)).fetchone()
        return {
            "prev_rate": prev["rate"] if prev else None,
            "prev_date": prev["rate_date"] if prev else None,
            "change_bp": round((rate - prev["rate"]) * 100, 1) if prev else None,
        }

    rows = []
    for r in conn.execute(
            "SELECT tenor, rate FROM rates WHERE curve=? AND rate_date=?",
            (curve, last)).fetchall():
        # as_of is None when the value belongs to the card's own date, which is
        # the normal case. See the carry-forward below for when it is not.
        rows.append({"tenor": r["tenor"], "rate": r["rate"], "as_of": None,
                     **_change(r["tenor"], r["rate"], last)})

    # Sources do not always publish every tenor of a curve on the same day. The
    # NY Fed routinely has the SOFR averages out for a date before overnight
    # SOFR itself, and dropping the tenor would make the headline rate vanish
    # from the card for a few hours. Carry the last published value instead,
    # but only for declared headline tenors, and stamp it with its own date so
    # the card never implies it belongs to the date in the heading.
    present = {r["tenor"] for r in rows}
    for tenor in db.CURVES.get(curve, {}).get("headline_tenors") or []:
        if tenor in present:
            continue
        held = conn.execute(
            "SELECT rate_date, rate FROM rates WHERE curve=? AND tenor=? AND rate_date<? "
            "ORDER BY rate_date DESC LIMIT 1", (curve, tenor, last)).fetchone()
        if not held:
            continue
        rows.append({"tenor": tenor, "rate": held["rate"],
                     "as_of": held["rate_date"],
                     **_change(tenor, held["rate"], held["rate_date"])})

    rows.sort(key=lambda x: db.tenor_sort_key(x["tenor"]))
    return {"curve": curve, "date": last, "rows": rows,
            # Resolved server-side so the local and hosted dashboards always
            # show the same headline figures. A list: a market often quotes
            # several tenors together.
            "headlines": db.headline_tenors(curve, [r["tenor"] for r in rows])}


# A plotted line is identified by curve and tenor together, because "3M" alone
# is ambiguous once KLIBOR, MYOR and THOR can share a chart.
SERIES_SEP = "|"


def series_key(curve, tenor):
    return f"{curve}{SERIES_SEP}{tenor}"


def get_series_pairs(conn, pairs, start, end):
    """Aligned time series for any mix of curves and tenors.

    A shared date axis with None for gaps, so the front end never has to
    reconcile differing publication calendars. That matters more here than for
    a single curve: a Philippine holiday, a Malaysian one and a US one fall on
    different days, so a union axis will always be sparser than any one source.

    Returns labels alongside the data because "3M" on its own does not say
    whose 3M it is once several markets are on the same chart.
    """
    if not pairs:
        return {"dates": [], "series": {}, "labels": {}}

    collected, all_dates = {}, set()
    for curve, tenor in pairs:
        rows = conn.execute(
            "SELECT rate_date, rate FROM rates WHERE curve=? AND tenor=? "
            "AND rate_date BETWEEN ? AND ? ORDER BY rate_date",
            (curve, tenor, start, end)).fetchall()
        values = {r["rate_date"]: r["rate"] for r in rows}
        collected[series_key(curve, tenor)] = values
        all_dates.update(values)

    dates = sorted(all_dates)
    return {
        "dates": dates,
        "series": {key: [vals.get(d) for d in dates] for key, vals in collected.items()},
        "labels": {series_key(c, t): f"{db.CURVES[c]['label']} {t}" for c, t in pairs},
    }


def get_series(conn, curve, tenors, start, end):
    """Single-curve view, keyed by plain tenor. Kept as-is: the term structure
    panel and the existing callers have no use for the curve prefix."""
    if not tenors:
        return {"dates": [], "series": {}}
    full = get_series_pairs(conn, [(curve, t) for t in tenors], start, end)
    return {"dates": full["dates"],
            "series": {t: full["series"][series_key(curve, t)] for t in tenors}}


def parse_series_spec(spec):
    """"SOFR:90D,KLIBOR:3M" -> [("SOFR", "90D"), ("KLIBOR", "3M")].

    Unknown curves are dropped rather than raising: a stale bookmark naming a
    curve that has since been renamed should draw the rest of the chart, not
    fail the request.
    """
    pairs = []
    for item in (spec or "").split(","):
        curve, sep, tenor = item.partition(":")
        if sep and curve in db.CURVES and tenor and (curve, tenor) not in pairs:
            pairs.append((curve, tenor))
    return pairs


def get_curve_shape(conn, curve, date):
    """Term structure on the latest date at or before `date`."""
    row = conn.execute(
        "SELECT MAX(rate_date) d FROM rates WHERE curve=? AND rate_date<=?",
        (curve, date)).fetchone()
    if not row or not row["d"]:
        return {"date": None, "points": []}
    actual = row["d"]
    points = [{"tenor": r["tenor"], "rate": r["rate"], "months": db.tenor_sort_key(r["tenor"])}
              for r in conn.execute(
                  "SELECT tenor, rate FROM rates WHERE curve=? AND rate_date=?",
                  (curve, actual)).fetchall()]
    points.sort(key=lambda p: p["months"])
    return {"date": actual, "points": points}


# The CSV writer lives in db.py so that the web download, the committed seed
# files and the command-line export are all byte-identical by construction.
export_csv = db.export_csv


# --------------------------------------------------------------------------
# Refresh (runs the same cli.py the scheduled task runs)
# --------------------------------------------------------------------------

def _run_refresh():
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "cli.py"), "update"],
            capture_output=True, text=True, timeout=900, cwd=HERE)
        _refresh_state["output"] = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        _refresh_state["output"] = f"Refresh failed: {exc}"
    finally:
        _refresh_state["running"] = False
        _refresh_state["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")


def start_refresh():
    with _refresh_lock:
        if _refresh_state["running"]:
            return False
        _refresh_state.update(running=True, output=None, finished_at=None)
    threading.Thread(target=_run_refresh, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RatesDB/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console clean; failures still surface in the browser

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def _file(self, name, ctype):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if route in ("/", "/index.html"):
            return self._file("dashboard.html", "text/html; charset=utf-8")

        if not route.startswith("/api/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")

        conn = db.connect()
        try:
            if route == "/api/meta":
                return self._json({
                    "curves": get_meta(conn),
                    # Grouped left-to-right by market, with an inline SVG flag,
                    # a column span and money-market/government sub-groups, so
                    # both dashboards lay out identically.
                    "markets": db.market_layout(),
                    "today": datetime.date.today().isoformat(),
                    "refresh": _refresh_state,
                })

            if route == "/api/latest":
                curve = q.get("curve")
                if curve not in db.CURVES:
                    return self._json({"error": "unknown curve"}, 400)
                return self._json(get_latest(conn, curve))

            if route == "/api/series":
                start = q.get("start") or "1900-01-01"
                end = q.get("end") or datetime.date.today().isoformat()
                # Cross-curve form, used by the history chart so SOFR can be
                # plotted against BVAL, MGS and the rest on one axis.
                if q.get("series"):
                    pairs = parse_series_spec(q["series"])
                    return self._json(get_series_pairs(conn, pairs, start, end))
                curve = q.get("curve")
                if curve not in db.CURVES:
                    return self._json({"error": "unknown curve"}, 400)
                tenors = [t for t in (q.get("tenors") or "").split(",") if t]
                return self._json(get_series(conn, curve, tenors, start, end))

            if route == "/api/shape":
                curve = q.get("curve")
                if curve not in db.CURVES:
                    return self._json({"error": "unknown curve"}, 400)
                date = q.get("date") or datetime.date.today().isoformat()
                return self._json(get_curve_shape(conn, curve, date))

            if route == "/api/export":
                curve = q.get("curve")
                if curve not in db.CURVES:
                    return self._json({"error": "unknown curve"}, 400)
                start = q.get("start") or "1900-01-01"
                end = q.get("end") or datetime.date.today().isoformat()
                body = export_csv(conn, curve, start, end)
                fname = f"{curve}_{start}_to_{end}.csv"
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fname}"'})

            return self._json({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/refresh":
            return self._json({"error": "unknown endpoint"}, 404)
        started = start_refresh()
        return self._json({"started": started, "state": _refresh_state})


def main():
    ap = argparse.ArgumentParser(description="Rates database web app")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db.ensure_database()  # rebuild from data/ if rates.db is not there
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Rates database running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
