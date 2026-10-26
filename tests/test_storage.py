import pytest

from realty_data_engine.archive import BlobPdfArchive
from realty_data_engine import cli, scraper
from realty_data_engine.config import load_settings
from realty_data_engine.geocoder import ParcelLocation
from realty_data_engine.parser import parse_pdf
from realty_data_engine.storage import SCHEMA_STATEMENTS, AzureSqlStore, build_connection_string


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None

    def execute(self, sql, *params):
        normalized_sql = " ".join(sql.split())
        self.db.executed.append((normalized_sql, params))
        self._result = None
        self._rows = []
        if normalized_sql.startswith("SELECT DISTINCT permits.parcel_number"):
            missing = self.db.parcel_numbers - self.db.parcel_locations.keys()
            self._rows = [(parcel_number,) for parcel_number in sorted(missing)]
        elif normalized_sql.startswith("SELECT DISTINCT parcel_number FROM dbo.building_permits"):
            self._rows = [(parcel_number,) for parcel_number in sorted(self.db.parcel_numbers)]
        elif normalized_sql.startswith("MERGE dbo.building_parcel_locations"):
            for offset in range(0, len(params), 6):
                parcel_number, latitude, longitude, address, status, checked_at = params[offset:offset + 6]
                self.db.parcel_locations[parcel_number] = (latitude, longitude, address, status, checked_at)
        elif normalized_sql.startswith("SELECT id, content_sha256"):
            self._result = self.db.reports.get(params[0])
        elif normalized_sql.startswith("SELECT id, issue_date FROM dbo.building_permits"):
            permit = next((row for row in self.db.permits if row[3] == params[0]), None)
            self._result = (permit[0], permit[4]) if permit else None
        elif normalized_sql.startswith("INSERT INTO dbo.building_activity_reports"):
            report_id = len(self.db.reports) + 1
            self.db.reports[params[0]] = (report_id, params[4])
            self._result = (report_id,)
        elif normalized_sql.startswith("INSERT INTO dbo.building_permits"):
            permit_id = self.db.next_permit_id
            self.db.next_permit_id += 1
            self.db.permits.append((permit_id, *params))
            self._result = (permit_id,)
        elif normalized_sql.startswith("UPDATE dbo.building_permits"):
            permit_id = params[-1]
            self.db.permits = [
                (permit_id, *params[:-1]) if row[0] == permit_id else row
                for row in self.db.permits
            ]
        elif normalized_sql.startswith("DELETE FROM dbo.building_permit_reports"):
            report_id = params[0]
            self.db.permit_links = {link for link in self.db.permit_links if link[0] != report_id}
        elif normalized_sql.startswith("IF NOT EXISTS") and "building_permit_reports" in normalized_sql:
            self.db.permit_links.add((params[0], params[1]))
        elif normalized_sql.startswith("DELETE FROM dbo.building_permits WHERE NOT EXISTS"):
            linked_permits = {permit_id for _, permit_id in self.db.permit_links}
            self.db.permits = [row for row in self.db.permits if row[0] in linked_permits]
        elif normalized_sql.startswith("UPDATE dbo.building_activity_reports"):
            for url, (report_id, _) in self.db.reports.items():
                if report_id == params[-1]:
                    content_sha256 = params[3] if "content_sha256" in normalized_sql else self.db.reports[url][1]
                    self.db.reports[url] = (report_id, content_sha256)

    def executemany(self, sql, rows):
        self.db.permits.extend(rows)

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self):
        self.reports = {}
        self.permits = []
        self.permit_links = set()
        self.parcel_numbers = set()
        self.parcel_locations = {}
        self.next_permit_id = 1
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_build_connection_string_from_parts():
    conn_str = build_connection_string({
        "AZURE_SQL_SERVER": "myserver.database.windows.net",
        "AZURE_SQL_DATABASE": "realty",
        "AZURE_SQL_USERNAME": "app",
        "AZURE_SQL_PASSWORD": "p;w{d" + "}",
    })
    parts = conn_str.split(";")
    assert parts[:4] == [
        "Driver={ODBC Driver 18 for SQL Server}",
        "Server=tcp:myserver.database.windows.net,1433",
        "Database=realty",
        "Uid=app",
    ]
    # Values containing ODBC special characters are wrapped in braces with "}" escaped as "}}".
    assert "=" + "{p;w{d}}}" + ";Encrypt=yes;" in conn_str
    assert conn_str.endswith("TrustServerCertificate=no;Connection Timeout=120;")


def test_build_connection_string_allows_connection_timeout_override():
    conn_str = build_connection_string({
        "AZURE_SQL_SERVER": "myserver.database.windows.net",
        "AZURE_SQL_DATABASE": "realty",
        "AZURE_SQL_CONNECTION_TIMEOUT": "45",
    })
    assert conn_str.endswith("TrustServerCertificate=no;Connection Timeout=45;")


def test_build_connection_string_prefers_full_string():
    assert build_connection_string({"AZURE_SQL_CONNECTION_STRING": "Driver=x;", "AZURE_SQL_SERVER": "s"}) == "Driver=x;"


def test_build_connection_string_requires_settings():
    with pytest.raises(ValueError):
        build_connection_string({})


def test_load_settings_reads_local_json_and_environment_overrides(tmp_path):
    config_path = tmp_path / "azure-creds.json"
    config_path.write_text(
        '{"azure_sql":{"server":"from-file","database":"permit_data","password":"",'
        '"connection_timeout":"120"},'
        '"azure_storage":{"account_url":"https://storage.example","container":"permits"}}',
        encoding="utf-8",
    )

    settings = load_settings({"AZURE_SQL_SERVER": "from-env"}, config_path)

    assert settings["AZURE_SQL_SERVER"] == "from-env"
    assert settings["AZURE_SQL_DATABASE"] == "permit_data"
    assert settings["AZURE_SQL_CONNECTION_TIMEOUT"] == "120"
    assert settings["AZURE_STORAGE_ACCOUNT_URL"] == "https://storage.example"
    assert settings["AZURE_STORAGE_CONTAINER"] == "permits"


def test_blob_archive_uses_content_addressed_pdf_path():
    class FakeBlob:
        url = "https://storage.example/permit-reports/pdfs/monthly/ab/abc.pdf"

        def upload_blob(self, content, **kwargs):
            self.content = content
            self.options = kwargs

    class FakeContainer:
        def __init__(self):
            self.blob = FakeBlob()
            self.name = None

        def get_blob_client(self, name):
            self.name = name
            return self.blob

    container = FakeContainer()
    archive = BlobPdfArchive(container)

    uri = archive.archive_pdf(b"%PDF-data", "monthly", "a" * 64)

    assert container.name == f"pdfs/monthly/aa/{'a' * 64}.pdf"
    assert container.blob.content == b"%PDF-data"
    assert container.blob.options["overwrite"] is True
    assert container.blob.options["content_settings"].content_type == "application/pdf"
    assert uri == container.blob.url


def test_blob_archive_requires_account_url():
    with pytest.raises(ValueError, match="AZURE_STORAGE_ACCOUNT_URL"):
        BlobPdfArchive.from_environment({})


def test_ensure_schema_runs_all_statements():
    conn = FakeConnection()
    AzureSqlStore(conn).ensure_schema()
    assert len(conn.executed) == len(SCHEMA_STATEMENTS)
    assert conn.commits == 1


def test_parcel_location_cache_finds_missing_ids_and_saves_batches():
    conn = FakeConnection()
    conn.parcel_numbers = {"01-08559-004", "01-02258-000-00134-20-13"}
    store = AzureSqlStore(conn)

    missing = store.get_unlocated_parcel_numbers()
    store.save_parcel_locations({
        parcel_number: ParcelLocation(36.34, -94.19, "2700 SE S ST", "located")
        for parcel_number in missing
    }, batch_size=10)

    assert store.get_unlocated_parcel_numbers() == []
    assert len(conn.parcel_locations) == 2
    assert conn.parcel_locations["01-08559-004"][3] == "located"


def test_save_report_inserts_then_skips_unchanged_then_replaces(permit_pdf):
    conn = FakeConnection()
    store = AzureSqlStore(conn)
    records = parse_pdf(permit_pdf)

    assert store.save_report("https://x/a.pdf", "Jan", "a" * 64, records) is True
    assert len(conn.permits) == 2
    assert conn.permits[0][0] == 1 and conn.permits[0][3] == "BP-2024-0001"
    assert len(conn.permits[0]) == 21
    assert conn.permits[0][15] is None
    assert conn.permits[0][20] == "{}"
    assert len(conn.permit_links) == 2

    assert store.save_report("https://x/a.pdf", "Jan", "a" * 64, records) is False
    assert len(conn.permits) == 2

    assert store.save_report("https://x/a.pdf", "Jan", "b" * 64, records[:1]) is True
    assert len(conn.permits) == 1
    assert len(conn.permit_links) == 1
    assert conn.reports["https://x/a.pdf"] == (1, "b" * 64)


def test_run_skips_existing_urls_and_force_reprocesses(monkeypatch, permit_pdf):
    links = [scraper.ReportLink("https://x/new.pdf", "New"), scraper.ReportLink("https://x/old.pdf", "Old")]
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: links)
    downloaded = []

    def fake_download(url, session=None):
        downloaded.append(url)
        return permit_pdf

    monkeypatch.setattr(scraper, "download_pdf", fake_download)
    conn = FakeConnection()
    conn.reports["https://x/old.pdf"] = (99, "z" * 64)

    store = AzureSqlStore(conn)
    summary = cli.run("https://page", store)

    assert summary == {"found": 2, "saved": 1, "skipped": 1, "failed": 0, "reports": []}
    assert downloaded == ["https://x/new.pdf"]
    assert len(conn.permits) == 2

    downloaded.clear()
    summary = cli.run("https://page", store, force=True)

    assert summary == {"found": 2, "saved": 2, "skipped": 0, "failed": 0, "reports": []}
    assert downloaded == ["https://x/new.pdf", "https://x/old.pdf"]
    assert len(conn.permits) == 2
    assert len(conn.permit_links) == 4


def test_same_permit_number_in_two_reports_reuses_permit_id(permit_pdf):
    connection = FakeConnection()
    store = AzureSqlStore(connection)
    records = parse_pdf(permit_pdf)

    store.save_report("https://x/monthly.pdf", "Monthly", "a" * 64, records, category="monthly")
    first_permit_id = next(row[0] for row in connection.permits if row[3] == "BP-2024-0001")
    store.save_report("https://x/biweekly.pdf", "Biweekly", "b" * 64, records, category="biweekly")

    assert len(connection.permits) == 2
    assert len(connection.permit_links) == 4
    assert next(row[0] for row in connection.permits if row[3] == "BP-2024-0001") == first_permit_id


def test_run_filters_by_category_and_archives_pdf(monkeypatch, permit_pdf):
    links = [
        scraper.ReportLink("https://x/month.pdf", "September 2026", "monthly"),
        scraper.ReportLink("https://x/biweekly.pdf", "October 1 to October 15", "biweekly"),
    ]
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: links)
    downloaded = []

    def fake_download(url, session=None):
        downloaded.append(url)
        return permit_pdf

    monkeypatch.setattr(scraper, "download_pdf", fake_download)

    class FakeArchive:
        def __init__(self):
            self.calls = []

        def archive_pdf(self, content, category, content_sha256):
            self.calls.append((category, content_sha256))
            return f"https://storage.example/{category}/{content_sha256}.pdf"

    archive = FakeArchive()
    summary = cli.run("https://page", store=None, category="monthly", archive=archive)

    report = summary["reports"][0]
    assert summary["found"] == 1
    assert downloaded == ["https://x/month.pdf"]
    assert report["category"] == "monthly"
    assert report["blob_uri"].startswith("https://storage.example/monthly/")
    assert archive.calls[0][0] == "monthly"


def test_run_filters_yearly_reports_to_requested_range(monkeypatch, permit_pdf):
    links = [
        scraper.ReportLink("https://x/2016.pdf", "January 2016 to December 2016", "yearly"),
        scraper.ReportLink("https://x/2017.pdf", "January 2017 to December 2017", "yearly"),
        scraper.ReportLink("https://x/2025.pdf", "January 2025 to December 2025", "yearly"),
        scraper.ReportLink("https://x/2026.pdf", "January 2026 to December 2026", "yearly"),
    ]
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: links)
    downloaded = []

    def fake_download(url, session=None):
        downloaded.append(url)
        return permit_pdf

    monkeypatch.setattr(scraper, "download_pdf", fake_download)

    summary = cli.run(
        "https://page",
        store=None,
        category="yearly",
        year_from=2017,
        year_to=2025,
    )

    assert summary["found"] == 2
    assert downloaded == ["https://x/2017.pdf", "https://x/2025.pdf"]
    assert [report["title"] for report in summary["reports"]] == [
        "January 2017 to December 2017",
        "January 2025 to December 2025",
    ]


def test_run_dry_run_returns_parsed_permits(monkeypatch, permit_pdf):
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: [scraper.ReportLink("u", "T")])
    monkeypatch.setattr(scraper, "download_pdf", lambda url, session=None: permit_pdf)

    summary = cli.run("https://page", store=None)

    assert summary["reports"][0]["permits"][0]["issue_date"] == "2024-01-05"
    assert summary["reports"][0]["permits"][0]["valuation"] == "350000.00"
