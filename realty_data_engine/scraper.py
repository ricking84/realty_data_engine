"""Finds and downloads the building activity report PDFs from the City of Bentonville website."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

REPORTS_PAGE_URL = "https://www.bentonville.ar.gov/152/Building-Activity-Reports"
USER_AGENT = "Mozilla/5.0 (compatible; realty-data-engine/0.1)"
DEFAULT_TIMEOUT = 60

# CivicPlus sites expose documents either via the Document Center, the Archive Center
# (Archive.aspx?ADID=...) or as direct links to .pdf files.
_PDF_HREF_RE = re.compile(r"(/DocumentCenter/View/\d+|Archive\.aspx\?ADID=\d+|\.pdf(\?|#|$))", re.IGNORECASE)
_ARCHIVE_LIST_RE = re.compile(r"Archive\.aspx\?AMID=\d+", re.IGNORECASE)


@dataclass(frozen=True)
class ReportLink:
    url: str
    title: str


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _same_site(url: str, base_url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower()


def extract_pdf_links(html: str, base_url: str) -> tuple[list[ReportLink], list[str]]:
    """Return (pdf report links, archive listing page urls) found in ``html``."""
    soup = BeautifulSoup(html, "html.parser")
    reports: dict[str, ReportLink] = {}
    archive_pages: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        if _PDF_HREF_RE.search(href):
            title = " ".join(anchor.get_text(" ", strip=True).split()) or url.rsplit("/", 1)[-1]
            reports.setdefault(url, ReportLink(url=url, title=title))
        elif _ARCHIVE_LIST_RE.search(href) and _same_site(url, base_url) and url not in archive_pages:
            archive_pages.append(url)
    return list(reports.values()), archive_pages


def find_report_links(
    page_url: str = REPORTS_PAGE_URL,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[ReportLink]:
    """Fetch the reports page (and any linked Archive Center listings) and return all PDF links."""
    session = session or _new_session()
    response = session.get(page_url, timeout=timeout)
    response.raise_for_status()
    links, archive_pages = extract_pdf_links(response.text, page_url)

    found = {link.url: link for link in links}
    for archive_url in archive_pages:
        logger.info("Following archive listing %s", archive_url)
        archive_response = session.get(archive_url, timeout=timeout)
        archive_response.raise_for_status()
        archive_links, _ = extract_pdf_links(archive_response.text, archive_url)
        for link in archive_links:
            found.setdefault(link.url, link)
    return list(found.values())


def download_pdf(url: str, session: requests.Session | None = None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Download ``url`` and return its bytes, raising ``ValueError`` if it is not a PDF."""
    session = session or _new_session()
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    content = response.content
    if not content.lstrip().startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF document")
    return content
