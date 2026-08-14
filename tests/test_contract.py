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
import os
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
        self.assertEqual(set(latest), {"curve", "date", "rows"})
        for key in ("tenor", "rate", "prev_rate", "prev_date", "change_bp"):
            self.assertIn(key, latest["rows"][0])

        series = serve.get_series(conn, "SOFR", ["O/N"], "2026-01-01", "2026-12-31")
        self.assertEqual(set(series), {"dates", "series"})


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
