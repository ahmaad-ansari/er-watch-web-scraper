"""
Tests for DataFormatter using real strings seen on hospital sites, plus the edge cases that
used to silently produce None (short waits, round hours, day-first dates, time-only after
midnight UTC). Run with:  python -m unittest discover -s tests
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pytz

from scraper.utils import data_formatter
from scraper.utils.data_formatter import DataFormatter, parse_duration_minutes

fmt = DataFormatter.format_value
TORONTO = pytz.timezone("America/Toronto")


def wait(raw, pattern=None, unit=None):
    return fmt("estimatedWaitTime", None, raw, pattern, unit)


class FrozenDatetime(datetime):
    """datetime whose now() is pinned, so date-filling and roll-back logic is deterministic."""
    frozen_utc = datetime(2026, 11, 6, 1, 40, tzinfo=pytz.UTC)  # 8:40 PM Nov 5 in Toronto

    @classmethod
    def now(cls, tz=None):
        return cls.frozen_utc.astimezone(tz) if tz else cls.frozen_utc.replace(tzinfo=None)


def last_updated(raw, pattern, format_code, unit="EST", now_utc=None):
    FrozenDatetime.frozen_utc = now_utc or datetime(2026, 11, 6, 1, 40, tzinfo=pytz.UTC)
    with mock.patch.object(data_formatter, "datetime", FrozenDatetime):
        return fmt("lastUpdated", format_code, raw, pattern, unit)


class WaitTimeConfiguredPatterns(unittest.TestCase):
    """Patterns currently configured in scraping_targets, with the values they were built for."""

    def test_existing_shapes_unchanged(self):
        self.assertEqual(wait("5 hours 9 mins.", r"(\d+)\s+hours?\s+(\d+)\s+mins?"), 309)
        self.assertEqual(wait("5h 33m", r"(\d+)h\s*(\d+)m"), 333)
        self.assertEqual(wait("10 hours and 31 minutes", r"(\d+)\s+hours?\s+and\s+(\d+)\s+minutes?"), 631)
        self.assertEqual(wait("05:22", r"(\d{2}):(\d{2})"), 322)
        self.assertEqual(wait("05h28m", r"(\d{1,2})h(\d{1,2})m"), 328)
        self.assertEqual(wait("2 hr 48 min to4 hr 23 min",
                              r"(?:(\d+)\s*hr(?:\s+or\s+less(?:\s+to)?)?)?(?:\s*(\d+)\s*min)?"), 168)
        self.assertEqual(wait("239", unit="minutes"), 239)
        self.assertEqual(wait("-1", unit="minutes"), -1)  # sources use -1 for "offline"
        self.assertEqual(wait("7.5 Hours", r"(\d+(?:\.\d+)?)\s*hours?", "hours"), 450)
        self.assertEqual(wait("6.490235018911564", unit="hours"), 389)


class WaitTimeEdgeCases(unittest.TestCase):
    """Values that used to come back as None while the hospital still showed a wait time."""

    def test_under_an_hour_when_pattern_requires_hours(self):
        self.assertEqual(wait("45 mins.", r"(\d+)\s+hours?\s+(\d+)\s+mins?"), 45)
        self.assertEqual(wait("38m", r"(\d+)h\s*(\d+)m"), 38)
        self.assertEqual(wait("52 minutes", r"(\d+)\s+hours?\s+and\s+(\d+)\s+minutes?"), 52)

    def test_round_hours_when_pattern_requires_minutes(self):
        p = r"(?:(\d+)\s*hour[s]?\s*and\s*)?(\d+)\s*minute[s]?"
        self.assertEqual(wait("2 hours", p), 120)
        self.assertEqual(wait("1 hour", p), 60)

    def test_single_digit_hour_clock(self):
        self.assertEqual(wait("5:22", r"(\d{2}):(\d{2})"), 322)

    def test_minutes_unit_with_text(self):
        self.assertEqual(wait("45 min", unit="minutes"), 45)

    def test_new_oak_valley_format(self):
        self.assertEqual(wait("2 hrs 35 mins", None), 155)
        self.assertEqual(wait("50 mins", None), 50)

    def test_unparseable_is_none(self):
        self.assertIsNone(wait("Unavailable", r"(\d+)h\s*(\d+)m"))
        self.assertIsNone(wait("", None))


class DurationFallback(unittest.TestCase):
    def test_shapes(self):
        cases = {
            "1 hour and 48 minutes": 108, "3.25 Hours": 195, "4 hr 23 min": 263, "05h28m": 328,
            "Less than 1 hour": 60, "3:31": 211, "2 hr 48 min to 4 hr 23 min": 168, "no data": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_duration_minutes(raw), expected, raw)


class Counts(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(fmt("patientsWaiting", None, "37", None, None), 37)
        self.assertEqual(fmt("patientsWaiting", None, "1,234", None, None), 1234)
        self.assertEqual(fmt("patientsInTreatment", None, "12 patients", None, None), 12)
        self.assertIsNone(fmt("patientsWaiting", None, "N/A", None, None))


class LastUpdated(unittest.TestCase):
    def test_full_dates(self):
        self.assertEqual(
            last_updated("Updated as of 11/5/2026 8:35:13 PM", r"(\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} \w{2})",
                         "%m/%d/%Y %I:%M:%S %p"),
            datetime(2026, 11, 6, 1, 35, 13, tzinfo=pytz.UTC))
        self.assertEqual(
            last_updated("2026-11-06T01:33:13.4639010Z", r"(.+)", "%Y-%m-%dT%H:%M:%S.%fZ", unit="UTC"),
            datetime(2026, 11, 6, 1, 33, 13, 463901, tzinfo=pytz.UTC))
        self.assertEqual(
            last_updated("As of November 5 at 8:30 p.m.", r"As of (.+)", "%B %d at %I:%M %p"),
            datetime(2026, 11, 6, 1, 30, tzinfo=pytz.UTC))

    def test_day_first_date_falls_back(self):
        # Windsor/Erie Shores switched to day-first; the configured pattern expects "November 5, 2026".
        pattern = r"([A-Za-z]+\s+\d{1,2},\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*(?:AM|PM|a\.?m\.?|p\.?m\.?))"
        self.assertEqual(last_updated("5 November 2026 at 8:30 pm", pattern, "%B %d, %Y at %I:%M %p"),
                         datetime(2026, 11, 6, 1, 30, tzinfo=pytz.UTC))

    def test_time_only_uses_toronto_date_not_utc(self):
        # 8:40 PM in Toronto is already Nov 6 in UTC; "8:30 pm" must stay Nov 5 (Toronto).
        self.assertEqual(last_updated("8:30 pm", r"(\d{1,2}:\d{2}\s*[ap]m)", "%I:%M %p"),
                         datetime(2026, 11, 6, 1, 30, tzinfo=pytz.UTC))
        self.assertEqual(last_updated("8:35PM", r"(\d{1,2}:\d{2}[APM]{2})", "%I:%M%p"),
                         datetime(2026, 11, 6, 1, 35, tzinfo=pytz.UTC))

    def test_time_only_just_after_midnight_rolls_back(self):
        # Scraped 12:05 AM Nov 6 Toronto; source still shows "11:58 PM" from Nov 5.
        now = TORONTO.localize(datetime(2026, 11, 6, 0, 5)).astimezone(pytz.UTC)
        self.assertEqual(last_updated("11:58 PM", r"(\d{1,2}:\d{2} [AP]M)", "%I:%M %p", now_utc=now),
                         TORONTO.localize(datetime(2026, 11, 5, 23, 58)).astimezone(pytz.UTC))

    def test_no_year_on_new_years_day_rolls_back(self):
        now = TORONTO.localize(datetime(2027, 1, 1, 0, 10)).astimezone(pytz.UTC)
        self.assertEqual(last_updated("Dec 31, 23:45", r"([A-Za-z]{3} \d{1,2}, \d{1,2}:\d{2})", "%b %d, %H:%M",
                                      now_utc=now),
                         TORONTO.localize(datetime(2026, 12, 31, 23, 45)).astimezone(pytz.UTC))

    def test_leap_day_without_year(self):
        now = TORONTO.localize(datetime(2028, 2, 29, 14, 0)).astimezone(pytz.UTC)
        self.assertEqual(last_updated("Feb 29, 13:45", r"([A-Za-z]{3} \d{1,2}, \d{1,2}:\d{2})", "%b %d, %H:%M",
                                      now_utc=now),
                         TORONTO.localize(datetime(2028, 2, 29, 13, 45)).astimezone(pytz.UTC))

    def test_oak_valley_format(self):
        self.assertEqual(
            last_updated("Last refreshed: Nov 5 2026 8:44pm",
                         r"Last refreshed:\s*([A-Za-z]{3} \d{1,2} \d{4} \d{1,2}:\d{2}\s*[AaPp][Mm])", "%b %d %Y %I:%M%p"),
            datetime(2026, 11, 6, 1, 44, tzinfo=pytz.UTC))

    def test_ordinal_day(self):
        self.assertEqual(
            last_updated("Thursday November 05th,  08:31:09 PM", r"([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?,\s+\d{1,2}:\d{2}:\d{2} [AP]M)",
                         "%B %d, %I:%M:%S %p"),
            datetime(2026, 11, 6, 1, 31, 9, tzinfo=pytz.UTC))

    def test_garbage_is_none(self):
        self.assertIsNone(last_updated("not a date", r"(\d+/\d+/\d+)", "%m/%d/%Y"))


if __name__ == "__main__":
    unittest.main()
