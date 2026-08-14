"""SQLite storage for the benchmark rates database.

One row per (curve, date, tenor). Stdlib only - no pip install required.
"""

import csv
import io
import sqlite3
import datetime
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "rates.db")

# The committed representation of the data. Plain CSV rather than the SQLite
# file: text survives git and web uploads intact, is a fifth of the size, and
# can be diffed. rates.db is a local build artefact rebuilt from these.
DATA_DIR = os.path.join(HERE, "data")

# Curve registry. Adding a new benchmark (e.g. MYOR) means adding a line here
# plus a fetcher in sources.py - no schema migration needed.
CURVES = {
    "BVAL": {
        "currency": "PHP",
        "market": "Philippines",
        "label": "PHP BVAL",
        "description": "Bloomberg Valuation benchmark tenors for PHP government securities",
        "source": "PDEx (pds.com.ph)",
        "url": "https://www.pds.com.ph/",
    },
    "KLIBOR": {
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR KLIBOR",
        "description": "Kuala Lumpur Interbank Offered Rate",
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-klibor",
    },
    "MYOR": {
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MYOR",
        "description": ("Malaysia Overnight Rate, the transaction-based benchmark BNM is "
                        "transitioning to from KLIBOR, with compounded 1M, 3M and 6M averages"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-myor",
    },
    "MYORI": {
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MYOR-i (Islamic)",
        "description": ("Malaysia Overnight Rate-i, the Shariah-compliant equivalent of MYOR, "
                        "based on Islamic money market transactions"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-myori",
    },
    "MGS": {
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MGS",
        "description": "Malaysian Government Securities benchmark closing yields (conventional)",
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/benchmark-yields",
    },
    "MGII": {
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MGII (Islamic)",
        "description": ("Malaysian Government Investment Issues benchmark closing yields. "
                        "This is the Shariah-compliant benchmark, and the correct reference "
                        "for Sukuk rather than conventional MGS"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/benchmark-yields",
    },
    "SOFR": {
        "currency": "USD",
        "market": "United States",
        "label": "USD SOFR",
        "description": "Secured Overnight Financing Rate (overnight, published rate)",
        "source": "Federal Reserve Bank of New York",
        "url": "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json",
    },
}

# Sort weight for tenors, expressed in months. Used for ordering term structures.
TENOR_MONTHS = {
    "O/N": 1.0 / 30, "1W": 0.25, "2W": 0.5,
    "1M": 1, "2M": 2, "3M": 3, "6M": 6, "9M": 9, "12M": 12,
    "1Y": 12, "2Y": 24, "3Y": 36, "4Y": 48, "5Y": 60,
    "7Y": 84, "10Y": 120, "20Y": 240, "25Y": 300,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rates (
    curve       TEXT NOT NULL,
    rate_date   TEXT NOT NULL,          -- ISO YYYY-MM-DD
    tenor       TEXT NOT NULL,
    rate        REAL NOT NULL,          -- percent, e.g. 4.7396
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (curve, rate_date, tenor)
);

CREATE INDEX IF NOT EXISTS idx_rates_curve_date ON rates (curve, rate_date DESC);
CREATE INDEX IF NOT EXISTS idx_rates_lookup     ON rates (curve, tenor, rate_date);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    curve       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | error | no_new_data
    rows_written INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_curve ON fetch_log (curve, run_at DESC);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # read-only filesystem (Streamlit Cloud); reads work regardless
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(path=DB_PATH):
    """Open without any possibility of writing.

    Used by the hosted dashboard, where the database ships with the deployment
    and the filesystem is read-only. Opening read-only also avoids creating the
    -wal and -shm sidecar files, which a read-only host cannot write.
    """
    uri = "file:" + str(path).replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def checkpoint(path=DB_PATH):
    """Fold the write-ahead log back into the main file and leave it in a form
    that can be committed to git and read on a read-only host.

    Switching out of WAL mode needs an exclusive lock, so it fails if anything
    else has the database open - the local dashboard, typically. That is not
    worth crashing over: the data is already committed either way, and the
    journal-mode switch only matters before publishing the file. Returns True if
    the mode was switched, False if something else held the database.
    """
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # works alongside readers
        conn.commit()
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            print(f"  note: could not switch journal mode ({exc}). The data is "
                  f"safely committed. Close the local dashboard and re-run if you "
                  f"are about to publish rates.db.")
            return False
    finally:
        conn.close()


def init(path=DB_PATH):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_rates(conn, curve, rows):
    """rows: iterable of (rate_date_iso, tenor, rate_float).

    Returns the number of rows that were new or changed. Re-running with
    identical data is a no-op, which makes the daily job safe to run twice.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    changed = 0
    cur = conn.cursor()
    for rate_date, tenor, rate in rows:
        if rate is None:
            continue
        existing = cur.execute(
            "SELECT rate FROM rates WHERE curve=? AND rate_date=? AND tenor=?",
            (curve, rate_date, tenor),
        ).fetchone()
        if existing is not None and abs(existing["rate"] - rate) < 1e-9:
            continue
        cur.execute(
            "INSERT INTO rates (curve, rate_date, tenor, rate, fetched_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(curve, rate_date, tenor) "
            "DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at",
            (curve, rate_date, tenor, rate, now),
        )
        changed += 1
    conn.commit()
    return changed


def log_fetch(conn, curve, status, rows_written=0, message=None):
    conn.execute(
        "INSERT INTO fetch_log (run_at, curve, status, rows_written, message) VALUES (?,?,?,?,?)",
        (datetime.datetime.now().isoformat(timespec="seconds"), curve, status,
         rows_written, message),
    )
    conn.commit()


def latest_date(conn, curve):
    row = conn.execute(
        "SELECT MAX(rate_date) AS d FROM rates WHERE curve=?", (curve,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def tenor_sort_key(tenor):
    return TENOR_MONTHS.get(tenor, 9999)


# --------------------------------------------------------------------------
# CSV representation - what actually gets committed
# --------------------------------------------------------------------------

def export_csv(conn, curve, start, end):
    """Wide format - one row per date, one column per tenor. Paste-ready.

    This is the single CSV formatter: the browser download, the committed seed
    files under data/, and `cli.py export` all go through it, so they cannot
    drift from one another. tests/test_contract.py pins the output
    byte-for-byte.
    """
    rows = conn.execute(
        "SELECT rate_date, tenor, rate FROM rates WHERE curve=? "
        "AND rate_date BETWEEN ? AND ? ORDER BY rate_date DESC", (curve, start, end)).fetchall()
    tenors = sorted({r["tenor"] for r in rows}, key=tenor_sort_key)
    table = {}
    for r in rows:
        table.setdefault(r["rate_date"], {})[r["tenor"]] = r["rate"]

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date"] + tenors)
    for date in sorted(table, reverse=True):
        w.writerow([date] + [table[date].get(t, "") for t in tenors])
    return buf.getvalue()


def parse_csv(text):
    """Inverse of export_csv. Returns [(date, tenor, rate)]."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return []
    header = next(csv.reader([lines[0]]))
    tenors = header[1:]
    out = []
    for line in lines[1:]:
        cells = next(csv.reader([line]))
        if not cells or not cells[0]:
            continue
        date = cells[0]
        for tenor, cell in zip(tenors, cells[1:]):
            if cell == "":
                continue
            out.append((date, tenor, float(cell)))
    return out


def write_csv_files(conn, data_dir=DATA_DIR):
    """Write one CSV per curve. Returns {curve: rows_written}."""
    os.makedirs(data_dir, exist_ok=True)
    written = {}
    for curve in CURVES:
        text = export_csv(conn, curve, "1900-01-01", "2999-12-31")
        if text.count("\n") <= 1:          # header only: nothing stored yet
            written[curve] = 0
            continue
        path = os.path.join(data_dir, f"{curve}.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        written[curve] = text.count("\n") - 1
    return written


def bulk_insert(conn, curve, rows):
    """Insert without the per-row comparison upsert_rates does.

    upsert_rates checks each row against what is stored so it can report how
    much actually changed, which costs a SELECT per row. A rebuild from CSV has
    nothing to compare against and runs tens of thousands of rows, so it uses
    executemany instead - roughly two orders of magnitude faster, which matters
    because the hosted app rebuilds at startup.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO rates (curve, rate_date, tenor, rate, fetched_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(curve, rate_date, tenor) "
        "DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at",
        [(curve, d, t, r, now) for d, t, r in rows if r is not None])
    conn.commit()
    return len(rows)


def read_csv_files(conn, data_dir=DATA_DIR):
    """Load every curve CSV into the database. Returns {curve: rows_loaded}."""
    loaded = {}
    for curve in CURVES:
        path = os.path.join(data_dir, f"{curve}.csv")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            rows = parse_csv(fh.read())
        if rows:
            bulk_insert(conn, curve, rows)
        loaded[curve] = len(rows)
    return loaded


def csv_files_present(data_dir=DATA_DIR):
    return bool(os.path.isdir(data_dir)
                and [f for f in os.listdir(data_dir) if f.endswith(".csv")])


# --------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------

def detect_anomaly(conn, curve, rows, min_overlap=20, change_bp=25, max_fraction=0.30):
    """Guard against a source silently changing shape.

    Every run re-fetches the previous few weeks, so most incoming rows should
    already be in the database with identical values. If a large share of that
    overlap suddenly disagrees, something structural has changed upstream - BNM
    reordering its columns, a source switching from percent to basis points -
    and overwriting would corrupt good history.

    Genuine revisions do happen (the NY Fed revises SOFR occasionally) but they
    affect a day or two, not a third of a month. Returns None if the data looks
    fine, otherwise a message explaining what tripped the guard.
    """
    cur = conn.cursor()
    overlap = 0
    moved = []
    for rate_date, tenor, rate in rows:
        row = cur.execute(
            "SELECT rate FROM rates WHERE curve=? AND rate_date=? AND tenor=?",
            (curve, rate_date, tenor)).fetchone()
        if row is None:
            continue
        overlap += 1
        diff_bp = abs(row["rate"] - rate) * 100
        if diff_bp > change_bp:
            moved.append((rate_date, tenor, row["rate"], rate, diff_bp))

    if overlap < min_overlap or not moved:
        return None

    fraction = len(moved) / overlap
    if fraction <= max_fraction:
        return None

    sample = "; ".join(
        f"{d} {t}: stored {old:.4f} -> incoming {new:.4f} ({bp:.0f}bp)"
        for d, t, old, new, bp in moved[:3])
    return (f"{len(moved)} of {overlap} re-fetched {curve} observations "
            f"({fraction:.0%}) disagree with what is stored by more than {change_bp}bp. "
            f"This looks like an upstream format change rather than a revision, so "
            f"nothing was written. Examples: {sample}. "
            f"Check the source, then re-run with --force if the new values are correct.")


def missed_weekdays(conn, curve, today=None):
    """Weekdays elapsed since this curve last published anything.

    Counting weekdays rather than calendar days means a normal weekend never
    registers, and a run of public holidays only pushes the count up slowly.
    """
    last = latest_date(conn, curve)
    if not last:
        return None
    today = today or datetime.date.today()
    day = datetime.date.fromisoformat(last) + datetime.timedelta(days=1)
    count = 0
    while day <= today:
        if day.weekday() < 5:
            count += 1
        day += datetime.timedelta(days=1)
    return count


def recent_failures(conn, curve, limit=5):
    """Most recent consecutive 'error' entries in the log, newest first."""
    rows = conn.execute(
        "SELECT run_at, status, message FROM fetch_log WHERE curve=? "
        "ORDER BY id DESC LIMIT ?", (curve, limit)).fetchall()
    out = []
    for r in rows:
        if r["status"] != "error":
            break
        out.append({"run_at": r["run_at"], "message": r["message"]})
    return out
