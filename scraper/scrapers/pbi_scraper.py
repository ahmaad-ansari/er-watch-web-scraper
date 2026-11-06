import asyncio
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .base_scraper import BaseScraper
from scraper.parsers.html_parser import HTMLParser
from scraper.utils.logger import get_logger

logger = get_logger(__name__)


class PBIScraper(BaseScraper):
    """
    A scraper class specifically designed to handle pages rendered by Power BI (PBI).
    It uses Playwright to load dynamic content before parsing the resulting HTML with BeautifulSoup.

    Inherits from:
        BaseScraper (ABC): Provides abstract scrape method signature and
        a process_parsed_data hook for post-processing the scraped data.
    """

    async def scrape(self, use_headers=False):
        """
        Launches a headless Chromium browser using Playwright to load a
        Power BI-rendered page, then extracts and parses HTML content. This
        method helps handle dynamic or JavaScript-heavy pages that standard
        requests-based scraping might fail to parse correctly.

        Args:
            use_headers (bool, optional): If True, sets additional HTTP headers
                                          such as User-Agent or Accept-Language
                                          before navigating to the URL. Defaults to False.

        Returns:
            dict or None:
                - A dictionary with structured fields (e.g., estimated_wait_time,
                  patients_waiting, patients_in_treatment, etc.) if scraping succeeds.
                - None if any error occurs or the parsed data is empty.

        Raises:
            Exception: Propagates exceptions with detailed error info for failure tracking.
        """
        logger.info(f"Starting Power BI scraping for {self.url}")

        browser = None
        playwright = None

        try:
            # 1) Initialize the async Playwright context and launch headless Chromium.
            playwright = await async_playwright().start()
            logger.debug("Launching headless Chromium browser.")
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()

            # Optionally set custom headers (e.g., User-Agent).
            if use_headers:
                logger.debug("Applying additional HTTP headers for scraping.")
                await page.set_extra_http_headers({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1"
                })

            # 2) Navigate to the provided URL.
            logger.debug(f"Navigating to: {self.url}")
            await page.goto(self.url)

            # 3) Wait for the network to become idle, indicating that most
            #    dynamic content should be loaded.
            try:
                logger.debug("Waiting for Power BI visual to load...")
                await page.wait_for_load_state('networkidle', timeout=30_000)
                logger.debug("Power BI visual loaded successfully.")
            except PlaywrightTimeoutError:
                error_msg = "Timeout waiting for network idle (30s)"
                logger.error(error_msg)
                raise Exception(error_msg)

            # 3b) Network idle is not "rendered": Power BI draws its SVG text ~1-2s later, and a
            #     half-drawn report shifts every positional selector. Wait until text exists and
            #     the amount of it has stopped changing.
            await self._wait_for_stable_render(page)

            # 4) Extract the fully rendered HTML content after JavaScript has executed.
            logger.debug("Extracting rendered HTML content from the page.")
            content = await page.content()

        except PlaywrightTimeoutError as e:
            error_msg = f"Playwright timeout: {e}"
            logger.error(f"PBI scrape failed: {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            # Re-raise with clear error message
            if "Timeout" in str(e):
                raise
            error_msg = f"Browser error: {type(e).__name__}: {e}"
            logger.error(f"PBI scrape failed: {error_msg}")
            raise Exception(error_msg)
        finally:
            # 5) Always close browser and playwright to free resources
            if browser:
                try:
                    await browser.close()
                    logger.debug("Browser closed successfully.")
                except Exception as close_err:
                    logger.warning(f"Error closing browser: {close_err}")
            if playwright:
                try:
                    await playwright.stop()
                    logger.debug("Playwright stopped successfully.")
                except Exception as stop_err:
                    logger.warning(f"Error stopping playwright: {stop_err}")

        # 6) Parse the retrieved HTML with BeautifulSoup and the custom HTMLParser.
        try:
            logger.debug("Converting rendered HTML content to a BeautifulSoup object.")
            soup = BeautifulSoup(content, "html.parser")

            logger.debug("Parsing the HTML content with the custom HTMLParser.")
            parser = HTMLParser(self.scraping_instructions)
            parsed_data = parser.parse(soup)

        except Exception as e:
            error_msg = f"HTML parsing failed: {type(e).__name__}: {e}"
            logger.error(error_msg)
            raise Exception(error_msg)

        return self.process_parsed_data(parsed_data)

    # How long the rendered text must stay unchanged before the report counts as drawn.
    STABLE_FOR_MS = 1500
    POLL_MS = 250
    RENDER_TIMEOUT_MS = 20_000

    async def _wait_for_stable_render(self, page):
        """
        Wait until the page has rendered text and that text has stopped changing.
        Power BI reports are judged by their SVG text; other pages scraped through this
        browser path (e.g. 'pbi_h' targets that aren't Power BI) by their body text.
        """
        signature_js = (
            "() => { const svg = Array.from(document.querySelectorAll('svg text'))"
            ".map(t => t.textContent).join('\\u0001');"
            " return svg || (document.body ? document.body.innerText : ''); }"
        )
        last, stable_ms, waited = None, 0, 0
        while waited < self.RENDER_TIMEOUT_MS:
            signature = await page.evaluate(signature_js)
            if signature and signature == last:
                stable_ms += self.POLL_MS
                if stable_ms >= self.STABLE_FOR_MS:
                    logger.debug(f"Power BI render stable after {waited}ms")
                    return
            else:
                stable_ms = 0
            last = signature
            await page.wait_for_timeout(self.POLL_MS)
            waited += self.POLL_MS
        if not last:
            raise Exception(f"Power BI report rendered no text within {self.RENDER_TIMEOUT_MS // 1000}s")
        logger.warning("Power BI text was still changing after the render timeout; parsing what is there")
