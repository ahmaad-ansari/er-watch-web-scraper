"""
Shared aiohttp session manager with connection pooling for async HTTP requests.
"""
import ssl
from typing import Optional

import aiohttp
import certifi
from scraper.utils.logger import get_logger

logger = get_logger(__name__)

# Global session instance
_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    """
    Gets or creates a shared aiohttp ClientSession with connection pooling.

    Returns:
        The shared aiohttp ClientSession instance
    """
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            # Use certifi's up-to-date CA bundle rather than the OS store: some hospital sites
            # chain to newer roots (e.g. Halton Healthcare) that older system stores lack.
            ssl=ssl.create_default_context(cafile=certifi.where()),
            limit=100,  # Max total connections
            limit_per_host=10,  # Max connections per host
            ttl_dns_cache=300,  # DNS cache TTL in seconds
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            total=30,  # Total timeout for the request
            connect=10,  # Connection timeout
            sock_read=10,  # Socket read timeout
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            },
        )
        logger.debug("Created new aiohttp ClientSession with connection pooling")
    return _session


async def close_session() -> None:
    """
    Closes the shared aiohttp ClientSession.
    Should be called at the end of the scraping run.
    """
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        logger.debug("Closed aiohttp ClientSession")
    _session = None


async def fetch_url(url: str, timeout: Optional[int] = None) -> aiohttp.ClientResponse:
    """
    Fetches a URL using the shared session.

    Args:
        url: The URL to fetch
        timeout: Optional custom timeout in seconds

    Returns:
        The aiohttp ClientResponse

    Raises:
        aiohttp.ClientError: If the request fails
    """
    session = await get_session()
    request_timeout = None
    if timeout:
        request_timeout = aiohttp.ClientTimeout(total=timeout)

    response = await session.get(url, timeout=request_timeout)
    response.raise_for_status()
    return response
