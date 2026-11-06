import logging
import re
from datetime import datetime, timedelta
import pytz
from dateutil import parser as date_parser

from scraper.utils.logger import get_logger

logger = get_logger(__name__)

# Every source is an Ontario hospital: wall-clock times without a zone are Toronto time.
TORONTO = pytz.timezone("America/Toronto")
TORONTO_UNITS = {"EST", "EDT", "ET", "EASTERN", "US/EASTERN", "AMERICA/TORONTO"}

# A "last updated" time can't be in the future; allow some slack for clock skew.
FUTURE_TOLERANCE = timedelta(hours=2)

_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)(?![a-z])")
_MINUTES_RE = re.compile(r"(\d+)\s*(?:minutes?|mins?|m)(?![a-z])")
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")


def parse_duration_minutes(raw_value):
    """
    Tolerant fallback for wait-time text when a hospital's configured pattern doesn't match.

    Handles "45 mins", "2 hrs", "1 hour and 48 minutes", "5h 33m", "05h28m", "5:22", "3.5 hours".
    For ranges ("2 hr 48 min to 4 hr 23 min") the first value wins, matching the configured patterns.
    Returns total minutes, or None if no duration is recognisable.
    """
    if not raw_value:
        return None
    text = raw_value.lower().replace(",", " ")

    clock = _CLOCK_RE.search(text)
    hours = _HOURS_RE.search(text)
    minutes = _MINUTES_RE.search(text)

    # "H:MM" only when there are no unit words (so "8:30 pm" style strings never get here as durations).
    if clock and not hours and not minutes:
        return int(clock.group(1)) * 60 + int(clock.group(2))

    # Keep the first duration in the text: drop a minutes match that belongs to a later range value.
    if hours and minutes and minutes.start() < hours.start():
        hours = None
    if not hours and not minutes:
        return None
    total = (float(hours.group(1)) * 60 if hours else 0) + (int(minutes.group(1)) if minutes else 0)
    return round(total)


def _localize(dt, unit, format_code):
    """Attach a timezone: explicit UTC ("Z" or unit UTC) stays UTC, everything else is Toronto time."""
    if dt.tzinfo is not None:
        return dt.astimezone(pytz.UTC)
    if (format_code and "Z" in format_code) or (unit and unit.upper() == "UTC"):
        return dt.replace(tzinfo=pytz.UTC)
    if unit and unit.upper() not in TORONTO_UNITS:
        logger.debug(f"Unknown timezone unit '{unit}' - assuming America/Toronto.")
    return TORONTO.localize(dt, is_dst=False).astimezone(pytz.UTC)


def _roll_back_if_future(dt_utc, date_was_inferred):
    """
    Sources that show only a time ("8:30 pm") or no year ("Nov 05, 20:30") get today's date / this
    year filled in. Just after midnight (or New Year) that lands in the future: step back a day or a year.
    """
    if not date_was_inferred:
        return dt_utc
    now = datetime.now(pytz.UTC)
    if dt_utc <= now + FUTURE_TOLERANCE:
        return dt_utc
    for step in (timedelta(days=1), None):
        candidate = dt_utc - step if step else dt_utc.replace(year=dt_utc.year - 1)
        if candidate <= now + FUTURE_TOLERANCE:
            logger.info(f"lastUpdated {dt_utc} was in the future; rolled back to {candidate}")
            return candidate
    return dt_utc


class DataFormatter:
    """
    A utility class for converting raw field values into standardized types/formats
    (e.g., date/time objects, integers, total minutes).

    Example Fields the Formatter Handles:
    - lastUpdated
    - patientsWaiting
    - patientsInTreatment
    - estimatedWaitTime
    """

    @staticmethod
    def format_value(field, format_code, raw_value, pattern, unit):
        """
        Parse or transform the 'raw_value' based on 'field', 'format_code', and 'pattern'.
        This method standardizes various types of input data such as date/time strings,
        numeric counts, and time durations into more usable Python objects.

        Args:
            field (str): The name of the field to format (e.g., 'lastUpdated', 'patientsWaiting').
            format_code (str): A datetime format string to guide parsing, if needed.
            raw_value (str or None): The original, unprocessed data.
            pattern (str or None): A regex pattern for extracting data from the 'raw_value'.
            unit (str or None): A unit of measurement or **timezone** (e.g., 'minutes', 'hours', 'UTC', 'EST').

        Returns:
            datetime | int | str | None:
                - A datetime object if 'lastUpdated' is successfully parsed.
                - An integer if numeric fields or wait times are parsed.
                - The original 'raw_value' if the field is unrecognized (with a warning).
                - None if parsing or conversion fails.
        """
        logger.debug(f"Starting format_value for field='{field}', raw_value='{raw_value}', unit='{unit}'")

        # If there's no value to process, log a warning and return None.
        if raw_value is None:
            logger.warning(f"Field '{field}' has no value. Returning None.")
            return None

        # ---------------------------------------------------------------------
        # 1) Handle "lastUpdated" (Date/Time Parsing)
        # ---------------------------------------------------------------------
        if field == "lastUpdated":
            logger.debug(f"Processing 'lastUpdated' with raw_value='{raw_value}', pattern='{pattern}'")

            def normalize(s):
                # Ordinals ("05th" -> "05"), "p.m."/"a.m." -> "PM"/"AM", microseconds capped at 6 digits.
                s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s.strip())
                s = re.sub(r"\b([ap])\.?m\.?(?![a-z])", lambda m: m.group(1).upper() + "M", s, flags=re.I)
                s = re.sub(r"(\.\d{6})\d+", r"\1", s)
                return re.sub(r"\s+", " ", s)

            # Toronto "today", used to fill in date parts a source doesn't show.
            now_local = datetime.now(TORONTO).replace(tzinfo=None)
            dt = None
            date_was_inferred = False

            # 1) The hospital's configured pattern + format.
            match = re.search(pattern, raw_value) if pattern else None
            if match and format_code:
                candidate = normalize(match.group(1))
                missing_day = "%d" not in format_code
                missing_month = all(x not in format_code for x in ["%m", "%b", "%B"])
                missing_year = all(x not in format_code for x in ["%Y", "%y"])
                base_format = format_code
                if missing_year:
                    # Parse with the year attached; without it strptime assumes 1900 and rejects Feb 29.
                    candidate = f"{candidate} {now_local.year}"
                    base_format = f"{format_code} %Y"
                # Also accept the same format with/without a space before AM/PM.
                formats = [base_format, base_format.replace(" %p", "%p"), base_format.replace("%M%p", "%M %p")]
                for fmt in dict.fromkeys(formats):
                    try:
                        dt = datetime.strptime(candidate, fmt)
                        break
                    except ValueError:
                        continue
                if dt is not None:
                    # One replace() call, so no invalid intermediate date (e.g. Jan 31 -> "Feb 31").
                    fill = {}
                    if missing_day:
                        fill["day"] = now_local.day
                    if missing_month:
                        fill["month"] = now_local.month
                    try:
                        dt = dt.replace(**fill)
                    except ValueError:
                        dt = None  # e.g. "31" with the current month being 30 days; fall back below
                    date_was_inferred = missing_day or missing_month or missing_year

            # 2) Fallback: lenient parse of the whole text, e.g. a site switching from
            #    "November 5, 2026 at 8:30 pm" to "5 November 2026 at 8:30 pm".
            if dt is None:
                try:
                    default = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                    dt = date_parser.parse(normalize(raw_value), fuzzy=True, default=default)
                    date_was_inferred = True
                    logger.warning(
                        f"lastUpdated '{raw_value}' did not match pattern '{pattern}' / format "
                        f"'{format_code}'; parsed leniently as {dt}. Update this target's instructions."
                    )
                except (ValueError, OverflowError):
                    logger.warning(f"Could not parse lastUpdated from '{raw_value}'. Returning None.")
                    return None

            dt = _roll_back_if_future(_localize(dt, unit, format_code), date_was_inferred)
            logger.info(f"Successfully parsed 'lastUpdated' as UTC: '{dt}'")
            return dt

        # ---------------------------------------------------------------------
        # 2) Handle "patientsWaiting" or "patientsInTreatment" (Integer Parsing)
        # ---------------------------------------------------------------------
        elif field in ["patientsWaiting", "patientsInTreatment"]:
            logger.debug(f"Processing numeric field='{field}' with raw_value='{raw_value}'")
            try:
                result = int(raw_value.strip())
                logger.info(f"Successfully converted '{raw_value}' to integer: {result}")
                return result
            except ValueError:
                # e.g. "1,234", "12 patients", "Waiting: 37"
                match = re.search(r"\d[\d,]*", raw_value)
                if match:
                    result = int(match.group(0).replace(",", ""))
                    logger.warning(f"Extracted {result} from non-numeric '{raw_value}' for field='{field}'.")
                    return result
                logger.error(f"Failed to convert raw_value='{raw_value}' to int for field='{field}'.")
                return None

        # ---------------------------------------------------------------------
        # 3) Handle "estimatedWaitTime" (Time Duration Parsing)
        # ---------------------------------------------------------------------
        elif field == "estimatedWaitTime":
            logger.debug(
                f"Processing 'estimatedWaitTime' with raw_value='{raw_value}', pattern='{pattern}', unit='{unit}'"
            )
            result = DataFormatter._parse_wait_time(raw_value, pattern, unit)
            if result is None:
                # The configured pattern only covers the shapes seen when the target was set up
                # (e.g. it requires hours, so "45 mins" fails). Try a tolerant parse before giving up.
                result = parse_duration_minutes(raw_value)
                if result is not None:
                    logger.warning(
                        f"estimatedWaitTime '{raw_value}' did not match pattern '{pattern}' (unit={unit}); "
                        f"fallback parsed {result} minutes. Update this target's instructions."
                    )
            return result

        # ---------------------------------------------------------------------
        # 4) Handle Unrecognized Fields
        # ---------------------------------------------------------------------
        logger.warning(
            f"Field '{field}' not recognized. Returning raw_value='{raw_value}' without modification."
        )
        return raw_value

    @staticmethod
    def _parse_wait_time(raw_value, pattern, unit):
        """The hospital's configured wait-time parsing. Returns minutes, or None if it doesn't fit."""
        if unit in ("minutes", "hours"):
            factor = 1 if unit == "minutes" else 60
            try:
                return round(float(raw_value) * factor)
            except ValueError:
                pass
            # Not a bare number: for hours, the pattern's first group holds the (decimal) hours.
            match = re.match(pattern, raw_value) if pattern else None
            if match and match.group(1):
                return round(float(match.group(1)) * factor)
            logger.debug(f"'{raw_value}' is not a plain number of {unit} and pattern '{pattern}' did not match.")
            return None

        # Otherwise the pattern captures (hours, minutes); either group may be optional.
        match = re.match(pattern, raw_value) if pattern else None
        if not match:
            logger.debug(f"Wait-time pattern '{pattern}' did not match '{raw_value}'.")
            return None
        groups = match.groups()
        hours_str = groups[0] if len(groups) > 0 else None
        minutes_str = groups[1] if len(groups) > 1 else None
        if not hours_str and not minutes_str:
            logger.debug(f"Pattern matched '{raw_value}' but captured no hours or minutes.")
            return None
        total = int(hours_str or 0) * 60 + int(minutes_str or 0)
        logger.info(f"Extracted '{raw_value}' as {total} total minutes.")
        return total

if __name__ == "__main__":
    # Example usage and test cases to demonstrate the formatter's functionality.

    test_cases = [
        {
            "lastUpdated": {
                "pattern": r"^([A-Za-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} (AM|PM))$",
                "format_code": "%B %d, %Y at %I:%M %p",
                "raw_value": "January 27, 2025 at 1:30 AM",
                # Suppose we say the data is EST:
                "unit": "EST"
            },
            "patientsWaiting": {
                "format_code": None,
                "pattern": None,
                "raw_value": "17",
                "unit": None
            },
            "estimatedWaitTime": {
                "format_code": None,
                "pattern": "(\\d+)\\s*hour[s]?\\s*and\\s*(\\d+)\\s*minute[s]?",
                "raw_value": "2 hours and 36 minutes",
                "unit": None
            },
            "patientsInTreatment": {
                "format_code": None,
                "pattern": None,
                "raw_value": None,
                "unit": None
            },
            # The "response" key is just for an example check
            "response": "[datetime.datetime(2025, 1, 16, 23, 15), 17, 161, None]"
        }
    ]

    formatter = DataFormatter()

    for index, test_case in enumerate(test_cases, start=1):
        response = []
        for field, info in test_case.items():
            if field == "response":
                continue

            result = formatter.format_value(
                field=field,
                format_code=info.get("format_code"),
                raw_value=info.get("raw_value"),
                pattern=info.get("pattern"),
                unit=info.get("unit")
            )
            response.append(result)

        # Compare the actual result to the expected 'response' string.
        expected = test_case["response"]
        print(f"Test case {index}: ", end='')
        if str(response) == expected:
            print("PASS")
        else:
            print("FAIL")
            print(f"  Expected: {expected}")
            print(f"  Got:      {response}")
