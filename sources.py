"""Fetchers for each benchmark curve.

Each public fetcher returns an Outcome, so the caller can tell three different
things apart:

    OK      - data was returned and passed validation
    EMPTY   - the source answered correctly but published nothing (weekend,
              public holiday, or not released yet). This is NOT a failure.
    FAILED  - the source could not be reached, could not be parsed, or returned
              something that failed validation. This IS a failure and is worth
              alerting on.

Conflating EMPTY with FAILED is the main thing to avoid: BVAL is legitimately
empty every weekend and on every PH public holiday, so treating "no rows" as an
error would cry wolf constantly, and treating a genuine breakage as "no rows"
would let the database silently stop updating.

Every fetcher tries its strategies in order and reports which one worked, so a
silent fallback still shows up in the log.

Stdlib only - urllib + re + json.
"""

import datetime
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PDEX_ENDPOINT = "https://www.pds.com.ph/api-mp/graphql/"
PDEX_KEY_HELP = (
    "PDEx rejected the API key (HTTP 401). The key has most likely rotated. "
    "Open https://www.pds.com.ph/wd-mp/php-bval-reference-rate-benchmark-tenors "
    "in Chrome, press F12, open Network, reload, click the /api-mp/graphql/ "
    "request and copy the value after 'APIKey ' in the Authorization header, "
    "then paste it into config.json as pdex_api_key."
)

BNM_KLIBOR_URL = "https://financialmarkets.bnm.gov.my/data-download-klibor"
NYFED_BASE = "https://markets.newyorkfed.org/api/rates"

# Documented column order, used only if BNM's <thead> disappears. Parsing is
# normally driven by the header itself so a reordered table cannot be
# misread as a different tenor.
KLIBOR_FALLBACK_TENORS = ["1M", "2M", "3M", "6M", "9M", "12M"]

# Validation gates. A payload that trips these is refused rather than stored.
CURVE_TENORS = {
    "BVAL": {"1M", "3M", "6M", "1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "20Y", "25Y"},
    "KLIBOR": {"1M", "2M", "3M", "6M", "9M", "12M"},
    "MYOR": {"O/N", "1M", "3M", "6M"},
    "MYORI": {"O/N", "1M", "3M", "6M"},
    "MGS": {"3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"},
    "MGII": {"3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"},
    "THOR": {"O/N", "1M", "3M", "6M"},
    "SOFR": {"O/N"},
}
RATE_MIN, RATE_MAX = 0.0, 30.0
EARLIEST_SANE_DATE = datetime.date(2000, 1, 1)

OK, EMPTY, FAILED = "ok", "empty", "failed"

_SSL_CTX = ssl.create_default_context()


class FetchError(Exception):
    """A genuine failure: unreachable, unparsable, or failed validation."""


class Outcome:
    """Result of a fetch attempt."""

    def __init__(self, status, rows=None, strategy=None, detail="", degraded=False):
        self.status = status
        self.rows = rows or []
        self.strategy = strategy
        self.detail = detail
        self.degraded = degraded  # succeeded, but via a less trustworthy path

    @property
    def ok(self):
        return self.status == OK

    @property
    def failed(self):
        return self.status == FAILED

    def __repr__(self):
        return (f"<Outcome {self.status} rows={len(self.rows)} "
                f"strategy={self.strategy!r} degraded={self.degraded}>")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

_DEFAULTS = {
    # Deliberately empty. The key always comes from the PDEX_API_KEY environment
    # variable: a user variable on Windows, an Actions secret when hosted. It is
    # never written to a file in this folder, so the whole folder is safe to
    # upload. config.json may still supply it as a fallback for anyone who
    # prefers that, but nothing here ships with a key in it.
    "pdex_api_key": "",
    "lookback_days": 45,
    "anomaly_min_overlap": 20,
    "anomaly_change_bp": 25,
    "anomaly_max_fraction": 0.30,
    "missed_weekdays_before_alarm": {"BVAL": 3, "KLIBOR": 3, "SOFR": 4},
}


def load_config():
    cfg = dict(_DEFAULTS)
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                if not k.startswith("_"):
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: config.json unreadable ({exc}); using built-in defaults")

    # An environment variable wins over the file, so the key can be supplied as
    # a GitHub Actions or Streamlit secret and never committed to a repository.
    env_key = os.environ.get("PDEX_API_KEY")
    if env_key:
        cfg["pdex_api_key"] = env_key.strip()
    return cfg


CONFIG = load_config()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(url, data=None, headers=None, timeout=60, retries=3):
    """Returns (body_text, http_status). Raises FetchError once retries are spent.

    HTTP error bodies are returned rather than raised when the status carries
    meaning we want to act on (401 for an expired key, for instance).
    """
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return resp.read().decode("utf-8", errors="replace"), resp.status
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            # Auth and client errors will not fix themselves on retry.
            if exc.code in (400, 401, 403, 404):
                return body, exc.code
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{url}: {last}")


def _try_strategies(strategies):
    """Run strategies in order. First one to return rows wins.

    If every strategy answers cleanly but with no rows, that is EMPTY, not a
    failure. Only if every strategy errors do we report FAILED.
    """
    errors, empties = [], []
    for label, fn in strategies:
        try:
            rows, degraded = fn()
        except FetchError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if rows:
            detail = ""
            if errors:
                detail = "recovered after " + "; ".join(errors)
            return Outcome(OK, rows, label, detail, degraded)
        empties.append(label)

    if empties and not errors:
        return Outcome(EMPTY, [], None, f"no data published (tried {', '.join(empties)})")
    if empties:
        return Outcome(EMPTY, [], None,
                       f"no data via {', '.join(empties)}; other paths errored: " + "; ".join(errors))
    return Outcome(FAILED, [], None, "; ".join(errors))


# --------------------------------------------------------------------------
# Validation - the guard against storing structurally wrong data
# --------------------------------------------------------------------------

def validate_rows(curve, rows):
    """Raise FetchError if the payload does not look like this curve's data.

    This is what catches a backend that changes shape but still returns
    HTTP 200, which would otherwise be written to the database as if fine.
    """
    if not rows:
        return rows

    allowed = CURVE_TENORS.get(curve, set())
    today = datetime.date.today()
    seen = {}

    for item in rows:
        try:
            date_str, tenor, rate = item
        except (TypeError, ValueError):
            raise FetchError(f"{curve}: malformed row {item!r}")

        if allowed and tenor not in allowed:
            raise FetchError(
                f"{curve}: unexpected tenor {tenor!r}. The source layout has probably "
                f"changed. Expected one of {sorted(allowed)}")

        if not isinstance(rate, (int, float)) or not (RATE_MIN < rate <= RATE_MAX):
            raise FetchError(
                f"{curve}: implausible rate {rate!r} for {tenor} on {date_str}. "
                f"Expected a percentage between {RATE_MIN} and {RATE_MAX} - the source "
                f"may have changed units or column order")

        try:
            d = datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            raise FetchError(f"{curve}: unparsable date {date_str!r}")

        if d < EARLIEST_SANE_DATE or d > today + datetime.timedelta(days=1):
            raise FetchError(f"{curve}: date {date_str} is outside the plausible range")

        key = (date_str, tenor)
        if key in seen and abs(seen[key] - rate) > 1e-9:
            raise FetchError(
                f"{curve}: conflicting values for {tenor} on {date_str} "
                f"({seen[key]} vs {rate}) - the response is inconsistent")
        seen[key] = rate

    return rows


# --------------------------------------------------------------------------
# PHP BVAL - PDEx GraphQL, one request per trade date
# --------------------------------------------------------------------------

_BVAL_QUERY = (
    'query ($tradeDate: Date) { bvalRates(tradeDate: $tradeDate, sortBy: "", '
    'sortByDirection: "") { tenor bvalRateToday } }'
)


def _bval_call(trade_date, api_key):
    payload = json.dumps({
        "query": _BVAL_QUERY,
        "variables": {"tradeDate": trade_date.isoformat()},
    }).encode()
    body, status = _request(
        PDEX_ENDPOINT, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"APIKey {api_key}"})

    if status == 401:
        raise FetchError(PDEX_KEY_HELP)
    if status >= 400:
        raise FetchError(f"PDEx returned HTTP {status} for {trade_date}")

    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        raise FetchError(
            f"PDEx returned non-JSON for {trade_date} (first 120 chars: {body[:120]!r}). "
            f"The endpoint or its response format may have changed")

    if doc.get("errors"):
        msgs = "; ".join(str(e.get("message")) for e in doc["errors"])
        raise FetchError(f"PDEx GraphQL error for {trade_date}: {msgs}")

    if "data" not in doc:
        raise FetchError(f"PDEx response for {trade_date} has no 'data' key - schema changed")

    node = doc["data"].get("bvalRates")
    if node is None:
        raise FetchError(
            f"PDEx response for {trade_date} has no 'bvalRates' field - the GraphQL "
            f"schema has changed and the query in sources.py needs updating")

    rows = []
    for item in node:
        tenor = (item.get("tenor") or "").strip().upper()
        rate = item.get("bvalRateToday")
        if tenor and isinstance(rate, (int, float)) and rate > 0:
            rows.append((trade_date.isoformat(), tenor, float(rate)))
    return rows


def fetch_bval_day(trade_date, api_key=None):
    """One trade date. EMPTY on weekends, PH public holidays, and data gaps."""
    key = api_key or CONFIG["pdex_api_key"]
    if not key:
        return Outcome(FAILED, [], None,
                       "No PDEx API key configured. Set the PDEX_API_KEY environment "
                       "variable (Start > 'Edit environment variables for your account'), "
                       "or a GitHub Actions secret of that name when running hosted.")
    out = _try_strategies([("pdex-graphql", lambda: (_bval_call(trade_date, key), False))])
    if out.ok:
        validate_rows("BVAL", out.rows)
    return out


def fetch_bval_range(start, end, pause=0.25, on_progress=None):
    """Probe each weekday in [start, end].

    Weekends are never requested. Weekdays that come back empty are recorded as
    non-publication days (PH holidays look exactly like this). Weekdays that
    error are recorded separately, because those are the ones worth alarming on.
    """
    rows, no_pub, failures = [], [], []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out = fetch_bval_day(day)
            if out.ok:
                rows.extend(out.rows)
            elif out.status == EMPTY:
                no_pub.append(day.isoformat())
            else:
                failures.append(f"{day}: {out.detail}")
            if on_progress:
                on_progress(day, len(out.rows), out.status)
            time.sleep(pause)
        day += datetime.timedelta(days=1)

    if failures and not rows:
        return Outcome(FAILED, [], None,
                       f"every requested day failed. First: {failures[0]}")
    detail = f"{len(no_pub)} weekday(s) with no publication"
    if failures:
        detail += f"; {len(failures)} day(s) errored (first: {failures[0]})"
    if not rows:
        return Outcome(EMPTY, [], "pdex-graphql", detail)
    return Outcome(OK, rows, "pdex-graphql", detail)


# --------------------------------------------------------------------------
# MYR KLIBOR - BNM FMIP renders the table server-side; no API is published
# --------------------------------------------------------------------------

_THEAD_RE = re.compile(r"<thead[^>]*>(.*?)</thead>", re.S | re.I)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S | re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_TENOR_RE = re.compile(r"^(\d{1,2})\s*([MY])$", re.I)


def _clean(html):
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _header_tenors(html):
    """Read the tenor for each column from BNM's <thead>.

    Driving the mapping off the header means that if BNM reorders, adds, or
    removes a tenor column, values still land on the right tenor instead of
    being silently shifted. Returns None if no usable header is present.
    """
    m = _THEAD_RE.search(html)
    if not m:
        return None
    cells = [_clean(c) for c in _TH_RE.findall(m.group(1))]
    if not cells:
        return None

    tenors = []
    for cell in cells[1:]:                      # column 0 is the date
        tm = _TENOR_RE.match(cell.replace(" ", ""))
        tenors.append(f"{int(tm.group(1))}{tm.group(2).upper()}" if tm else None)
    return tenors if any(tenors) else None


def parse_klibor_html(html):
    """Returns (rows, degraded). degraded=True means the header was missing and
    the documented column order had to be assumed."""
    tenors = _header_tenors(html)
    degraded = tenors is None
    if degraded:
        tenors = KLIBOR_FALLBACK_TENORS

    rows = []
    for tr in _ROW_RE.findall(html):
        cells = [_clean(c) for c in _CELL_RE.findall(tr)]
        if not cells:
            continue
        m = _DATE_RE.match(cells[0])
        if not m:
            continue                            # header, footnote, or a stat tile
        dd, mm, yyyy = m.groups()
        iso = f"{yyyy}-{mm}-{dd}"
        for tenor, cell in zip(tenors, cells[1:]):
            if not tenor:
                continue
            try:
                rows.append((iso, tenor, float(cell)))
            except ValueError:
                continue                        # "-" means not published
    return rows, degraded


def _klibor_call(url):
    body, status = _request(url, timeout=180)
    if status >= 400:
        raise FetchError(f"BNM returned HTTP {status}")
    if "<thead" not in body.lower() and "<tr" not in body.lower():
        raise FetchError("BNM response contains no table at all - the page has changed")
    return parse_klibor_html(body)


def fetch_klibor(start=None, end=None):
    """Omit both dates to pull the full published history (back to 2007)."""
    strategies = []
    if start and end:
        strategies.append((
            "bnm-date-range",
            lambda: _klibor_call(f"{BNM_KLIBOR_URL}?date_range=month_date"
                                 f"&date_select={start.isoformat()}&date_end={end.isoformat()}")))

        # If the date-range parameters ever stop working, the full table still
        # contains the window we want; filter it down locally.
        def _from_full():
            rows, degraded = _klibor_call(f"{BNM_KLIBOR_URL}?date_range=all_date")
            lo, hi = start.isoformat(), end.isoformat()
            return [r for r in rows if lo <= r[0] <= hi], degraded

        strategies.append(("bnm-full-table-filtered", _from_full))
    else:
        strategies.append((
            "bnm-all-dates",
            lambda: _klibor_call(f"{BNM_KLIBOR_URL}?date_range=all_date")))

    out = _try_strategies(strategies)
    if out.ok:
        validate_rows("KLIBOR", out.rows)
        if out.degraded:
            out.detail = ("BNM table header missing; assumed the documented column "
                          "order. Verify a value against the website. " + out.detail).strip()
    return out


# --------------------------------------------------------------------------
# MYR MYOR and MYOR-i - BNM overnight reference rates
#
# The table is unusually easy to misread and needs strict header mapping:
#   Reference Date | Publication Date | Reference Rate | Aggregate Volume |
#   Index | 1M Average | 3M Average | 6M Average
# There are TWO date columns, and MYOR publishes a day in arrears, so taking the
# first date column blindly would tag every rate with the wrong day. There is
# also a compounding Index that sits around 1.14 - inside the plausible range for
# a percentage, so the rate validator would happily store it as a rate. Only the
# header mapping stops that.
# --------------------------------------------------------------------------

BNM_MYOR_URLS = {
    "MYOR": "https://financialmarkets.bnm.gov.my/data-download-myor",
    "MYORI": "https://financialmarkets.bnm.gov.my/data-download-myori",
}

# Header label (lowercased, matched by prefix) to the tenor it represents.
# Anything not listed here is deliberately ignored.
MYOR_RATE_COLUMNS = {
    "reference rate": "O/N",
    "1m average": "1M",
    "3m average": "3M",
    "6m average": "6M",
}
MYOR_DATE_COLUMN = "reference date"

MYOR_HISTORY_START = datetime.date(2021, 9, 1)


def parse_myor_html(html):
    """Returns [(date_iso, tenor, rate)] for a MYOR or MYOR-i page.

    Raises FetchError rather than guessing if the header is absent: the columns
    here cannot safely be read by position.
    """
    head = _THEAD_RE.search(html)
    if not head:
        raise FetchError(
            "BNM MYOR page has no table header. The columns include two dates, a "
            "volume and a compounding index, so they cannot be read by position - "
            "refusing to guess")

    labels = [_clean(c).lower() for c in _TH_RE.findall(head.group(1))]
    date_idx = next((i for i, l in enumerate(labels) if l.startswith(MYOR_DATE_COLUMN)), None)
    if date_idx is None:
        raise FetchError(
            f"BNM MYOR page has no '{MYOR_DATE_COLUMN}' column (headers: {labels}). "
            f"The layout has changed")

    tenor_by_idx = {}
    for i, label in enumerate(labels):
        for prefix, tenor in MYOR_RATE_COLUMNS.items():
            if label.startswith(prefix):
                tenor_by_idx[i] = tenor
                break
    if not tenor_by_idx:
        raise FetchError(
            f"BNM MYOR page exposes no recognised rate columns (headers: {labels})")

    rows = []
    for tr in _ROW_RE.findall(html):
        cells = [_clean(c) for c in _CELL_RE.findall(tr)]
        if len(cells) <= date_idx:
            continue
        m = _DATE_RE.match(cells[date_idx])
        if not m:
            continue                       # header, footnote, or a summary tile
        dd, mm, yyyy = m.groups()
        iso = f"{yyyy}-{mm}-{dd}"
        for idx, tenor in tenor_by_idx.items():
            if idx >= len(cells):
                continue
            try:
                rows.append((iso, tenor, float(cells[idx].replace(",", ""))))
            except ValueError:
                continue                   # "-" before a rate was first published
    return rows


def fetch_myor(curve, start=None, end=None):
    """MYOR or MYOR-i over a date window.

    Unlike the KLIBOR page, date_range=all_date returns only a handful of rows
    here, so an explicit window is always supplied.
    """
    base = BNM_MYOR_URLS[curve]
    start = start or MYOR_HISTORY_START
    end = end or datetime.date.today()

    def strategy():
        url = (f"{base}?date_range=month_date"
               f"&date_select={start.isoformat()}&date_end={end.isoformat()}")
        body, status = _request(url, timeout=180)
        if status >= 400:
            raise FetchError(f"BNM returned HTTP {status} for {curve}")
        return parse_myor_html(body), False

    out = _try_strategies([("bnm-myor-range", strategy)])
    if out.ok:
        validate_rows(curve, out.rows)
    return out


# --------------------------------------------------------------------------
# MYR MGS and MGII - BNM benchmark yields, one page per trade date
#
# MGS is the conventional government bond benchmark; MGII is the Islamic one
# and is the correct reference for Sukuk. Both tables live on the same page, so
# one request serves both curves and the result is memoised per date.
# --------------------------------------------------------------------------

BNM_BENCHMARK_URL = "https://financialmarkets.bnm.gov.my/benchmark-yields"

# Column labels BNM uses in the leaf header row. MGS carries a Coupon column
# and MGII does not, so the position of "Close" differs between the two tables.
# Reading the order from the header is what keeps a 5Y yield from being stored
# as a 7Y one if BNM ever adds or drops a column.
_LEAF_HEADERS = ("tenor", "maturity", "coupon", "low", "high", "close")

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_TENOR_CELL_RE = re.compile(r"^(\d{1,2})\s*Y$", re.I)

_benchmark_cache = {}
_BENCHMARK_CACHE_MAX = 400


def _leaf_columns(thead_html):
    """Ordered list of the leaf column names, e.g.
    ['tenor','maturity','coupon','low','high','close'] for MGS."""
    cells = [_clean(c).lower() for c in _TH_RE.findall(thead_html)]
    out = []
    for cell in cells:
        for name in _LEAF_HEADERS:
            if cell.startswith(name):
                out.append(name)
                break
    return out


def parse_benchmark_yields(html):
    """Returns {"MGS": [(tenor, close_rate)], "MGII": [...]}.

    Yields marked with an asterisk are BNM's indication that no actual trade
    took place that day; the value is still the official close, so it is kept
    and the marker stripped.
    """
    result = {}
    for table_html in _TABLE_RE.findall(html):
        head = _THEAD_RE.search(table_html)
        if not head:
            continue
        head_text = _clean(head.group(1)).lower()

        if "government investment issues" in head_text or "mgii" in head_text:
            curve = "MGII"
        elif "government securities" in head_text or "(mgs)" in head_text:
            curve = "MGS"
        else:
            continue

        cols = _leaf_columns(head.group(1))
        if "close" not in cols or "tenor" not in cols:
            raise FetchError(
                f"BNM {curve} table header is missing a Tenor or Close column "
                f"(found {cols}). The page layout has changed")
        tenor_idx, close_idx = cols.index("tenor"), cols.index("close")

        rows = []
        for tr in _ROW_RE.findall(table_html):
            cells = [_clean(c) for c in _CELL_RE.findall(tr)]
            if len(cells) <= close_idx:
                continue
            tm = _TENOR_CELL_RE.match(cells[tenor_idx])
            if not tm:
                continue
            tenor = f"{int(tm.group(1))}Y"
            raw = cells[close_idx].replace("*", "").strip()
            try:
                rows.append((tenor, float(raw)))
            except ValueError:
                continue          # "-" means no yield published for that tenor
        result[curve] = rows

    if not result:
        raise FetchError("No MGS or MGII table found on the BNM benchmark yields page")
    return result


def _benchmark_day(trade_date):
    """Both curves for one date, memoised so MGS and MGII share one request."""
    key = trade_date.isoformat()
    if key in _benchmark_cache:
        return _benchmark_cache[key]

    body, status = _request(f"{BNM_BENCHMARK_URL}?date={key}", timeout=120)
    if status >= 400:
        raise FetchError(f"BNM returned HTTP {status} for benchmark yields on {key}")
    parsed = parse_benchmark_yields(body)

    if len(_benchmark_cache) >= _BENCHMARK_CACHE_MAX:
        _benchmark_cache.clear()
    _benchmark_cache[key] = parsed
    return parsed


def fetch_benchmark_day(curve, trade_date):
    """One trade date for MGS or MGII. EMPTY on weekends and MY holidays."""
    def strategy():
        parsed = _benchmark_day(trade_date)
        return [(trade_date.isoformat(), t, r) for t, r in parsed.get(curve, [])], False

    out = _try_strategies([("bnm-benchmark-yields", strategy)])
    if out.ok:
        validate_rows(curve, out.rows)
    return out


def fetch_benchmark_range(curve, start, end, pause=0.3, on_progress=None):
    """Probe each weekday in [start, end], as with BVAL. Weekdays that come back
    empty are non-publication days; weekdays that error are recorded separately."""
    rows, no_pub, failures = [], [], []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out = fetch_benchmark_day(curve, day)
            if out.ok:
                rows.extend(out.rows)
            elif out.status == EMPTY:
                no_pub.append(day.isoformat())
            else:
                failures.append(f"{day}: {out.detail}")
            if on_progress:
                on_progress(day, len(out.rows), out.status)
            time.sleep(pause)
        day += datetime.timedelta(days=1)

    if failures and not rows:
        return Outcome(FAILED, [], None, f"every requested day failed. First: {failures[0]}")
    detail = f"{len(no_pub)} weekday(s) with no publication"
    if failures:
        detail += f"; {len(failures)} day(s) errored (first: {failures[0]})"
    if not rows:
        return Outcome(EMPTY, [], "bnm-benchmark-yields", detail)
    return Outcome(OK, rows, "bnm-benchmark-yields", detail)


# --------------------------------------------------------------------------
# THB THOR - ThaiBMA, the calculation agent for Bank of Thailand
#
# One request per date. The response mixes two series:
#   code "THOR"  - the overnight rate, which DOES honour the asof parameter
#   code "THORA" - the 1M/3M/6M compounded averages, which DO NOT: they always
#                  come back with the latest values, whatever date you ask for.
#
# So each row's own asof field is used rather than the requested date. Trusting
# the query date would stamp today's average rates onto every historical date in
# a backfill - eleven years of fabricated data that would look entirely
# plausible. The consequence is that O/N backfills fully while the averages only
# accumulate from the day this starts running.
# --------------------------------------------------------------------------

THAIBMA_THOR_URL = "https://www.thaibma.or.th/api/thor/daily"
THAIBMA_AVAIL_URL = "https://www.thaibma.or.th/api/thor/avail"

THOR_HISTORY_START = datetime.date(2015, 1, 5)   # per /api/thor/avail


def parse_thor(payload, requested_date):
    """Returns [(date_iso, tenor, rate)] from the ThaiBMA daily response."""
    if not isinstance(payload, list):
        raise FetchError("ThaiBMA THOR response was not a list - format changed")

    rows = []
    for item in payload:
        tenor = (item.get("tenor") or "").strip().upper()
        rate = item.get("rate")
        asof = item.get("asof") or ""
        if not tenor or not isinstance(rate, (int, float)):
            continue
        # The row's own date, never the requested one. See the note above.
        date = asof[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise FetchError(
                f"ThaiBMA returned an unparsable asof {asof!r} for {tenor} - "
                f"format changed")
        rows.append((date, tenor, float(rate)))
    return rows


def fetch_thor_day(trade_date):
    """One trade date. EMPTY on weekends and Thai public holidays."""
    def strategy():
        body, status = _request(
            f"{THAIBMA_THOR_URL}?asof={trade_date.isoformat()}",
            headers={"Accept": "application/json"}, timeout=60)
        if status >= 400:
            raise FetchError(f"ThaiBMA returned HTTP {status} for {trade_date}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise FetchError(
                f"ThaiBMA returned non-JSON for {trade_date} - the endpoint may "
                f"have moved or now requires a key")
        return parse_thor(payload, trade_date), False

    out = _try_strategies([("thaibma-daily", strategy)])
    if out.ok:
        validate_rows("THOR", out.rows)
    return out


def fetch_thor_range(start, end, pause=0.25, on_progress=None):
    """Probe each weekday in [start, end], as with BVAL and the BNM benchmarks."""
    rows, no_pub, failures = [], [], []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out = fetch_thor_day(day)
            if out.ok:
                rows.extend(out.rows)
            elif out.status == EMPTY:
                no_pub.append(day.isoformat())
            else:
                failures.append(f"{day}: {out.detail}")
            if on_progress:
                on_progress(day, len(out.rows), out.status)
            time.sleep(pause)
        day += datetime.timedelta(days=1)

    if failures and not rows:
        return Outcome(FAILED, [], None, f"every requested day failed. First: {failures[0]}")
    detail = f"{len(no_pub)} weekday(s) with no publication"
    if failures:
        detail += f"; {len(failures)} day(s) errored (first: {failures[0]})"
    if not rows:
        return Outcome(EMPTY, [], "thaibma-daily", detail)
    return Outcome(OK, rows, "thaibma-daily", detail)


# --------------------------------------------------------------------------
# USD SOFR - NY Fed public API
# --------------------------------------------------------------------------

def _sofr_parse(body, status, what):
    if status >= 400:
        raise FetchError(f"NY Fed returned HTTP {status} for {what}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise FetchError(f"NY Fed returned non-JSON for {what} - endpoint may have moved")
    if "refRates" not in payload:
        raise FetchError(f"NY Fed response for {what} has no 'refRates' key - format changed")

    rows = []
    for item in payload["refRates"]:
        if (item.get("type") or "").upper() != "SOFR":
            continue
        date_str, rate = item.get("effectiveDate"), item.get("percentRate")
        if date_str and isinstance(rate, (int, float)):
            rows.append((date_str, "O/N", float(rate)))
    return rows


def fetch_sofr(start=None, end=None):
    start = start or datetime.date(2018, 4, 1)
    end = end or datetime.date.today()
    span = max(1, (end - start).days)

    def _search():
        url = f"{NYFED_BASE}/secured/sofr/search.json?startDate={start.isoformat()}&endDate={end.isoformat()}"
        return _sofr_parse(*_request(url, timeout=120), what="search.json"), False

    def _last_n():
        n = min(max(span, 5), 1000)
        url = f"{NYFED_BASE}/secured/sofr/last/{n}.json"
        rows = _sofr_parse(*_request(url, timeout=120), what=f"last/{n}.json")
        lo, hi = start.isoformat(), end.isoformat()
        return [r for r in rows if lo <= r[0] <= hi], False

    def _latest_only():
        # Last resort: keeps today's number flowing even if the history
        # endpoints are down. Marked degraded because it back-fills nothing.
        url = f"{NYFED_BASE}/all/latest.json"
        rows = _sofr_parse(*_request(url, timeout=60), what="all/latest.json")
        lo, hi = start.isoformat(), end.isoformat()
        return [r for r in rows if lo <= r[0] <= hi], True

    out = _try_strategies([
        ("nyfed-search", _search),
        ("nyfed-last-n", _last_n),
        ("nyfed-latest", _latest_only),
    ])
    if out.ok:
        validate_rows("SOFR", out.rows)
    return out


# --------------------------------------------------------------------------
# Reachability probe, used by "cli.py doctor"
# --------------------------------------------------------------------------

def probe(curve):
    """Light end-to-end check of one source. Returns an Outcome."""
    today = datetime.date.today()
    if curve == "SOFR":
        return fetch_sofr(today - datetime.timedelta(days=10), today)
    if curve == "KLIBOR":
        return fetch_klibor(today - datetime.timedelta(days=10), today)
    if curve in BNM_MYOR_URLS:
        return fetch_myor(curve, today - datetime.timedelta(days=10), today)
    if curve in ("BVAL", "MGS", "MGII", "THOR"):
        # Walk back to the most recent weekday that published, so a probe run
        # on a Monday morning or a holiday does not look like a failure.
        if curve == "BVAL":
            fetch, label = fetch_bval_day, "pdex-graphql"
        elif curve == "THOR":
            fetch, label = fetch_thor_day, "thaibma-daily"
        else:
            fetch, label = (lambda d: fetch_benchmark_day(curve, d)), "bnm-benchmark-yields"
        day, checked = today, 0
        while checked < 7:
            if day.weekday() < 5:
                out = fetch(day)
                if out.ok or out.failed:
                    return out
                checked += 1
            day -= datetime.timedelta(days=1)
        return Outcome(EMPTY, [], label, "no publication in the last 7 weekdays")
    raise ValueError(f"unknown curve {curve}")


# --------------------------------------------------------------------------
# THB THOR averages - Bank of Thailand statistics page
#
# ThaiBMA's JSON API serves the overnight rate historically but always returns
# today's values for the 1M/3M/6M averages, so their history cannot be built
# from it. BOT's statistics report FM_RT_013 does carry them: selecting a
# calendar month returns every business day in that month, which makes a
# month-at-a-time backfill practical.
#
# It is an ASP.NET WebForms page, so each request needs the viewstate from a
# fresh GET. That is more fragile than a JSON API, which is why it is used only
# for the averages that ThaiBMA cannot provide.
# --------------------------------------------------------------------------

BOT_THOR_URL = ("https://app.bot.or.th/BTWS_STAT/statistics/"
                "BOTWEBSTAT.aspx?reportID=945&language=Eng")

# Row labels in the report mapped to our tenor vocabulary.
BOT_THOR_ROWS = {
    "thor": "O/N",
    "1 month": "1M",
    "3 months": "3M",
    "6 months": "6M",
}

_BOT_DATE_RE = re.compile(r"(\d{2})\s+([A-Z]{3})\s+(\d{4})")
_BOT_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _bot_hidden(html, name):
    m = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else ""


def parse_bot_thor(html):
    """Returns [(date_iso, tenor, rate)] from the FM_RT_013 table."""
    # The report pads its row labels with &nbsp;, so entities have to go before
    # whitespace is collapsed or "1 Month" never matches.
    def clean_row(tr):
        text = _TAG_RE.sub(" ", tr)
        text = text.replace("&nbsp;", " ").replace("&#160;", " ")
        return re.sub(r"\s+", " ", text).strip()

    rows = [clean_row(tr) for tr in _ROW_RE.findall(html)]

    header = next((r for r in rows if _BOT_DATE_RE.search(r)), None)
    if not header:
        raise FetchError("BOT THOR report has no date header - layout changed")

    dates = []
    for dd, mon, yyyy in _BOT_DATE_RE.findall(header):
        month = _BOT_MONTHS.get(mon)
        if not month:
            raise FetchError("BOT THOR report has an unknown month %r" % mon)
        dates.append("%s-%02d-%s" % (yyyy, month, dd))

    out = []
    for raw in rows:
        # Data rows look like "1 THOR 0.99 ..." or "3 1 Month 0.99 ...", with a
        # leading sequence number. Strip it, then match the label.
        line = re.sub(r"^\d+\s+", "", raw).strip()
        label = None
        for key, tenor in BOT_THOR_ROWS.items():
            if line.lower().startswith(key):
                label = tenor
                line = line[len(key):]
                break
        if label is None:
            continue

        values = line.split()
        for date, cell in zip(dates, values):
            try:
                out.append((date, label, float(cell.replace(",", ""))))
            except ValueError:
                continue          # "n.a." before publication
    return out


def fetch_thor_month(year, month):
    """THOR and its averages for one calendar month, from BOT."""
    def strategy():
        page, status = _request(BOT_THOR_URL, timeout=120)
        if status >= 400:
            raise FetchError("BOT returned HTTP %d for the THOR report" % status)

        form = {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
            "__VIEWSTATE": _bot_hidden(page, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _bot_hidden(page, "__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": _bot_hidden(page, "__EVENTVALIDATION"),
            "drpPeriod": "DAY",
            # The dropdowns use masked values the server recombines.
            "drpFromMonth": "xxxx%02dxx" % month, "drpFromYear": "%dxxxx" % year,
            "drpToMonth": "xxxx%02dxx" % month, "drpToYear": "%dxxxx" % year,
            "btnSubmit": "Submit",
        }
        if not form["__VIEWSTATE"]:
            raise FetchError("BOT THOR page returned no viewstate - layout changed")

        body, status = _request(
            BOT_THOR_URL, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=180)
        if status >= 400:
            raise FetchError("BOT rejected the THOR query with HTTP %d" % status)
        return parse_bot_thor(body), False

    out = _try_strategies([("bot-fm-rt-013", strategy)])
    if out.ok:
        validate_rows("THOR", out.rows)
    return out


def fetch_thor_recent(end=None, months=2):
    """THOR and its averages for the last `months` calendar months, from BOT.

    Used by the daily job in place of ThaiBMA. BOT returns the overnight rate
    AND the averages for every business day of a month, so two requests cover
    any realistic gap - against up to forty-five one-per-day calls to ThaiBMA
    that would still leave the averages stuck at today's value.
    """
    end = end or datetime.date.today()
    wanted, cursor = [], end.replace(day=1)
    for _ in range(max(1, months)):
        wanted.append((cursor.year, cursor.month))
        cursor = (cursor - datetime.timedelta(days=1)).replace(day=1)

    rows, errors = [], []
    for year, month in wanted:
        out = fetch_thor_month(year, month)
        if out.ok:
            rows.extend(out.rows)
        elif out.failed:
            errors.append("%04d-%02d: %s" % (year, month, out.detail))

    if not rows:
        if errors:
            return Outcome(FAILED, [], None, "; ".join(errors))
        return Outcome(EMPTY, [], "bot-fm-rt-013", "no data in the last %d months" % months)
    detail = "%d month(s)" % len(wanted)
    if errors:
        detail += "; %d failed (%s)" % (len(errors), errors[0])
    return Outcome(OK, rows, "bot-fm-rt-013", detail)
