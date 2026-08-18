"""Contract and resilience tests.

    python tests/test_contract.py

The first group locks the output format: the CSV export and the database schema
must not drift, whatever changes upstream. The second group proves the
protections behave - that a reordered source column cannot be misread, that a
weekend is not mistaken for a breakage, and that a breakage is not mistaken for
a weekend.

Stdlib unittest only. Does not touch the network or the real database.
"""

import datetime
import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db          # noqa: E402
import serve       # noqa: E402
import sources     # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


# ==========================================================================
# 1. Output contract - these must never change
# ==========================================================================

class TestOutputContract(unittest.TestCase):

    def test_rates_table_columns_unchanged(self):
        conn = memory_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(rates)")]
        self.assertEqual(
            cols, ["curve", "rate_date", "tenor", "rate", "fetched_at"],
            "The rates table shape changed. Downstream exports depend on it.")

    def test_primary_key_unchanged(self):
        conn = memory_db()
        pk = [r[1] for r in conn.execute("PRAGMA table_info(rates)") if r[5]]
        self.assertEqual(pk, ["curve", "rate_date", "tenor"])

    def test_csv_matches_golden_exactly(self):
        """The exported CSV must be byte-for-byte what it was before."""
        if not os.path.isdir(GOLDEN):
            self.skipTest("no golden fixtures captured")

        conn = memory_db()
        for curve in ("BVAL", "KLIBOR", "SOFR"):
            path = os.path.join(GOLDEN, f"{curve}.csv")
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                expected = fh.read()

            # Rebuild the same window from the golden file itself, so the test
            # checks the formatting rather than the contents of the live DB.
            lines = expected.strip().split("\n")
            tenors = lines[0].split(",")[1:]
            rows = []
            for line in lines[1:]:
                parts = line.split(",")
                for tenor, val in zip(tenors, parts[1:]):
                    if val != "":
                        rows.append((parts[0], tenor, float(val)))
            db.upsert_rates(conn, curve, rows)

            actual = serve.export_csv(conn, curve, "2026-07-01", "2026-08-13")
            self.assertEqual(actual, expected,
                             f"{curve} CSV export format changed")

    def test_csv_header_shape(self):
        conn = memory_db()
        db.upsert_rates(conn, "BVAL", [
            ("2026-08-13", "3M", 4.9436), ("2026-08-13", "10Y", 7.3531),
            ("2026-08-12", "3M", 4.9465), ("2026-08-12", "10Y", 7.2997)])
        csv_text = serve.export_csv(conn, "BVAL", "2026-01-01", "2026-12-31")
        lines = csv_text.strip().split("\n")
        self.assertEqual(lines[0], "Date,3M,10Y", "header must be Date then tenors in order")
        self.assertTrue(lines[1].startswith("2026-08-13"), "newest date must come first")
        self.assertEqual(csv_text[-1], "\n", "file must end with a newline")
        self.assertNotIn("\r", csv_text, "line endings must stay LF")

    def test_api_response_keys_unchanged(self):
        conn = memory_db()
        db.upsert_rates(conn, "SOFR", [("2026-08-12", "O/N", 3.62),
                                       ("2026-08-11", "O/N", 3.64)])
        meta = serve.get_meta(conn)[0]
        for key in ("curve", "label", "currency", "market", "rows", "first_date",
                    "last_date", "tenors", "active_tenors", "age_days", "stale"):
            self.assertIn(key, meta, f"/api/meta lost the '{key}' field")

        latest = serve.get_latest(conn, "SOFR")
        for key in ("curve", "date", "rows", "headlines"):
            self.assertIn(key, latest, f"/api/latest lost the '{key}' field")
        for key in ("tenor", "rate", "prev_rate", "prev_date", "change_bp"):
            self.assertIn(key, latest["rows"][0])

        series = serve.get_series(conn, "SOFR", ["O/N"], "2026-01-01", "2026-12-31")
        self.assertEqual(set(series), {"dates", "series"})

    def test_get_latest_shape_is_the_same_with_no_data(self):
        """A curve that has never published must return the same keys, or every
        caller has to special-case it. This bit when THOR was first added."""
        conn = memory_db()
        empty = serve.get_latest(conn, "THOR")
        populated_keys = {"curve", "date", "rows", "headlines"}
        self.assertEqual(set(empty), populated_keys)
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["headlines"], [])


# ==========================================================================
# 2. Resilience - a changed backend must not corrupt the database
# ==========================================================================

HEADER = ("<table><thead><th>Date</th><th>1M</th><th>2M</th><th>3M</th>"
          "<th>6M</th><th>9M</th><th>12M</th></thead><tbody>")
ROW = ("<tr><td>13/08/2026</td><td>3.01</td><td>-</td><td>3.46</td>"
       "<td>3.49</td><td>-</td><td>-</td></tr>")
FOOT = "</tbody></table>"


class TestKliborParsing(unittest.TestCase):

    def test_normal_layout(self):
        rows, degraded = sources.parse_klibor_html(HEADER + ROW + FOOT)
        self.assertFalse(degraded)
        self.assertEqual(dict(((t, r) for _, t, r in rows)),
                         {"1M": 3.01, "3M": 3.46, "6M": 3.49})

    def test_reordered_columns_still_map_correctly(self):
        """The important one. If BNM swaps its columns, values must follow the
        header - not silently land on the wrong tenor."""
        head = ("<table><thead><th>Date</th><th>6M</th><th>3M</th><th>1M</th>"
                "</thead><tbody>")
        row = "<tr><td>13/08/2026</td><td>3.49</td><td>3.46</td><td>3.01</td></tr>"
        rows, degraded = sources.parse_klibor_html(head + row + FOOT)
        self.assertFalse(degraded)
        self.assertEqual(dict(((t, r) for _, t, r in rows)),
                         {"1M": 3.01, "3M": 3.46, "6M": 3.49},
                         "reordered columns were misread - this is the corruption case")

    def test_added_column_does_not_shift_others(self):
        head = ("<table><thead><th>Date</th><th>1W</th><th>1M</th><th>3M</th>"
                "<th>6M</th></thead><tbody>")
        row = "<tr><td>13/08/2026</td><td>2.90</td><td>3.01</td><td>3.46</td><td>3.49</td></tr>"
        rows, _ = sources.parse_klibor_html(head + row + FOOT)
        got = dict(((t, r) for _, t, r in rows))
        self.assertEqual(got["1M"], 3.01)
        self.assertEqual(got["6M"], 3.49)

    def test_missing_header_flags_degraded(self):
        rows, degraded = sources.parse_klibor_html("<table><tbody>" + ROW + FOOT)
        self.assertTrue(degraded, "a missing header must be reported, not assumed away")
        self.assertTrue(rows)

    def test_spaced_tenor_labels(self):
        head = "<table><thead><th>Date</th><th>1 M</th><th>3 M</th></thead><tbody>"
        row = "<tr><td>13/08/2026</td><td>3.01</td><td>3.46</td></tr>"
        rows, _ = sources.parse_klibor_html(head + row + FOOT)
        self.assertEqual(sorted(t for _, t, _ in rows), ["1M", "3M"])


MGS_TABLE = (
    "<table><thead>"
    "<th>Malaysian Government Securities (MGS) - Conventional</th>"
    "<th>MGS Benchmarks</th><th>Trading Yields</th>"
    "<th>Total Volume (MYR mil)</th><th>Daily change (bps)</th>"
    "<th>Tenor</th><th>Maturity</th><th>Coupon (%)</th>"
    "<th>Low (%)</th><th>High (%)</th><th>Close (%)</th>"
    "</thead><tbody>"
    "<tr><td>3Y</td><td>March 2029</td><td>3.24</td><td>3.34</td><td>3.35</td>"
    "<td>3.34</td><td>254.54</td><td>0</td></tr>"
    "<tr><td>5Y</td><td>June 2031</td><td>4.23</td><td>3.53*</td><td>3.55*</td>"
    "<td>3.53*</td><td>-</td><td>-</td></tr>"
    "</tbody></table>")

# MGII has no Coupon column, so Close sits one place earlier.
MGII_TABLE = (
    "<table><thead>"
    "<th>Malaysian Government Investment Issues (MGII) - Islamic</th>"
    "<th>MGII Benchmarks</th><th>Trading Yields</th>"
    "<th>Total Volume (MYR mil)</th><th>Daily change (bps)</th>"
    "<th>Tenor</th><th>Maturity</th>"
    "<th>Low (%)</th><th>High (%)</th><th>Close (%)</th>"
    "</thead><tbody>"
    "<tr><td>3Y</td><td>October 2029</td><td>3.35</td><td>3.35</td><td>3.35</td>"
    "<td>440.00</td><td>-1</td></tr>"
    "<tr><td>10Y</td><td>April 2035</td><td>3.73</td><td>3.75</td><td>3.75</td>"
    "<td>191.94</td><td>3</td></tr>"
    "</tbody></table>")


class TestBenchmarkYieldParsing(unittest.TestCase):
    """MGS and MGII share a page but have different column counts, so Close must
    be located by header name rather than by position."""

    def test_both_tables_parsed(self):
        got = sources.parse_benchmark_yields(MGS_TABLE + MGII_TABLE)
        self.assertEqual(dict(got["MGS"]), {"3Y": 3.34, "5Y": 3.53})
        self.assertEqual(dict(got["MGII"]), {"3Y": 3.35, "10Y": 3.75})

    def test_mgii_close_not_shifted_by_missing_coupon(self):
        """The regression that matters. MGII lacks a Coupon column; reading Close
        positionally from the MGS layout would return Low instead."""
        got = sources.parse_benchmark_yields(MGII_TABLE)
        self.assertEqual(dict(got["MGII"])["10Y"], 3.75,
                         "MGII Close was misread - likely the Low column")

    def test_asterisk_stripped(self):
        """A starred yield means no trade occurred, but the close is still official."""
        got = sources.parse_benchmark_yields(MGS_TABLE)
        self.assertEqual(dict(got["MGS"])["5Y"], 3.53)

    def test_reordered_columns_follow_the_header(self):
        head = ("<table><thead>"
                "<th>Malaysian Government Securities (MGS) - Conventional</th>"
                "<th>Tenor</th><th>Close (%)</th><th>Low (%)</th><th>High (%)</th>"
                "</thead><tbody>"
                "<tr><td>3Y</td><td>3.34</td><td>3.30</td><td>3.40</td></tr>"
                "</tbody></table>")
        got = sources.parse_benchmark_yields(head)
        self.assertEqual(dict(got["MGS"])["3Y"], 3.34,
                         "reordered columns were misread")

    def test_missing_close_column_raises(self):
        head = ("<table><thead>"
                "<th>Malaysian Government Securities (MGS) - Conventional</th>"
                "<th>Tenor</th><th>Maturity</th>"
                "</thead><tbody><tr><td>3Y</td><td>March 2029</td></tr></tbody></table>")
        with self.assertRaises(sources.FetchError):
            sources.parse_benchmark_yields(head)

    def test_no_table_raises(self):
        with self.assertRaises(sources.FetchError):
            sources.parse_benchmark_yields("<html><body>Service unavailable</body></html>")

    def test_dash_yields_skipped(self):
        head = ("<table><thead>"
                "<th>Malaysian Government Investment Issues (MGII) - Islamic</th>"
                "<th>Tenor</th><th>Low (%)</th><th>High (%)</th><th>Close (%)</th>"
                "</thead><tbody>"
                "<tr><td>3Y</td><td>-</td><td>-</td><td>-</td></tr>"
                "<tr><td>5Y</td><td>3.54</td><td>3.54</td><td>3.54</td></tr>"
                "</tbody></table>")
        got = sources.parse_benchmark_yields(head)
        self.assertEqual(dict(got["MGII"]), {"5Y": 3.54})


MYOR_TABLE = (
    "<table><thead>"
    "<th>Reference Date</th><th>Publication Date</th><th>Reference Rate</th>"
    "<th>Aggregate Volume (MYR Mil)</th><th>Index</th>"
    "<th>1M Average</th><th>3M Average</th><th>6M Average</th>"
    "</thead><tbody>"
    "<tr><td>13/08/2026</td><td>14/08/2026</td><td>2.75</td><td>51,549.48</td>"
    "<td>1.1401270471</td><td>2.75303</td><td>2.76076</td><td>2.76974</td></tr>"
    "<tr><td>23/09/2021</td><td>24/09/2021</td><td>-</td><td>-</td>"
    "<td>1.0000000000</td><td>-</td><td>-</td><td>-</td></tr>"
    "</tbody></table>")


class TestMyorParsing(unittest.TestCase):
    """MYOR's table has two date columns, a volume, and a compounding index that
    sits near 1.14 - inside the plausible range for a percentage. The rate
    validator cannot catch that, so header mapping is the only defence."""

    def test_only_rate_columns_are_taken(self):
        rows = sources.parse_myor_html(MYOR_TABLE)
        got = {(d, t): r for d, t, r in rows}
        self.assertEqual(got, {
            ("2026-08-13", "O/N"): 2.75,
            ("2026-08-13", "1M"): 2.75303,
            ("2026-08-13", "3M"): 2.76076,
            ("2026-08-13", "6M"): 2.76974,
        })

    def test_index_column_is_never_stored_as_a_rate(self):
        rows = sources.parse_myor_html(MYOR_TABLE)
        self.assertNotIn(1.1401270471, [r for _, _, r in rows],
                         "the compounding index was stored as a rate")

    def test_volume_column_is_never_stored_as_a_rate(self):
        rows = sources.parse_myor_html(MYOR_TABLE)
        self.assertNotIn(51549.48, [r for _, _, r in rows],
                         "aggregate volume was stored as a rate")

    def test_reference_date_used_not_publication_date(self):
        """MYOR publishes a day in arrears. Using the publication date would tag
        every rate with the wrong day."""
        dates = {d for d, _, _ in sources.parse_myor_html(MYOR_TABLE)}
        self.assertEqual(dates, {"2026-08-13"})
        self.assertNotIn("2026-08-14", dates)

    def test_rows_before_first_publication_are_skipped(self):
        """The index base date carries dashes instead of rates."""
        dates = {d for d, _, _ in sources.parse_myor_html(MYOR_TABLE)}
        self.assertNotIn("2021-09-23", dates)

    def test_reordered_columns_follow_the_header(self):
        table = (
            "<table><thead>"
            "<th>Publication Date</th><th>Reference Date</th>"
            "<th>6M Average</th><th>Index</th><th>Reference Rate</th>"
            "</thead><tbody>"
            "<tr><td>14/08/2026</td><td>13/08/2026</td><td>2.76974</td>"
            "<td>1.1401270471</td><td>2.75</td></tr>"
            "</tbody></table>")
        got = {(d, t): r for d, t, r in sources.parse_myor_html(table)}
        self.assertEqual(got, {("2026-08-13", "6M"): 2.76974,
                               ("2026-08-13", "O/N"): 2.75})

    def test_missing_header_refuses_rather_than_guessing(self):
        body = MYOR_TABLE[MYOR_TABLE.index("<tbody>"):]
        with self.assertRaises(sources.FetchError):
            sources.parse_myor_html("<table>" + body)

    def test_missing_reference_date_column_raises(self):
        table = ("<table><thead><th>Publication Date</th><th>Reference Rate</th></thead>"
                 "<tbody><tr><td>14/08/2026</td><td>2.75</td></tr></tbody></table>")
        with self.assertRaises(sources.FetchError):
            sources.parse_myor_html(table)


THOR_PAYLOAD = [
    {"asof": "2024-03-15T00:00:00", "code": "THOR", "tenor": "O/N", "rate": 2.49426},
    {"asof": "2026-08-17T00:00:00", "code": "THORA", "tenor": "1M", "rate": 0.99243},
    {"asof": "2026-08-17T00:00:00", "code": "THORA", "tenor": "3M", "rate": 0.99394},
    {"asof": "2026-08-17T00:00:00", "code": "THORA", "tenor": "6M", "rate": 1.00677},
]


class TestThorParsing(unittest.TestCase):
    """ThaiBMA honours the asof parameter for the overnight rate but ignores it
    for the compounded averages, which always return the latest values. Each row
    must therefore be dated by its own asof field."""

    def test_each_row_uses_its_own_asof(self):
        rows = sources.parse_thor(THOR_PAYLOAD, datetime.date(2024, 3, 15))
        self.assertEqual(
            {(d, t): r for d, t, r in rows},
            {("2024-03-15", "O/N"): 2.49426,
             ("2026-08-17", "1M"): 0.99243,
             ("2026-08-17", "3M"): 0.99394,
             ("2026-08-17", "6M"): 1.00677})

    def test_query_date_never_stamped_on_the_averages(self):
        """The corruption case. Using the requested date would write today's
        average rates onto every historical date in a backfill."""
        rows = sources.parse_thor(THOR_PAYLOAD, datetime.date(2024, 3, 15))
        stamped = [(d, t) for d, t, _ in rows if d == "2024-03-15" and t != "O/N"]
        self.assertEqual(stamped, [],
                         "an average was dated with the requested date")

    def test_overnight_keeps_its_historical_date(self):
        rows = sources.parse_thor(THOR_PAYLOAD, datetime.date(2024, 3, 15))
        overnight = [(d, r) for d, t, r in rows if t == "O/N"]
        self.assertEqual(overnight, [("2024-03-15", 2.49426)])

    def test_non_list_payload_raises(self):
        with self.assertRaises(sources.FetchError):
            sources.parse_thor({"error": "nope"}, datetime.date(2026, 8, 14))

    def test_unparsable_asof_raises(self):
        bad = [{"asof": "not-a-date", "code": "THOR", "tenor": "O/N", "rate": 1.0}]
        with self.assertRaises(sources.FetchError):
            sources.parse_thor(bad, datetime.date(2026, 8, 14))

    def test_rows_without_a_rate_are_skipped(self):
        payload = [{"asof": "2026-08-17T00:00:00", "code": "THOR",
                    "tenor": "O/N", "rate": None}]
        self.assertEqual(sources.parse_thor(payload, datetime.date(2026, 8, 17)), [])

    def test_index_value_would_be_rejected_by_validation(self):
        """thor-index returns a compounding index around 108, not a rate."""
        with self.assertRaises(sources.FetchError):
            sources.validate_rows("THOR", [("2026-08-17", "O/N", 108.78)])


BOT_THOR_TABLE = (
    "<table>"
    "<tr><td>&nbsp;</td><td>&nbsp;</td><td>15 MAR 2024 </td><td>14 MAR 2024 </td></tr>"
    "<tr><td>1</td><td>THOR</td><td>2.49426</td><td>2.49353</td></tr>"
    "<tr><td>2</td><td>THOR Average</td><td>&nbsp;</td><td>&nbsp;</td></tr>"
    "<tr><td>3</td><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;1 Month</td>"
    "<td>2.49606</td><td>2.49610</td></tr>"
    "<tr><td>4</td><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;3 Months</td>"
    "<td>2.50093</td><td>2.50097</td></tr>"
    "<tr><td>5</td><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;6 Months</td>"
    "<td>2.49124</td><td>n.a.</td></tr>"
    "</table>")


class TestBotThorParsing(unittest.TestCase):
    """BOT's report supplies the THOR averages that ThaiBMA will not serve
    historically. Its row labels are padded with &nbsp;, which silently made
    every average row unmatchable until the entities were stripped."""

    def test_all_four_tenors_parsed(self):
        rows = sources.parse_bot_thor(BOT_THOR_TABLE)
        got = {(d, t): r for d, t, r in rows}
        self.assertEqual(got[("2024-03-15", "O/N")], 2.49426)
        self.assertEqual(got[("2024-03-15", "1M")], 2.49606)
        self.assertEqual(got[("2024-03-15", "3M")], 2.50093)
        self.assertEqual(got[("2024-03-15", "6M")], 2.49124)

    def test_nbsp_padding_does_not_hide_the_averages(self):
        """The regression. Without entity stripping only O/N came back."""
        tenors = {t for _, t, _ in sources.parse_bot_thor(BOT_THOR_TABLE)}
        self.assertEqual(tenors, {"O/N", "1M", "3M", "6M"})

    def test_dates_map_to_the_right_columns(self):
        got = {(d, t): r for d, t, r in sources.parse_bot_thor(BOT_THOR_TABLE)}
        self.assertEqual(got[("2024-03-14", "O/N")], 2.49353)
        self.assertEqual(got[("2024-03-14", "1M")], 2.49610)

    def test_na_cells_are_skipped(self):
        got = {(d, t) for d, t, _ in sources.parse_bot_thor(BOT_THOR_TABLE)}
        self.assertNotIn(("2024-03-14", "6M"), got)

    def test_summary_row_is_not_treated_as_a_rate(self):
        """The bare 'THOR Average' row carries no numbers."""
        rows = sources.parse_bot_thor(BOT_THOR_TABLE)
        self.assertEqual(len([r for d, t, r in rows if d == "2024-03-15"]), 4)

    def test_missing_header_raises(self):
        with self.assertRaises(sources.FetchError):
            sources.parse_bot_thor("<table><tr><td>1</td><td>THOR</td></tr></table>")


class TestSofrAveragesParsing(unittest.TestCase):
    """The compounded averages come from the NY Fed's separate SOFRAI series."""

    PAYLOAD = json.dumps({"refRates": [
        {"effectiveDate": "2026-08-17", "type": "SOFRAI", "average30day": 3.63649,
         "average90day": 3.63370, "average180day": 3.66107, "index": 1.25515131,
         "revisionIndicator": ""},
        {"effectiveDate": "2026-08-14", "type": "SOFRAI", "average30day": 3.63617,
         "average90day": 3.63113, "average180day": 3.66204, "index": 1.25477279,
         "revisionIndicator": ""},
    ]})

    def test_all_three_averages_are_read(self):
        rows = sources._sofrai_parse(self.PAYLOAD, 200, "test")
        got = {(d, t): r for d, t, r in rows}
        self.assertEqual(got[("2026-08-17", "30D")], 3.63649)
        self.assertEqual(got[("2026-08-17", "90D")], 3.63370)
        self.assertEqual(got[("2026-08-17", "180D")], 3.66107)
        self.assertEqual(len(rows), 6)

    def test_the_index_is_not_stored_as_a_rate(self):
        """index is a cumulative level near 1.25, not a percentage. Stored beside
        rates it would wreck every chart and breach the 0-30 validation band."""
        rows = sources._sofrai_parse(self.PAYLOAD, 200, "test")
        self.assertNotIn(1.25515131, [r for _, _, r in rows])
        self.assertEqual({t for _, t, _ in rows}, {"30D", "90D", "180D"})

    def test_renamed_fields_raise_rather_than_read_as_empty(self):
        """A payload with no recognised keys must not look like a quiet day:
        empty means 'nothing published', which is a normal weekend."""
        payload = json.dumps({"refRates": [
            {"effectiveDate": "2026-08-17", "type": "SOFRAI", "avg30d": 3.6}]})
        with self.assertRaises(sources.FetchError):
            sources._sofrai_parse(payload, 200, "test")

    def test_an_empty_series_is_still_empty_not_an_error(self):
        self.assertEqual(sources._sofrai_parse('{"refRates": []}', 200, "test"), [])

    def test_other_series_in_the_payload_are_ignored(self):
        payload = json.dumps({"refRates": [
            {"effectiveDate": "2026-08-17", "type": "SOFR", "percentRate": 3.62}]})
        self.assertEqual(sources._sofrai_parse(payload, 200, "test"), [])

    def test_fields_map_by_name_not_position(self):
        """Guards against a reordered payload shifting 90-day data into 30D."""
        self.assertEqual(sources.SOFRAI_FIELDS,
                         {"average30day": "30D", "average90day": "90D",
                          "average180day": "180D"})

    def test_averages_sort_inside_the_monthly_tenors(self):
        """30/90/180 calendar days are a little short of 1/3/6 months, and a tie
        would make the column order arbitrary."""
        self.assertEqual(
            sorted(["180D", "O/N", "90D", "30D"], key=db.tenor_sort_key),
            ["O/N", "30D", "90D", "180D"])
        for days, month in (("30D", "1M"), ("90D", "3M"), ("180D", "6M")):
            self.assertLess(db.tenor_sort_key(days), db.tenor_sort_key(month))

    def test_sofr_now_declares_all_four_tenors(self):
        self.assertEqual(sources.CURVE_TENORS["SOFR"], {"O/N", "30D", "90D", "180D"})


class TestSofrOutcomeMerge(unittest.TestCase):
    """The overnight rate and the averages are separate NY Fed endpoints, so one
    changing shape must not cost us the other."""

    ON = [("2026-08-14", "O/N", 3.62)]
    AVG = [("2026-08-17", "30D", 3.63649)]

    def test_both_succeeding_returns_every_row(self):
        out = sources._merge_sofr(
            sources.Outcome(sources.OK, self.ON, "nyfed-search"),
            sources.Outcome(sources.OK, self.AVG, "nyfed-sofrai-search"))
        self.assertTrue(out.ok)
        self.assertEqual(len(out.rows), 2)

    def test_averages_failing_still_delivers_the_overnight_rate(self):
        out = sources._merge_sofr(
            sources.Outcome(sources.OK, self.ON, "nyfed-search"),
            sources.Outcome(sources.FAILED, [], None, "endpoint moved"))
        self.assertTrue(out.ok)
        self.assertEqual(out.rows, self.ON)
        self.assertIn("endpoint moved", out.detail)

    def test_overnight_failing_still_delivers_the_averages(self):
        out = sources._merge_sofr(
            sources.Outcome(sources.FAILED, [], None, "boom"),
            sources.Outcome(sources.OK, self.AVG, "nyfed-sofrai-search"))
        self.assertTrue(out.ok)
        self.assertEqual(out.rows, self.AVG)

    def test_both_failing_is_a_failure(self):
        out = sources._merge_sofr(
            sources.Outcome(sources.FAILED, [], None, "a"),
            sources.Outcome(sources.FAILED, [], None, "b"))
        self.assertTrue(out.failed)

    def test_both_quiet_is_empty_not_a_failure(self):
        """A US public holiday publishes neither series. That is not a fault."""
        out = sources._merge_sofr(
            sources.Outcome(sources.EMPTY, [], None, "no data published"),
            sources.Outcome(sources.EMPTY, [], None, "no data published"))
        self.assertEqual(out.status, sources.EMPTY)
        self.assertFalse(out.failed)

    def test_degradation_carries_through(self):
        out = sources._merge_sofr(
            sources.Outcome(sources.OK, self.ON, "nyfed-latest", degraded=True),
            sources.Outcome(sources.OK, self.AVG, "nyfed-sofrai-search"))
        self.assertTrue(out.degraded)


class TestValidation(unittest.TestCase):

    def test_accepts_good_data(self):
        sources.validate_rows("BVAL", [("2026-08-13", "3M", 4.9436)])

    def test_rejects_unknown_tenor(self):
        with self.assertRaises(sources.FetchError):
            sources.validate_rows("KLIBOR", [("2026-08-13", "7Y", 3.5)])

    def test_rejects_basis_point_units(self):
        """If a source switched from percent to basis points, 346.0 must be
        refused rather than stored as a 346% rate."""
        with self.assertRaises(sources.FetchError):
            sources.validate_rows("KLIBOR", [("2026-08-13", "3M", 346.0)])

    def test_rejects_zero_and_negative(self):
        for bad in (0.0, -1.5):
            with self.assertRaises(sources.FetchError):
                sources.validate_rows("SOFR", [("2026-08-12", "O/N", bad)])

    def test_rejects_future_date(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        with self.assertRaises(sources.FetchError):
            sources.validate_rows("SOFR", [(future, "O/N", 3.6)])

    def test_rejects_contradictory_rows(self):
        with self.assertRaises(sources.FetchError):
            sources.validate_rows("SOFR", [("2026-08-12", "O/N", 3.62),
                                           ("2026-08-12", "O/N", 4.10)])


class TestAnomalyGuard(unittest.TestCase):

    def _seed(self, conn, n=40, rate=3.46):
        rows = []
        day = datetime.date(2026, 6, 1)
        for _ in range(n):
            rows.append((day.isoformat(), "3M", rate))
            day += datetime.timedelta(days=1)
        db.upsert_rates(conn, "KLIBOR", rows)
        return rows

    def test_identical_refetch_is_clean(self):
        conn = memory_db()
        rows = self._seed(conn)
        self.assertIsNone(db.detect_anomaly(conn, "KLIBOR", rows))

    def test_single_revision_is_allowed(self):
        conn = memory_db()
        rows = self._seed(conn)
        rows[0] = (rows[0][0], "3M", 3.90)          # one genuine revision
        self.assertIsNone(db.detect_anomaly(conn, "KLIBOR", rows))

    def test_wholesale_shift_is_blocked(self):
        """A structural break moves most values at once. That must be caught.

        Models a BVAL column shift where the 3M column starts carrying 10Y
        values - a gap of roughly 240bp.
        """
        conn = memory_db()
        rows = []
        day = datetime.date(2026, 6, 1)
        for _ in range(40):
            rows.append((day.isoformat(), "3M", 4.94))
            day += datetime.timedelta(days=1)
        db.upsert_rates(conn, "BVAL", rows)

        shifted = [(d, t, 7.35) for d, t, _ in rows]
        msg = db.detect_anomaly(conn, "BVAL", shifted)
        self.assertIsNotNone(msg, "a wholesale shift slipped past the guard")
        self.assertIn("upstream format change", msg)

    def test_swap_of_near_equal_tenors_is_NOT_caught(self):
        """Documents a real limit of this guard.

        KLIBOR 3M and 6M currently sit 3bp apart. If BNM swapped those two
        columns, no threshold-based check could tell - the numbers barely move.
        Header-driven parsing in parse_klibor_html() is what actually prevents
        this; the anomaly guard only backstops larger breaks. If this test ever
        starts failing, the guard has become too sensitive and will block
        ordinary rate moves.
        """
        conn = memory_db()
        rows = self._seed(conn, rate=3.46)
        swapped = [(d, t, 3.49) for d, t, _ in rows]
        self.assertIsNone(db.detect_anomaly(conn, "KLIBOR", swapped))

    def test_small_sample_is_not_judged(self):
        conn = memory_db()
        self._seed(conn, n=5)
        rows = [("2026-06-01", "3M", 9.99)]
        self.assertIsNone(db.detect_anomaly(conn, "KLIBOR", rows),
                          "too little overlap to judge; must not block")


class TestEmptyVersusFailed(unittest.TestCase):
    """A weekend must never look like a breakage, and vice versa."""

    def test_all_strategies_empty_is_empty_not_failed(self):
        out = sources._try_strategies([("a", lambda: ([], False)),
                                       ("b", lambda: ([], False))])
        self.assertEqual(out.status, sources.EMPTY)
        self.assertFalse(out.failed)

    def test_all_strategies_error_is_failed(self):
        def boom():
            raise sources.FetchError("connection refused")
        out = sources._try_strategies([("a", boom), ("b", boom)])
        self.assertEqual(out.status, sources.FAILED)
        self.assertIn("connection refused", out.detail)

    def test_fallback_recovers_and_is_reported(self):
        def boom():
            raise sources.FetchError("primary down")
        out = sources._try_strategies([
            ("primary", boom),
            ("fallback", lambda: ([("2026-08-13", "O/N", 3.62)], False))])
        self.assertEqual(out.status, sources.OK)
        self.assertEqual(out.strategy, "fallback")
        self.assertIn("primary down", out.detail)

    def test_degraded_flag_survives(self):
        out = sources._try_strategies([("only", lambda: ([("2026-08-13", "O/N", 3.62)], True))])
        self.assertTrue(out.degraded)


class TestMissedWeekdays(unittest.TestCase):

    def test_weekend_counts_as_zero(self):
        conn = memory_db()
        db.upsert_rates(conn, "BVAL", [("2026-08-14", "3M", 4.94)])   # Friday
        # Following Sunday: no weekdays have passed, so nothing is missed.
        self.assertEqual(db.missed_weekdays(conn, "BVAL", datetime.date(2026, 8, 16)), 0)

    def test_long_holiday_stays_within_tolerance(self):
        conn = memory_db()
        db.upsert_rates(conn, "BVAL", [("2026-08-14", "3M", 4.94)])   # Friday
        # Following Wednesday: Mon, Tue and Wed have no data. Today is counted
        # because the morning run happens before publication, which is exactly
        # why the alarm tolerance is 3 rather than 1.
        missed = db.missed_weekdays(conn, "BVAL", datetime.date(2026, 8, 19))
        self.assertEqual(missed, 3)
        self.assertLessEqual(missed, 3, "a two-day holiday must not raise an alarm")

    def test_real_outage_exceeds_tolerance(self):
        conn = memory_db()
        db.upsert_rates(conn, "BVAL", [("2026-08-14", "3M", 4.94)])
        self.assertGreater(db.missed_weekdays(conn, "BVAL", datetime.date(2026, 9, 1)), 3)


class TestCsvRoundTrip(unittest.TestCase):
    """The repository carries the data as CSV, so export then import must be
    exactly lossless. If it is not, published data silently drifts from the
    database it was written from."""

    SAMPLE = [
        ("2026-08-14", "3Y", 3.34), ("2026-08-14", "10Y", 3.7412),
        ("2026-08-13", "3Y", 3.35), ("2026-08-13", "10Y", 3.74),
        ("2026-08-12", "10Y", 3.7),          # a date missing one tenor
    ]

    def test_round_trip_preserves_every_value(self):
        src = memory_db()
        db.upsert_rates(src, "MGS", self.SAMPLE)
        text = db.export_csv(src, "MGS", "1900-01-01", "2999-12-31")

        dst = memory_db()
        db.bulk_insert(dst, "MGS", db.parse_csv(text))

        before = {(r[0], r[1]): r[2] for r in
                  src.execute("SELECT rate_date, tenor, rate FROM rates")}
        after = {(r[0], r[1]): r[2] for r in
                 dst.execute("SELECT rate_date, tenor, rate FROM rates")}
        self.assertEqual(before, after)

    def test_re_export_is_byte_identical(self):
        src = memory_db()
        db.upsert_rates(src, "MGS", self.SAMPLE)
        text = db.export_csv(src, "MGS", "1900-01-01", "2999-12-31")

        dst = memory_db()
        db.bulk_insert(dst, "MGS", db.parse_csv(text))
        self.assertEqual(text, db.export_csv(dst, "MGS", "1900-01-01", "2999-12-31"))

    def test_gaps_stay_gaps(self):
        """An empty cell must not become a zero, which would look like a real rate."""
        src = memory_db()
        db.upsert_rates(src, "MGS", self.SAMPLE)
        text = db.export_csv(src, "MGS", "1900-01-01", "2999-12-31")
        parsed = db.parse_csv(text)
        self.assertNotIn(("2026-08-12", "3Y"), {(d, t) for d, t, _ in parsed})
        self.assertEqual(len(parsed), len(self.SAMPLE))

    def test_full_precision_survives(self):
        src = memory_db()
        db.upsert_rates(src, "BVAL", [("2026-08-13", "3M", 4.9436)])
        text = db.export_csv(src, "BVAL", "1900-01-01", "2999-12-31")
        self.assertEqual(db.parse_csv(text)[0][2], 4.9436)

    def test_bulk_insert_matches_upsert(self):
        a, b = memory_db(), memory_db()
        db.upsert_rates(a, "MGS", self.SAMPLE)
        db.bulk_insert(b, "MGS", self.SAMPLE)
        self.assertEqual(
            [tuple(r) for r in a.execute(
                "SELECT curve, rate_date, tenor, rate FROM rates ORDER BY rate_date, tenor")],
            [tuple(r) for r in b.execute(
                "SELECT curve, rate_date, tenor, rate FROM rates ORDER BY rate_date, tenor")])


class TestMarketLayout(unittest.TestCase):
    """Cards are grouped into market columns, left to right, each with a flag."""

    def test_columns_run_west_to_east_then_the_us(self):
        self.assertEqual([m for m, _, _ in db.curves_by_market()],
                         ["Philippines", "Malaysia", "Thailand", "United States"])

    def test_every_curve_lands_in_exactly_one_column(self):
        placed = [c for _, _, curves in db.curves_by_market() for c in curves]
        self.assertEqual(sorted(placed), sorted(db.CURVES),
                         "a curve is missing from the layout or duplicated")
        self.assertEqual(len(placed), len(set(placed)))

    def test_every_market_has_a_flag(self):
        for market, flag, _ in db.curves_by_market():
            self.assertTrue(flag.startswith("<svg"), f"{market} has no flag")
            self.assertIn("</svg>", flag)

    def test_flags_are_svg_not_emoji(self):
        """Windows has no flag glyphs, so emoji would render as a boxed 'PH'."""
        for market, flag, _ in db.curves_by_market():
            self.assertNotIn("\U0001F1E6", flag, f"{market} uses a regional indicator")

    def test_a_market_with_several_benchmarks_spans_two_slots(self):
        """Malaysia carries five benchmarks against one each for Thailand and
        the US. Left as equal columns it ran 1,728px against their ~300px, so
        it takes a wider slot and flows its cards several abreast instead."""
        spans = {m["market"]: m["span"] for m in db.market_layout()}
        self.assertEqual(spans["Malaysia"], 2)
        for market in ("Philippines", "Thailand", "United States"):
            self.assertEqual(spans[market], 1, market)

    def test_wide_markets_flow_several_cards_across(self):
        """Both dashboards read `columns` from here, so the hosted app, whose
        columns cannot reflow on width, matches the local one."""
        for entry in db.market_layout():
            expected = db.WIDE_MARKET_COLUMNS if entry["span"] > 1 else 1
            self.assertEqual(entry["columns"], expected, entry["market"])
        self.assertGreater(db.WIDE_MARKET_COLUMNS, 1)

    def test_malaysia_splits_into_money_market_then_government(self):
        malaysia = next(m for m in db.market_layout() if m["market"] == "Malaysia")
        self.assertEqual([g["group"] for g in malaysia["groups"]],
                         ["Money market", "Government"])
        by_group = {g["group"]: g["curves"] for g in malaysia["groups"]}
        self.assertEqual(sorted(by_group["Money market"]), ["KLIBOR", "MYOR", "MYORI"])
        self.assertEqual(sorted(by_group["Government"]), ["MGII", "MGS"])

    def test_single_group_markets_carry_no_heading(self):
        """A heading that contrasts with nothing is clutter, so a market whose
        benchmarks all sit in one group reports group=None."""
        for entry in db.market_layout():
            if entry["market"] == "Malaysia":
                continue
            self.assertEqual([g["group"] for g in entry["groups"]], [None],
                             entry["market"])

    def test_layout_groups_hold_every_curve_exactly_once(self):
        placed = [c for e in db.market_layout() for g in e["groups"] for c in g["curves"]]
        self.assertEqual(sorted(placed), sorted(db.CURVES))
        self.assertEqual(len(placed), len(set(placed)))

    def test_flat_curve_list_agrees_with_the_groups(self):
        for entry in db.market_layout():
            grouped = [c for g in entry["groups"] for c in g["curves"]]
            self.assertEqual(sorted(grouped), sorted(entry["curves"]), entry["market"])

    def test_every_curve_declares_a_known_group(self):
        for curve, meta in db.CURVES.items():
            self.assertIn(meta.get("group"), db.GROUP_ORDER, curve)

    def test_layout_keeps_the_market_order(self):
        self.assertEqual([m["market"] for m in db.market_layout()], db.MARKET_ORDER)

    def test_malaysia_star_is_a_polygon_not_a_ring(self):
        """It was drawn as a circle inside a circle, which reads as a ring."""
        flag = db.MARKET_FLAGS["Malaysia"]
        self.assertIn("<polygon", flag)
        points = re.search(r'points="([^"]+)"', flag).group(1).split()
        self.assertEqual(len(points), 28, "a 14-point star needs 28 vertices")
        # Two circles only: the crescent. Any more means the ring came back.
        self.assertEqual(flag.count("<circle"), 2)

    def test_flag_shapes_stay_inside_their_viewbox(self):
        for market, flag, _ in db.curves_by_market():
            for x, y in re.findall(r'c[xy]="([\d.]+)"\s+cy="([\d.]+)"', flag):
                self.assertLessEqual(float(x), 24, f"{market} shape overflows")
                self.assertLessEqual(float(y), 16, f"{market} shape overflows")

    def test_star_generator_geometry(self):
        pts = db._star_points(10, 8, 4, 2, 5).split()
        self.assertEqual(len(pts), 10)
        radii = [round(((float(p.split(",")[0]) - 10) ** 2
                        + (float(p.split(",")[1]) - 8) ** 2) ** 0.5, 2)
                 for p in pts]
        self.assertEqual(sorted(set(radii)), [2.0, 4.0],
                         "vertices must alternate between inner and outer radius")


class TestHeadlineTenors(unittest.TestCase):
    """The big figures on each card are declared per curve, not inferred."""

    def test_bval_shows_three_year_five_year_and_seven_year(self):
        self.assertEqual(
            db.headline_tenors("BVAL", ["1M", "3M", "3Y", "5Y", "7Y", "10Y", "25Y"]),
            ["3Y", "5Y", "7Y"])

    def test_government_curves_show_five_seven_and_ten_year(self):
        """Project debt is benchmarked at 5Y, 7Y and 10Y, so both the
        conventional and the Islamic government curve headline those."""
        for curve in ("MGS", "MGII"):
            self.assertEqual(
                db.headline_tenors(curve, ["3Y", "5Y", "7Y", "10Y", "20Y"]),
                ["5Y", "7Y", "10Y"], curve)

    def test_money_market_curves_show_one_three_and_six_month(self):
        for curve in ("KLIBOR", "MYOR", "MYORI", "THOR"):
            self.assertEqual(
                db.headline_tenors(curve, ["O/N", "1M", "3M", "6M"]),
                ["1M", "3M", "6M"], curve)

    def test_sofr_headlines_its_compounded_averages(self):
        """It headlined O/N alone only because that was the single tenor the NY
        Fed endpoint carried. With the averages added it follows MYOR and THOR:
        term rates as the figures, the overnight rate in the table below."""
        self.assertEqual(
            db.headline_tenors("SOFR", ["O/N", "30D", "90D", "180D"]),
            ["30D", "90D", "180D"])

    def test_sofr_falls_back_to_overnight_before_the_averages_existed(self):
        """SOFR Averages start Mar 2020. On any earlier date only O/N published,
        and the card must still show something rather than going blank."""
        self.assertEqual(db.headline_tenors("SOFR", ["O/N"]), ["O/N"])

    def test_results_are_in_tenor_order(self):
        out = db.headline_tenors("BVAL", ["7Y", "3Y", "5Y"])
        self.assertEqual(out, ["3Y", "5Y", "7Y"], "headlines must read short to long")

    def test_every_curve_declares_at_least_one(self):
        for curve, meta in db.CURVES.items():
            declared = meta.get("headline_tenors")
            self.assertTrue(declared, f"{curve} declares no headline tenors")
            self.assertIsInstance(declared, list)

    def test_declared_tenors_are_valid_for_their_curve(self):
        for curve, meta in db.CURVES.items():
            allowed = sources.CURVE_TENORS.get(curve)
            if allowed:
                for tenor in meta["headline_tenors"]:
                    self.assertIn(tenor, allowed,
                                  f"{curve} headline {tenor} is not one it publishes")

    def test_unpublished_headlines_are_dropped_not_blanked(self):
        """If only some of the declared tenors published, show those."""
        self.assertEqual(db.headline_tenors("BVAL", ["3Y", "5Y"]), ["3Y", "5Y"])

    def test_falls_back_when_none_published_that_day(self):
        self.assertEqual(db.headline_tenors("BVAL", ["1M", "3M", "6M"]), ["6M"])

    def test_empty_returns_empty(self):
        self.assertEqual(db.headline_tenors("BVAL", []), [])


class TestThreadSafety(unittest.TestCase):
    """The hosted dashboard reruns its script on a different thread every time a
    widget changes. A connection shared across those threads raises
    sqlite3.ProgrammingError, which is what happened when a tenor was removed
    from the selector. Connections must therefore be opened per call."""

    def setUp(self):
        self.path = os.path.join(GOLDEN, "_threadtest.db")
        if os.path.exists(self.path):
            os.remove(self.path)
        conn = db.init(self.path)
        db.upsert_rates(conn, "SOFR", [("2026-08-12", "O/N", 3.62),
                                       ("2026-08-11", "O/N", 3.64)])
        conn.close()
        db.checkpoint(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_shared_connection_across_threads_fails(self):
        """Documents why the app must not cache a connection."""
        import threading
        conn = db.connect_readonly(self.path)
        captured = {}

        def worker():
            try:
                serve.get_latest(conn, "SOFR")
                captured["err"] = None
            except Exception as exc:            # noqa: BLE001
                captured["err"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        conn.close()
        self.assertIsInstance(captured["err"], sqlite3.ProgrammingError,
                              "if this stops raising, sqlite changed and the "
                              "per-call connection rule can be revisited")

    def test_per_call_connection_works_across_threads(self):
        """The pattern streamlit_app.py actually uses."""
        import threading
        results, errors = [], []

        def worker():
            try:
                conn = db.connect_readonly(self.path)
                try:
                    results.append(serve.get_latest(conn, "SOFR")["date"])
                finally:
                    conn.close()
            except Exception as exc:            # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "per-call connections must work from any thread")
        self.assertEqual(results, ["2026-08-12"] * 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
