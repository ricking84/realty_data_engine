import pytest

from realty_data_engine import scraper

PAGE_HTML = """
<html><body>
  <a href="/DocumentCenter/View/1234/January-2024-Building-Activity-Report-PDF">January 2024</a>
  <a href="/DocumentCenter/View/1234/January-2024-Building-Activity-Report-PDF">duplicate</a>
  <a href="https://www.bentonville.ar.gov/Archive.aspx?ADID=55">  February
      2024 </a>
  <a href="files/march.pdf"></a>
  <a href="/Archive.aspx?AMID=40">Older reports</a>
  <a href="https://other.example.com/Archive.aspx?AMID=1">External archive</a>
  <a href="/152/Building-Activity-Reports">Self</a>
  <a href="mailto:permits@bentonvillear.com">Email</a>
</body></html>
"""

ARCHIVE_HTML = """
<a href="Archive.aspx?ADID=10">December 2023</a>
<a href="Archive.aspx?ADID=55">February 2024</a>
"""


class FakeResponse:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        return self.pages[url]


def test_extract_pdf_links():
    links, archives = scraper.extract_pdf_links(PAGE_HTML, scraper.REPORTS_PAGE_URL)

    assert [(link.url, link.title) for link in links] == [
        ("https://www.bentonville.ar.gov/DocumentCenter/View/1234/January-2024-Building-Activity-Report-PDF",
         "January 2024"),
        ("https://www.bentonville.ar.gov/Archive.aspx?ADID=55", "February 2024"),
        ("https://www.bentonville.ar.gov/152/files/march.pdf", "march.pdf"),
    ]
    assert archives == ["https://www.bentonville.ar.gov/Archive.aspx?AMID=40"]


def test_find_report_links_follows_archive_listing():
    session = FakeSession({
        scraper.REPORTS_PAGE_URL: FakeResponse(PAGE_HTML),
        "https://www.bentonville.ar.gov/Archive.aspx?AMID=40": FakeResponse(ARCHIVE_HTML),
    })

    urls = [link.url for link in scraper.find_report_links(session=session)]

    assert len(urls) == 4
    assert "https://www.bentonville.ar.gov/Archive.aspx?ADID=10" in urls


def test_download_pdf_rejects_non_pdf():
    session = FakeSession({"u": FakeResponse(content=b"<html>not found</html>")})
    with pytest.raises(ValueError):
        scraper.download_pdf("u", session=session)


def test_download_pdf_returns_bytes():
    session = FakeSession({"u": FakeResponse(content=b"%PDF-1.4 data")})
    assert scraper.download_pdf("u", session=session) == b"%PDF-1.4 data"
