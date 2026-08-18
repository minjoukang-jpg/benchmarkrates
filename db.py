"""SQLite storage for the benchmark rates database.

One row per (curve, date, tenor). Stdlib only - no pip install required.
"""

import csv
import io
import math
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
#
# "group" splits a market's benchmarks into money market and government
# curves. It is only used for layout: a market carrying several of each
# gets sub-headings so the cards do not read as one undifferentiated run.
CURVES = {
    "BVAL": {
        "group": "Government",
        "currency": "PHP",
        "market": "Philippines",
        "label": "PHP BVAL",
        "description": "Bloomberg Valuation benchmark tenors for PHP government securities",
        "source": "PDEx (pds.com.ph)",
        "url": "https://www.pds.com.ph/",
        "headline_tenors": ["3Y", "5Y", "7Y"],   # the tenors quoted most often here
    },
    "KLIBOR": {
        "group": "Money market",
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR KLIBOR",
        "description": "Kuala Lumpur Interbank Offered Rate",
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-klibor",
        # 1M/3M/6M are the live tenors: BNM discontinued 2M, 9M and 12M in
        # Jan 2023, so this is the whole published curve, and it lines up with
        # the MYOR card below so the KLIBOR-to-MYOR gap reads at a glance.
        "headline_tenors": ["1M", "3M", "6M"],
    },
    "MYOR": {
        "group": "Money market",
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MYOR",
        "description": ("Malaysia Overnight Rate, the transaction-based benchmark BNM is "
                        "transitioning to from KLIBOR, with compounded 1M, 3M and 6M averages"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-myor",
        # The compounded averages. O/N is in the table below; the averages
        # are what a facility actually prices off.
        "headline_tenors": ["1M", "3M", "6M"],
    },
    "MYORI": {
        "group": "Money market",
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MYOR-i (Islamic)",
        "description": ("Malaysia Overnight Rate-i, the Shariah-compliant equivalent of MYOR, "
                        "based on Islamic money market transactions"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/data-download-myori",
        "headline_tenors": ["1M", "3M", "6M"],
    },
    "MGS": {
        "group": "Government",
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MGS",
        # 5Y, 7Y and 10Y: the tenors project debt is actually benchmarked to.
        "headline_tenors": ["5Y", "7Y", "10Y"],
        "description": "Malaysian Government Securities benchmark closing yields (conventional)",
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/benchmark-yields",
    },
    "MGII": {
        "group": "Government",
        "currency": "MYR",
        "market": "Malaysia",
        "label": "MYR MGII (Islamic)",
        "headline_tenors": ["5Y", "7Y", "10Y"],
        "description": ("Malaysian Government Investment Issues benchmark closing yields. "
                        "This is the Shariah-compliant benchmark, and the correct reference "
                        "for Sukuk rather than conventional MGS"),
        "source": "Bank Negara Malaysia FMIP",
        "url": "https://financialmarkets.bnm.gov.my/benchmark-yields",
    },
    "THOR": {
        "group": "Money market",
        "currency": "THB",
        "market": "Thailand",
        "label": "THB THOR",
        # The compounded averages, matching the MYOR card. THOR itself is
        # overnight and sits in the table below.
        "headline_tenors": ["1M", "3M", "6M"],
        "description": ("Thai Overnight Repurchase Rate, Thailand's transaction-based "
                        "reference rate, with compounded 1M, 3M and 6M averages"),
        "source": "ThaiBMA, calculation agent for Bank of Thailand",
        "url": "https://app.bot.or.th/thor/en",
    },
    "SOFR": {
        "group": "Money market",
        "currency": "USD",
        "market": "United States",
        "label": "USD SOFR",
        "description": ("Secured Overnight Financing Rate, with the NY Fed's "
                        "compounded 30, 90 and 180 day averages"),
        "source": "Federal Reserve Bank of New York",
        "url": "https://www.newyorkfed.org/markets/reference-rates/sofr-averages-and-index",
        # All four on the face of the card. The overnight rate is what USD
        # facilities actually fix against day to day, so it is not something to
        # fold away behind a toggle.
        "headline_tenors": ["O/N", "30D", "90D", "180D"],
    },
}

# Left-to-right order of the market columns on the dashboards.
MARKET_ORDER = ["Philippines", "Malaysia", "Thailand", "United States"]

# Flags drawn as inline SVG rather than emoji. Windows has no flag glyphs, so
# emoji regional indicators render as a boxed letter pair ("PH") in Chrome on
# Windows - which is where these are actually read. SVG looks the same
# everywhere and needs no font support or external asset.
_FLAG_VIEWBOX = 'viewBox="0 0 24 16" width="24" height="16" xmlns="http://www.w3.org/2000/svg"'


def _star_points(cx, cy, outer, inner, points, rotate=-90.0):
    """Vertices for an n-pointed star, alternating outer and inner radius."""
    coords = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        angle = math.radians(rotate + i * 180.0 / points)
        coords.append(f"{cx + radius * math.cos(angle):.2f},"
                      f"{cy + radius * math.sin(angle):.2f}")
    return " ".join(coords)

MARKET_FLAGS = {
    "Philippines": (
        f'<svg {_FLAG_VIEWBOX} role="img" aria-label="Philippines">'
        '<rect width="24" height="8" fill="#0038a8"/>'
        '<rect y="8" width="24" height="8" fill="#ce1126"/>'
        '<path d="M0 0 L10.4 8 L0 16 Z" fill="#fff"/>'
        '<circle cx="3.2" cy="8" r="1.8" fill="#fcd116"/>'
        '<circle cx="1.5" cy="2.4" r="0.55" fill="#fcd116"/>'
        '<circle cx="1.5" cy="13.6" r="0.55" fill="#fcd116"/>'
        '<circle cx="7.9" cy="8" r="0.55" fill="#fcd116"/>'
        '</svg>'),
    "Malaysia": (
        f'<svg {_FLAG_VIEWBOX} role="img" aria-label="Malaysia">'
        '<rect width="24" height="16" fill="#fff"/>'
        + "".join(f'<rect y="{i * 16 / 14:.3f}" width="24" height="{16 / 14:.3f}" fill="#cc0001"/>'
                  for i in range(0, 14, 2)) +
        '<rect width="13" height="9.143" fill="#010066"/>'
        # Crescent: a yellow disc with a canton-blue disc cut out of its right,
        # so the opening faces the star, as on the real flag.
        '<circle cx="5.0" cy="4.55" r="2.85" fill="#ffcc00"/>'
        '<circle cx="6.35" cy="4.55" r="2.45" fill="#010066"/>'
        # The Bintang Persekutuan, a 14-point star, not a ring.
        f'<polygon points="{_star_points(8.95, 4.55, 2.05, 0.95, 14)}" fill="#ffcc00"/>'
        '</svg>'),
    # Five bands in 1:1:2:1:1 proportion - red, white, blue, white, red.
    "Thailand": (
        f'<svg {_FLAG_VIEWBOX} role="img" aria-label="Thailand">'
        '<rect width="24" height="16" fill="#f4f5f8"/>'
        '<rect y="0" width="24" height="2.667" fill="#a51931"/>'
        '<rect y="5.333" width="24" height="5.333" fill="#2d2a4a"/>'
        '<rect y="13.333" width="24" height="2.667" fill="#a51931"/>'
        '</svg>'),
    "United States": (
        f'<svg {_FLAG_VIEWBOX} role="img" aria-label="United States">'
        '<rect width="24" height="16" fill="#fff"/>'
        + "".join(f'<rect y="{i * 16 / 13:.3f}" width="24" height="{16 / 13:.3f}" fill="#b22234"/>'
                  for i in range(0, 13, 2)) +
        '<rect width="10" height="8.615" fill="#3c3b6e"/>'
        + "".join(f'<circle cx="{1.4 + c * 1.75:.2f}" cy="{1.4 + r * 1.95:.2f}" r="0.42" fill="#fff"/>'
                  for r in range(4) for c in range(5)) +
        '</svg>'),
}


def curves_by_market():
    """[(market, flag_svg, [curve, ...]), ...] in display order."""
    grouped = []
    for market in MARKET_ORDER:
        members = [c for c, m in CURVES.items() if m["market"] == market]
        if members:
            grouped.append((market, MARKET_FLAGS.get(market, ""), members))
    # Any market not listed in MARKET_ORDER still gets shown, on the end.
    for curve, meta in CURVES.items():
        if meta["market"] not in MARKET_ORDER:
            existing = next((g for g in grouped if g[0] == meta["market"]), None)
            if existing:
                existing[2].append(curve)
            else:
                grouped.append((meta["market"], MARKET_FLAGS.get(meta["market"], ""), [curve]))
    return grouped


# Order the group sub-headings appear in within a market column.
GROUP_ORDER = ["Money market", "Government"]

# A market carrying this many benchmarks or more gets a double-width column so
# its cards flow two-abreast instead of running down the page. Malaysia has
# five against one each for Thailand and the US, which otherwise left the row
# roughly 1,100px taller than it needed to be, nearly all of it whitespace.
WIDE_MARKET_CURVES = 4

# How many cards a wide market flows across. Three fits the ~330px a card needs
# for its headline figures inside the usual 1140px content width.
WIDE_MARKET_COLUMNS = 3


def market_layout():
    """Market columns ready to render, in display order.

    Each entry is {market, flag, curves, span, columns, groups}. `span` is how
    many grid slots the column takes and `columns` how many card columns flow
    inside it. `groups` is [{group, curves}, ...]; a market whose benchmarks all
    sit in one group gets a single entry with group=None, because a heading that
    contrasts with nothing is just noise.

    Both dashboards read this, so the local and hosted layouts cannot drift.
    """
    layout = []
    for market, flag, curves in curves_by_market():
        groups = []
        for name in GROUP_ORDER:
            members = [c for c in curves if CURVES[c].get("group") == name]
            if members:
                groups.append({"group": name, "curves": members})
        # Anything with no group, or a market sitting entirely in one group.
        ungrouped = [c for c in curves if CURVES[c].get("group") not in GROUP_ORDER]
        if ungrouped:
            groups.append({"group": None, "curves": ungrouped})
        if len(groups) < 2:
            groups = [{"group": None, "curves": curves}]

        wide = len(curves) >= WIDE_MARKET_CURVES
        layout.append({
            "market": market,
            "flag": flag,
            # Flat list kept alongside the groups: callers that only need the
            # curve names should not have to flatten.
            "curves": curves,
            "span": 2 if wide else 1,
            "columns": WIDE_MARKET_COLUMNS if wide else 1,
            "groups": groups,
        })
    return layout


# Sort weight for tenors, expressed in months. Used for ordering term structures.
TENOR_MONTHS = {
    "O/N": 1.0 / 30, "1W": 0.25, "2W": 0.5,
    # SOFR Averages are compounded over 30, 90 and 180 calendar days, which is
    # slightly short of 1, 3 and 6 months. Weighted in days/30.4375 so they sort
    # just inside the monthly tenors instead of tying with them.
    "30D": 30 / 30.4375, "90D": 90 / 30.4375, "180D": 180 / 30.4375,
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


def headline_tenors(curve, available):
    """Which tenors to show as the big figures on a curve's card.

    A list, because a market often quotes several: PHP BVAL is usually discussed
    at 3Y, 5Y and 7Y together. Declared per curve in CURVES so the choice is
    explicit and in one place rather than inferred. Any declared tenor that was
    not published on the latest date is dropped, and if none survive it falls
    back to a middle tenor so the card is never blank.
    """
    tenors = list(available or [])
    if not tenors:
        return []
    declared = CURVES.get(curve, {}).get("headline_tenors") or []
    present = [t for t in declared if t in tenors]
    if present:
        return sorted(present, key=tenor_sort_key)
    ordered = sorted(tenors, key=tenor_sort_key)
    return [ordered[min(2, len(ordered) - 1)]]


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


def ensure_database(path=DB_PATH, data_dir=DATA_DIR):
    """Return a connection, rebuilding from data/ if the database is absent.

    rates.db is a build artefact, not something to keep around: it is gitignored,
    and leaving a 10 MB binary in the folder invites it being dragged into a web
    upload, where it arrives corrupted. Rebuilding takes about a quarter of a
    second, so the file can simply be deleted and regenerated on demand.
    """
    missing = not os.path.exists(path)
    conn = init(path)
    empty = not conn.execute("SELECT COUNT(*) FROM rates").fetchone()[0]
    if (missing or empty) and csv_files_present(data_dir):
        read_csv_files(conn, data_dir)
    return conn


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
