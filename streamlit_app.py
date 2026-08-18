"""Hosted benchmark rates dashboard.

Run locally:   streamlit run streamlit_app.py
Hosted:        Streamlit Community Cloud, deployed from the repository root.

Reads the rates.db that ships with the deployment. The database is refreshed by
the GitHub Actions workflow in .github/workflows/daily-update.yml, so nothing
here writes to it and no laptop needs to be switched on.

The data-access functions are imported from serve.py rather than rewritten, so
the hosted app, the local app and the CSV export can never drift apart. In
particular the download button reuses export_csv, which the contract tests pin
byte-for-byte.
"""

import contextlib
import datetime
import hashlib
import importlib
import os
import tempfile

import altair as alt
import pandas as pd
import streamlit as st

import db
import serve


def _reload_if_stale(module):
    """Return `module`, re-imported if the file on disk has changed under it.

    Streamlit re-executes this script on every rerun, but Python caches imported
    modules in sys.modules. After a deploy the process can therefore end up
    running the new streamlit_app.py against the db.py it imported before the
    update. Anything added to db.py in the same commit is then missing, and
    because this script calls it at module level the AttributeError takes the
    whole page down rather than degrading one section. That is exactly how the
    market_layout() deploy went down.

    Comparing the file's hash against a stamp left on the module object catches
    it generically, so this does not need touching each time db.py gains a
    function. The reload costs a few milliseconds and happens once per change.

    This has to live here rather than in db.py: a stale db.py is the failure
    being recovered from, so it cannot be the thing holding the recovery.
    """
    path = getattr(module, "__file__", "")
    if not path or not os.path.exists(path):
        return module
    with open(path, "rb") as fh:
        stamp = hashlib.sha1(fh.read()).hexdigest()
    if getattr(module, "_source_stamp", None) == stamp:
        return module
    module = importlib.reload(module)
    module._source_stamp = stamp
    return module


# Order matters: serve.py binds db.export_csv at import time, so db has to be
# current before serve is rebuilt against it.
db = _reload_if_stale(db)
serve = _reload_if_stale(serve)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rates.db")

# Validated categorical palette, slots 1 to 5. Assigned to tenors in a fixed
# order so a tenor keeps its colour when the selection changes.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
MAX_SERIES = 5

RANGES = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "3Y": 1095, "5Y": 1825, "Max": None}

# KLIBOR's full history is about 5,500 dates across 3 tenors, which is well over
# Altair's default 5,000-row cap on embedded data. The payload at these sizes is
# a megabyte or so, which is fine, so lift the cap rather than downsample and
# quietly show the user something other than the real series.
alt.data_transformers.disable_max_rows()

st.set_page_config(page_title="Benchmark Rates", page_icon="chart_with_upwards_trend",
                   layout="wide")

# If the reload above could not recover, say so plainly. Streamlit redacts the
# real exception on Cloud, so an unguarded AttributeError here reads only as
# "This app has encountered an error", which says nothing about the fix.
_missing = [name for name in ("market_layout", "headline_tenors", "export_csv")
            if not hasattr(db, name)]
if _missing:
    st.error(
        "**This deployment is running a stale copy of db.py.** Missing: "
        + ", ".join(_missing)
        + ". Reboot the app from Manage app in the lower right, which restarts "
          "the process against the current files. The stored data is not "
          "affected.")
    st.stop()


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _build_database(path):
    """Build a SQLite database at `path` from the committed CSVs."""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = db.init(path)
    db.read_csv_files(conn)
    conn.close()
    # Leave no write-ahead log beside it, so read-only opens never depend on
    # being able to create the -shm sidecar.
    db.checkpoint(path)


def data_fingerprint():
    """Cheap signature of the CSV files: name, size and modification time.

    Every cache below is keyed on this. The daily workflow commits new CSVs, and
    when the host picks them up the fingerprint changes, so the database is
    rebuilt and every cached query is invalidated together.

    Without it the app served stale numbers indefinitely: st.cache_resource has
    no expiry, so the database built from the first set of CSVs was kept for the
    life of the process even after newer files landed underneath it.
    """
    if not db.csv_files_present():
        return ""
    parts = []
    for name in sorted(os.listdir(db.DATA_DIR)):
        if name.endswith(".csv"):
            info = os.stat(os.path.join(db.DATA_DIR, name))
            parts.append(f"{name}:{info.st_size}:{int(info.st_mtime)}")
    return "|".join(parts)


# The source files whose logic shapes what the cached loaders return. A
# code-only deploy changes no CSV, so keying the caches on the data alone left
# them serving results computed by the previous version of this code. That is
# how the SOFR overnight rate stayed off the card after it was put back: the
# data was identical, so nothing invalidated load_latest.
_SOURCE_FILES = ("db.py", "serve.py", "streamlit_app.py")


def code_fingerprint():
    """Signature of the code behind the cached values.

    Content hashed rather than mtime: a fresh git checkout stamps every file
    with the checkout time, so mtime says a file changed when it did not, and
    says nothing when a rollback restores an older version at a newer time.
    """
    digest = hashlib.sha1()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in _SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        try:
            with open(os.path.join(here, name), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:16]


@st.cache_resource
def database_path(fingerprint):
    """Path to a readable database, built from the committed CSVs when hosted.

    The repository carries the data as CSV under data/, not as rates.db: text
    survives git and browser uploads intact, is a fraction of the size, and can
    be diffed. A 9 MB SQLite binary pushed through a web uploader arrives
    corrupted, which is exactly what happened before this changed.

    `fingerprint` is not used in the body - it is the cache key. A new value
    means the CSVs changed, so a fresh database gets built.

    The build path includes the process id on purpose. With a fixed shared name,
    a redeployed or restarted process would delete and rebuild the very file a
    still-running process was reading, and the older one then failed with
    OperationalError on its next query. Each process now owns its own file.

    Note this caches the file PATH, a plain string, and never a connection. A
    cached connection breaks as soon as Streamlit reruns the script on another
    thread.
    """
    if os.path.exists(DB_PATH):
        return DB_PATH                     # local development: use the live database
    if not db.csv_files_present():
        return None

    # The fingerprint is in the filename too, so a rebuild never has to
    # overwrite a file an in-flight request is still reading. hashlib rather
    # than hash(), which is salted per process and so would not be stable.
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:10]
    tag = f"{os.getpid()}_{digest}"
    build = os.path.join(tempfile.gettempdir(), f"rates_csv_{tag}.db")
    if not os.path.exists(build):
        _build_database(build)
    return build


@contextlib.contextmanager
def _conn():
    """A connection scoped to a single query.

    Do not cache the connection with st.cache_resource. Streamlit reruns the
    script on a different thread whenever a widget changes, and SQLite refuses
    to reuse a connection across threads, so a cached connection raises
    ProgrammingError the moment anyone touches a control. Connections are cheap
    and the results are cached below, so the database is barely touched.
    """
    path = database_path(data_fingerprint())
    if path is None:
        raise RuntimeError("No rate data available: data/*.csv is missing.")
    # Self-heal if the build vanished underneath us - temp cleanup on a
    # long-running host, or a restart between one query and the next.
    if not os.path.exists(path) and path != DB_PATH:
        _build_database(path)

    conn = db.connect_readonly(path)
    try:
        yield conn
    finally:
        conn.close()


# Each loader takes the fingerprint as its first argument purely as a cache key.
# Without it these would keep returning results computed from the previous set of
# CSVs, so the page would show stale rates even after the database was rebuilt.
# The fingerprint covers the code as well as the data, so a deploy that changes
# how a value is derived takes effect on the next run rather than whenever the
# ttl happens to lapse. The ttl remains only as a backstop.

@st.cache_data(ttl=900)
def load_meta(fp):
    with _conn() as c:
        return serve.get_meta(c)


@st.cache_data(ttl=900)
def load_latest(fp, curve):
    with _conn() as c:
        return serve.get_latest(c, curve)


@st.cache_data(ttl=900)
def load_series(fp, curve, tenors, start, end):
    with _conn() as c:
        return serve.get_series(c, curve, list(tenors), start, end)


@st.cache_data(ttl=900)
def load_shape(fp, curve, date):
    with _conn() as c:
        return serve.get_curve_shape(c, curve, date)


@st.cache_data(ttl=900)
def load_csv(fp, curve, start, end):
    with _conn() as c:
        return serve.export_csv(c, curve, start, end)


def palette():
    try:
        dark = st.get_option("theme.base") == "dark"
    except Exception:
        dark = False
    return PALETTE_DARK if dark else PALETTE_LIGHT


def nice_date(iso):
    return datetime.date.fromisoformat(iso).strftime("%d %b %Y").lstrip("0")


def short_date(iso):
    """Day and month only, for dates that sit beside a figure where the year
    would crowd the cell without telling the reader anything."""
    return datetime.date.fromisoformat(iso).strftime("%d %b").lstrip("0")


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

# One signature per script run, keying every cache. Both halves are needed: the
# data half catches the daily CSV commit, the code half catches a deploy that
# changes how the data is read without changing the data itself.
FP = f"{data_fingerprint()}#{code_fingerprint()}"
if database_path(FP) is None:
    st.error(
        "No rate data found in this deployment. The repository should contain "
        "`data/BVAL.csv`, `data/KLIBOR.csv` and so on, which the daily workflow "
        "keeps up to date. Check that the `data/` folder was committed.")
    st.stop()

meta = load_meta(FP)
total = sum(m["rows"] for m in meta)

st.title("Benchmark Rates")

# Show the newest date held, so stale data is visible rather than something you
# have to notice by comparing against the source.
_newest = max((m["last_date"] for m in meta if m["last_date"]), default=None)
st.caption(f"PHP, MYR and USD base interest rates · {total:,} observations · "
           f"data to {nice_date(_newest) if _newest else 'no data'} · "
           f"updated daily at noon Malaysia time")

# -- health banners --------------------------------------------------------
# Only a genuine failure is surfaced. A weekend or a public holiday logs
# "no publication", which is normal and stays silent.
for m in meta:
    if m["failing"]:
        st.error(
            f"**{m['label']} is not updating.** The daily job has failed "
            f"{m['fail_count']} time(s) in a row. Stored data is unaffected and the "
            f"last good values are shown below.\n\n"
            f"Last error: {m['last_error']}")
    elif m["stale"]:
        st.warning(
            f"**{m['label']} may be stale.** No data for {m['missed_weekdays']} weekdays, "
            f"past the tolerance of {m['missed_tolerance']}.")

# -- latest rates ----------------------------------------------------------
st.subheader("Latest rates")

st.markdown("""
<style>
.mkt-head { display:flex; align-items:center; gap:9px; margin:0 0 14px;
            padding-bottom:9px; border-bottom:1px solid rgba(128,128,128,.28); }
.mkt-head .flag { display:inline-flex; line-height:0; border-radius:3px;
                  overflow:hidden; box-shadow:0 0 0 1px rgba(128,128,128,.35); }
.mkt-head .name { font-size:15px; font-weight:650; letter-spacing:-0.01em; }
.mkt-head .count { font-size:12px; opacity:.6; margin-left:auto; }
/* Money market vs government, matching the local dashboard. */
.grp-head { font-size:10px; font-weight:650; text-transform:uppercase;
            letter-spacing:.07em; opacity:.55; margin:14px 0 2px; }
</style>
""", unsafe_allow_html=True)

meta_by_curve = {m["curve"]: m for m in meta}
layout = db.market_layout()


def render_card(curve):
    """One benchmark: its headline figures, then the rest of the curve folded
    away. The headlines now cover three tenors, so for KLIBOR, THOR and SOFR the
    full table repeated them exactly; it is only drawn where the source
    publishes tenors the headlines leave out."""
    m = meta_by_curve[curve]
    if not m["rows"]:
        st.metric(m["label"], "no data")
        return
    latest = load_latest(FP, curve)
    rows = latest["rows"]
    by_tenor = {r["tenor"]: r for r in rows}
    # Which tenors headline the card is declared per curve in db.CURVES and
    # resolved by get_latest, so both dashboards agree.
    heads = [by_tenor[t] for t in latest["headlines"] if t in by_tenor] or [rows[0]]

    st.markdown(f"**{m['label']}**")
    # Four figures do not fit across one card, so they go two by two. Streamlit
    # columns cannot reflow on width the way the local dashboard's CSS does,
    # which is why this is an explicit row size rather than a wrap.
    per_row = 2 if len(heads) == 4 else len(heads)
    for start in range(0, len(heads), per_row):
        batch = heads[start:start + per_row]
        for head, hcol in zip(batch, st.columns(per_row)):
            change = head["change_bp"]
            with hcol:
                st.metric(
                    # as_of is set only where the source had not yet published
                    # this tenor for the card's date, so the figure is the last
                    # one it did publish. Naming that date is the point: an
                    # unlabelled stale rate is worse than no rate on a card
                    # people price off.
                    label=(f"{head['tenor']} · {short_date(head['as_of'])}"
                           if head.get("as_of") else head["tenor"]),
                    value=f"{head['rate']:.3f}%",
                    delta=None if change is None else f"{change:+.1f} bp",
                    # A rising benchmark raises borrowing cost, so a rise reads
                    # red. An unchanged rate stays neutral rather than red.
                    delta_color="off" if not change else "inverse")
    st.caption(f"{nice_date(latest['date'])} · {m['source']}")

    head_tenors = {h["tenor"] for h in heads}
    if not any(r["tenor"] not in head_tenors for r in rows):
        return
    with st.expander(f"All {len(rows)} tenors"):
        frame = pd.DataFrame([{
            "Tenor": (f"{r['tenor']} ({short_date(r['as_of'])})"
                      if r.get("as_of") else r["tenor"]),
            "Rate %": r["rate"],
            "Chg bp": r["change_bp"],
        } for r in rows])
        st.dataframe(frame, hide_index=True, use_container_width=True,
                     column_config={
                         "Rate %": st.column_config.NumberColumn(format="%.3f"),
                         "Chg bp": st.column_config.NumberColumn(format="%+.1f"),
                     })


def render_market(mkt, across=1):
    """A market column: heading, then its cards, optionally several abreast."""
    st.markdown(
        f'<div class="mkt-head"><span class="flag">{mkt["flag"]}</span>'
        f'<span class="name">{mkt["market"]}</span>'
        f'<span class="count">{len(mkt["curves"])} benchmark'
        f'{"s" if len(mkt["curves"]) != 1 else ""}</span></div>',
        unsafe_allow_html=True)

    for group in mkt["groups"]:
        # group is None where the market sits entirely in one group; a heading
        # that contrasts with nothing would only add clutter.
        if group["group"]:
            st.markdown(f'<p class="grp-head">{group["group"]}</p>',
                        unsafe_allow_html=True)
        curves = group["curves"]
        if across <= 1:
            for curve in curves:
                render_card(curve)
            continue
        # Fixed column count rather than one per card, so a group of two does
        # not render at twice the width of a group of three.
        for start in range(0, len(curves), across):
            for curve, ccol in zip(curves[start:start + across], st.columns(across)):
                with ccol:
                    render_card(curve)


# Markets carrying a single benchmark share one row; a market carrying several
# (Malaysia has five) takes a full-width row of its own and flows its cards
# three abreast. Splitting them this way keeps every card wide enough for three
# headline figures, which a four-column row does not.
narrow = [mkt for mkt in layout if mkt["span"] == 1]
wide = [mkt for mkt in layout if mkt["span"] > 1]

if narrow:
    for col, mkt in zip(st.columns(len(narrow)), narrow):
        with col:
            render_market(mkt)

for mkt in wide:
    st.markdown("")
    render_market(mkt, across=min(mkt["columns"], len(mkt["curves"])))

st.divider()

# -- history ---------------------------------------------------------------
st.subheader("History")

with_data = [m for m in meta if m["rows"]] or meta
labels = {m["label"]: m for m in with_data}

c1, c2 = st.columns([2, 3])
with c1:
    chosen_label = st.selectbox("Market", list(labels), index=0)
curve_meta = labels[chosen_label]
curve = curve_meta["curve"]

live = curve_meta["active_tenors"] or curve_meta["tenors"]
# A short curve fits entirely, so show all of it rather than a subset.
if len(live) <= MAX_SERIES:
    default = list(live)
else:
    default = [t for t in ("3M", "1Y", "5Y", "10Y") if t in live] or live[:3]

with c2:
    tenors = st.multiselect(
        f"Tenors (up to {MAX_SERIES})", curve_meta["tenors"], default=default,
        help="Tenors not in the latest publication are historical only. BNM "
             "discontinued 2M and 12M KLIBOR in January 2023.")
if len(tenors) > MAX_SERIES:
    st.info(f"Showing the first {MAX_SERIES} tenors selected.")
    tenors = tenors[:MAX_SERIES]

range_label = st.radio("Range", list(RANGES), index=3, horizontal=True,
                       label_visibility="collapsed")

end = curve_meta["last_date"]
days = RANGES[range_label]
start = "1900-01-01"
if end and days:
    start = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days)).isoformat()

if not tenors:
    st.info("Select at least one tenor.")
elif not end:
    st.info("No data for this market yet.")
else:
    data = load_series(FP, curve, tuple(tenors), start, end)
    records = []
    for tenor in tenors:
        for date, value in zip(data["dates"], data["series"].get(tenor, [])):
            if value is not None:
                records.append({"Date": date, "Tenor": tenor, "Rate": value})

    if not records:
        st.info("No data in this range. The tenors selected may be discontinued.")
    else:
        frame = pd.DataFrame(records)
        frame["Date"] = pd.to_datetime(frame["Date"])

        # Colour follows the tenor's fixed position, so changing the selection
        # never repaints the series that remain.
        order = [t for t in curve_meta["tenors"] if t in tenors]
        scale = alt.Scale(domain=order, range=palette()[:len(order)])

        chart = (
            alt.Chart(frame)
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("Date:T", title=None),
                y=alt.Y("Rate:Q", title="Rate %",
                        scale=alt.Scale(zero=False, nice=True)),
                color=alt.Color("Tenor:N", scale=scale,
                                legend=alt.Legend(title=None, orient="bottom")),
                tooltip=[alt.Tooltip("Date:T", title="Date"),
                         alt.Tooltip("Tenor:N"),
                         alt.Tooltip("Rate:Q", format=".3f", title="Rate %")],
            )
            .properties(height=380)
        )
        # Deliberately not .interactive(). Vega binds the mouse wheel to zoom,
        # so scrolling the page with the cursor over the chart silently rescales
        # both axes and can push series out of view with no obvious way back.
        # The range buttons above cover what that would have offered.
        st.altair_chart(chart, use_container_width=True)

        with st.expander("Table view"):
            wide = frame.pivot(index="Date", columns="Tenor", values="Rate")
            wide = wide[[t for t in order if t in wide.columns]].sort_index(ascending=False)
            st.dataframe(wide, use_container_width=True)

        st.download_button(
            f"Download {curve} CSV",
            data=load_csv(FP, curve, start, end),
            file_name=f"{curve}_{start}_to_{end}.csv",
            mime="text/csv")

st.caption(
    f"**{curve_meta['label']}** - {curve_meta['description']}. "
    f"Source: {curve_meta['source']}. Coverage "
    f"{nice_date(curve_meta['first_date']) if curve_meta['first_date'] else '-'} to "
    f"{nice_date(curve_meta['last_date']) if curve_meta['last_date'] else '-'}.")

st.divider()

# -- term structure --------------------------------------------------------
st.subheader("Term structure")

if not curve_meta["last_date"]:
    st.info("No data for this market yet.")
elif len(curve_meta["active_tenors"]) < 2:
    st.info(f"{curve_meta['label']} publishes a single tenor, so there is no term "
            f"structure to plot. Use the history chart above.")
else:
    back_label = st.radio(
        "Compare against", ["1 month ago", "3 months ago", "1 year ago", "3 years ago"],
        index=1, horizontal=True)
    back_days = {"1 month ago": 30, "3 months ago": 90,
                 "1 year ago": 365, "3 years ago": 1095}[back_label]

    latest_iso = curve_meta["last_date"]
    earlier_iso = (datetime.date.fromisoformat(latest_iso)
                   - datetime.timedelta(days=back_days)).isoformat()
    now_shape = load_shape(FP, curve, latest_iso)
    then_shape = load_shape(FP, curve, earlier_iso)

    order = [p["tenor"] for p in now_shape["points"]]
    records = []
    for name, shape in (("Latest", now_shape), ("Earlier", then_shape)):
        if not shape["points"]:
            continue
        label = f"{name} ({nice_date(shape['date'])})"
        for p in shape["points"]:
            records.append({"Tenor": p["tenor"], "Rate": p["rate"], "Curve": label})

    if records:
        frame = pd.DataFrame(records)
        base = alt.Chart(frame).encode(
            x=alt.X("Tenor:N", sort=order, title=None),
            y=alt.Y("Rate:Q", title="Rate %", scale=alt.Scale(zero=False, nice=True)),
            color=alt.Color("Curve:N",
                            scale=alt.Scale(range=palette()[:2]),
                            legend=alt.Legend(title=None, orient="bottom")),
            tooltip=[alt.Tooltip("Curve:N"), alt.Tooltip("Tenor:N"),
                     alt.Tooltip("Rate:Q", format=".3f", title="Rate %")],
        )
        st.altair_chart(
            (base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True))
            .properties(height=320),
            use_container_width=True)

st.divider()
st.caption(
    "Rates are stored exactly as published by each source and are not adjusted or "
    "interpolated. Figures are for internal reference; confirm against the primary "
    "source before use in documentation or pricing. CME Term SOFR is not included "
    "for licensing reasons. Malaysia is transitioning from KLIBOR toward MYOR.")
