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
import os
import tempfile

import altair as alt
import pandas as pd
import streamlit as st

import db
import serve

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


@st.cache_resource
def database_path():
    """Path to a readable database, built from the committed CSVs when hosted.

    The repository carries the data as CSV under data/, not as rates.db: text
    survives git and browser uploads intact, is a fraction of the size, and can
    be diffed. A 9 MB SQLite binary pushed through a web uploader arrives
    corrupted, which is exactly what happened before this changed.

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

    build = os.path.join(tempfile.gettempdir(), f"rates_csv_{os.getpid()}.db")
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
    path = database_path()
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


@st.cache_data(ttl=900)
def load_meta():
    with _conn() as c:
        return serve.get_meta(c)


@st.cache_data(ttl=900)
def load_latest(curve):
    with _conn() as c:
        return serve.get_latest(c, curve)


@st.cache_data(ttl=900)
def load_series(curve, tenors, start, end):
    with _conn() as c:
        return serve.get_series(c, curve, list(tenors), start, end)


@st.cache_data(ttl=900)
def load_shape(curve, date):
    with _conn() as c:
        return serve.get_curve_shape(c, curve, date)


@st.cache_data(ttl=900)
def load_csv(curve, start, end):
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


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

if database_path() is None:
    st.error(
        "No rate data found in this deployment. The repository should contain "
        "`data/BVAL.csv`, `data/KLIBOR.csv` and so on, which the daily workflow "
        "keeps up to date. Check that the `data/` folder was committed.")
    st.stop()

meta = load_meta()
total = sum(m["rows"] for m in meta)

st.title("Benchmark Rates")
st.caption(f"PHP, MYR and USD base interest rates · {total:,} observations · "
           f"refreshed automatically each weekday")

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

# Wrap into rows of three. One column per curve becomes unreadably narrow once
# there are more than three or four curves.
PER_ROW = 3
rows_of_meta = [meta[i:i + PER_ROW] for i in range(0, len(meta), PER_ROW)]
cards = []
for chunk in rows_of_meta:
    cols = st.columns(PER_ROW)          # pad the last row so widths stay even
    cards.extend(zip(cols, chunk))

for col, m in cards:
    with col:
        if not m["rows"]:
            st.metric(m["label"], "no data")
            continue
        latest = load_latest(m["curve"])
        rows = latest["rows"]
        # 10Y is the conventionally quoted reference for government yield
        # curves; otherwise fall back to a middle tenor.
        head = next((r for r in rows if r["tenor"] == "10Y"),
                    rows[min(2, len(rows) - 1)])
        change = head["change_bp"]
        st.metric(
            label=f"{m['label']} · {head['tenor']}",
            value=f"{head['rate']:.3f}%",
            delta=None if change is None else f"{change:+.1f} bp",
            # A rising benchmark raises borrowing cost, so a rise reads red.
            # An unchanged rate stays neutral rather than showing as a red rise.
            delta_color="off" if not change else "inverse")
        st.caption(f"{nice_date(latest['date'])} · {m['source']}")

        frame = pd.DataFrame([{
            "Tenor": r["tenor"],
            "Rate %": r["rate"],
            "Chg bp": r["change_bp"],
        } for r in rows])
        st.dataframe(frame, hide_index=True, use_container_width=True,
                     column_config={
                         "Rate %": st.column_config.NumberColumn(format="%.3f"),
                         "Chg bp": st.column_config.NumberColumn(format="%+.1f"),
                     })

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
    data = load_series(curve, tuple(tenors), start, end)
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
            data=load_csv(curve, start, end),
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
    now_shape = load_shape(curve, latest_iso)
    then_shape = load_shape(curve, earlier_iso)

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
