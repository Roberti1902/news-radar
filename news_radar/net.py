"""Gemeinsamer HTTP-Client: Rate-Limit pro Host, Retry mit Backoff, Timeout."""
import logging
import ssl
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

log = logging.getLogger("news_radar")
RETRY_STATUS = {429, 500, 502, 503, 504}


class Http:
    def __init__(self, host_intervals: dict[str, float], timeout: float = 20, retries: int = 3,
                 user_agent: str = "news-radar/0.1"):
        # OS-Zertifikatsspeicher statt certifi: funktioniert auch hinter TLS-inspizierenden Proxys/AV.
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, verify=ssl.create_default_context(),
                                   headers={"User-Agent": user_agent})
        self.intervals = host_intervals
        self.retries = retries
        self._last: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        wait = self.intervals.get(host, self.intervals.get("default", 0.5)) - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        host = urlparse(url).hostname or ""
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                r = self.client.request(method, url, **kw)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                time.sleep(min(_retry_after(r) or 2 ** attempt * 2, 60))
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None
