import re
import json
from bs4 import Tag, BeautifulSoup

from .base_parser import BaseParser
from scraper.utils.logger import get_logger
from scraper.utils.data_formatter import DataFormatter
from scraper.utils.field_mappings import map_field_to_db

logger = get_logger(__name__)


class SelectorError(Exception):
    """Exception raised when a selector fails to find or extract data."""

    def __init__(self, field: str, selector_sequence: list, reason: str, details: dict = None):
        self.field = field
        self.selector_sequence = selector_sequence
        self.reason = reason
        self.details = details or {}
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        msg = f"Selector failed for '{self.field}'\n"
        msg += f"       selectorSequence: {json.dumps(self.selector_sequence)}\n"
        msg += f"       {self.reason}"
        if self.details:
            for key, value in self.details.items():
                msg += f"\n       {key}: {value}"
        return msg


class HTMLParser(BaseParser):
    """
    A parser for HTML content based on instructions provided in
    'scraping_instructions'. This class uses BeautifulSoup to navigate the DOM,
    locate target elements, and extract raw text. It then leverages
    DataFormatter to transform those raw text values into standardized formats
    (e.g., integers, dates).

    Typical usage includes:
      1) Converting raw HTML to a BeautifulSoup object (via parse_from_html).
      2) Iterating through each field in 'scraping_instructions' (e.g. 'lastUpdated').
      3) Finding the relevant DOM element(s) using a sequence of selectors.
      4) Extracting the text, applying patterns/formatting, and returning a final dictionary.
    """

    def parse_from_html(self, html_content: str):
        """
        Converts a raw HTML string into a BeautifulSoup object, then calls
        'parse()' on the resulting soup.

        Args:
            html_content (str): The full HTML content to be parsed.

        Returns:
            dict or None: A dictionary of parsed fields if successful, or None
            if the content is empty or parsing fails.

        Raises:
            SelectorError: If parsing fails with detailed error info.
        """
        logger.debug("Creating BeautifulSoup object from HTML content.")
        soup = BeautifulSoup(html_content, "html.parser")
        return self.parse(soup)

    def parse(self, soup):
        """
        Parses the provided BeautifulSoup object according to the scraping instructions.

        Each entry in self.scraping_instructions may include:
          - 'selectorSequence': A list of selector dictionaries that define how
            to find the target element(s) in the DOM.
          - 'pattern': A regex pattern for refining or parsing the extracted text.
          - 'formatCode': A directive for DataFormatter (e.g., "%B %d, %Y", "int", "ISO8601").
          - 'unit': A unit of measure for time-based fields (e.g. "minutes", "hours").

        Workflow:
          1) Validate the soup is not None.
          2) For each field, attempt to find the DOM element with _find_element().
          3) Extract and strip the text content from that element.
          4) Pass the text to DataFormatter for any needed transformations.
          5) Map the field to a final key in the returned result dictionary.

        Args:
            soup (BeautifulSoup): A parsed BeautifulSoup object containing the DOM.

        Returns:
            dict or None: A dictionary of the extracted and formatted fields,
            keyed by final schema names. Returns None if soup is invalid or
            no fields can be parsed.

        Raises:
            SelectorError: If a selector fails with detailed error info.
        """
        if not soup:
            logger.error("HTMLParser received no BeautifulSoup object.")
            raise SelectorError(
                field="(root)",
                selector_sequence=[],
                reason="No HTML content provided to parse"
            )

        result = {}
        errors = []

        # Traverse the scraping_instructions to extract each field.
        for key, instructions in self.scraping_instructions.items():
            logger.debug(f"Processing field '{key}' with instructions: {instructions}")

            selector_sequence = instructions.get("selectorSequence", [])
            pattern = instructions.get("pattern")  # optional regex
            format_code = instructions.get("formatCode")  # e.g. "int", "%B %d, %Y"
            unit = instructions.get("unit")

            try:
                # 1) Find the element in the DOM based on selector_sequence.
                element = self._find_element(soup, selector_sequence, key)
                if not element:
                    raise SelectorError(
                        field=key,
                        selector_sequence=selector_sequence,
                        reason="No element found matching selector"
                    )

                # 2) Extract raw text from the found element.
                raw_value = element.get_text(strip=True)
                if not raw_value:
                    raise SelectorError(
                        field=key,
                        selector_sequence=selector_sequence,
                        reason="Element found but no text content",
                        details={"element_tag": element.name, "element_classes": element.get("class", [])}
                    )

                logger.debug(f"Extracted raw text for field '{key}': {raw_value}")

                # 3) Use DataFormatter to parse/format the raw text.
                parsed_value = DataFormatter.format_value(
                    field=key,
                    format_code=format_code,
                    raw_value=raw_value,
                    pattern=pattern,
                    unit=unit
                )
                logger.debug(f"Parsed value for field '{key}': {parsed_value}")

                # 4) Map to final schema using centralized mappings
                db_column = map_field_to_db(key)
                result[db_column] = parsed_value

            except SelectorError as e:
                errors.append(e)
                logger.warning(str(e))

        # If all fields failed, raise the first error
        if errors and not result:
            raise errors[0]

        logger.debug(f"HTMLParser result: {result}")
        return result

    def _find_element(self, soup, selector_sequence, field_name):
        """
        Iteratively narrows down DOM elements using the provided 'selector_sequence'.
        Each item in selector_sequence can specify:
          - tag (str): e.g. "div", "span"
          - classRegex (str): A regex pattern to match classes
          - idRegex (str): A regex pattern to match element ids
          - textRegex (str): A regex pattern to match inner text
          - nthOfType (int): Select the nth matching element (1-based index)

        For example:
            selectorSequence = [
                {"tag": "div", "classRegex": "er-status"},
                {"tag": "span", "nthOfType": 2}
            ]

        This means:
          1) Find all <div> elements whose class matches "er-status", take the first.
          2) Within that element, find all <span> elements, take the second one.

        Args:
            soup (Tag or BeautifulSoup): The current DOM context in which to search.
            selector_sequence (list): A list of dictionaries defining how to filter elements.
            field_name (str): The field name being extracted (for error reporting).

        Returns:
            Tag or None: The final matched element if found, otherwise None.

        Raises:
            SelectorError: If the selector sequence fails with detailed info.
        """
        current = soup
        step_index = 0

        for sel in selector_sequence:
            tag = sel.get("tag")
            class_regex = sel.get("classRegex")
            text_regex = sel.get("textRegex")
            nth_of_type = sel.get("nthOfType")
            id_regex = sel.get("idRegex")
            aria_regex = sel.get("ariaRegex")

            # Ensure 'current' is a valid Tag before searching within it.
            if not isinstance(current, Tag):
                raise SelectorError(
                    field=field_name,
                    selector_sequence=selector_sequence,
                    reason=f"Step {step_index}: Expected Tag element, got {type(current).__name__}",
                    details={"failed_at_step": step_index, "selector": sel}
                )

            # If 'tag' is specified, find all child elements matching it.
            # Otherwise, find_all(True) to get all child elements.
            candidates = current.find_all(tag) if tag else current.find_all(True)

            # Filter by class regex
            if class_regex:
                c_pattern = re.compile(class_regex, re.IGNORECASE)
                candidates = [
                    c for c in candidates
                    if any(c_pattern.search(cl) for cl in c.get("class", []))
                ]

            # Filter by id regex
            if id_regex:
                i_pattern = re.compile(id_regex, re.IGNORECASE)
                candidates = [
                    c for c in candidates
                    if c.has_attr("id") and i_pattern.search(c["id"])
                ]

            # Filter by aria-label regex. Power BI labels every visual with its field name
            # (e.g. "Patients Waiting To Be Seen", "CombinedTotalWait 2 hr ..."), which is far
            # more stable than counting <tspan>s, whose number changes when values wrap.
            if aria_regex:
                a_pattern = re.compile(aria_regex, re.IGNORECASE)
                candidates = [
                    c for c in candidates
                    if c.has_attr("aria-label") and a_pattern.search(c["aria-label"])
                ]

            # Filter by text regex
            if text_regex:
                t_pattern = re.compile(text_regex, re.IGNORECASE)
                candidates = [c for c in candidates if t_pattern.search(c.get_text())]

            if not candidates:
                raise SelectorError(
                    field=field_name,
                    selector_sequence=selector_sequence,
                    reason=f"Step {step_index}: No elements found matching criteria",
                    details={
                        "failed_at_step": step_index,
                        "selector": sel,
                        "parent_tag": current.name if hasattr(current, 'name') else str(type(current))
                    }
                )

            # If nthOfType is specified, select that specific occurrence (1-based).
            if nth_of_type:
                if 1 <= nth_of_type <= len(candidates):
                    current = candidates[nth_of_type - 1]
                else:
                    raise SelectorError(
                        field=field_name,
                        selector_sequence=selector_sequence,
                        reason=f"Step {step_index}: nthOfType {nth_of_type} out of range",
                        details={
                            "failed_at_step": step_index,
                            "selector": sel,
                            "available_count": len(candidates)
                        }
                    )
            else:
                # Default to the first candidate if no nthOfType was provided.
                current = candidates[0]

            step_index += 1

        return current
