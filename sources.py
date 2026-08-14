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
    # Deliberately empty. The key comes from config.json locally (gitignored)
    # or the PDEX_API_KEY environment variable when hosted, so that it is never
    # committed to a repository.
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
                       "No PDEx API key configured. Set pdex_api_key in config.json, "
                       "or the PDEX_API_KEY environment variable when running hosted.")
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
    if curve == "BVAL":
        # Walk back to the most recent weekday that published, so a probe run
        # on a Monday morning or a holiday does not look like a failure.
        day, checked = today, 0
        while checked < 7:
            if day.weekday() < 5:
                out = fetch_bval_day(day)
                if out.ok or out.failed:
                    return out
                checked += 1
            day -= datetime.timedelta(days=1)
        return Outcome(EMPTY, [], "pdex-graphql", "no publication in the last 7 weekdays")
    raise ValueError(f"unknown curve {curve}")
