import pytest

from realty_data_engine import cli, scraper
from realty_data_engine.parser import parse_pdf
from realty_data_engine.storage import SCHEMA_STATEMENTS, AzureSqlStore, build_connection_string


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None

    def execute(self, sql, *params):
        self.db.executed.append((" ".join(sql.split()), params))
        if sql.startswith("SELECT id, content_sha256"):
            self._result = self.db.reports.get(params[0])
        elif sql.startswith("INSERT INTO dbo.building_activity_reports"):
            report_id = len(self.db.reports) + 1
            self.db.reports[params[0]] = (report_id, params[2])
            self._result = (report_id,)
        elif sql.startswith("DELETE FROM dbo.building_permits"):
            self.db.permits = [p for p in self.db.permits if p[0] != params[0]]
        elif sql.startswith("UPDATE dbo.building_activity_reports"):
            for url, (report_id, _) in self.db.reports.items():
                if report_id == params[-1]:
                    self.db.reports[url] = (report_id, params[1])

    def executemany(self, sql, rows):
        self.db.permits.extend(rows)

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self):
        self.reports = {}
        self.permits = []
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
    assert conn_str.endswith("TrustServerCertificate=no;Connection Timeout=30;")


def test_build_connection_string_prefers_full_string():
    assert build_connection_string({"AZURE_SQL_CONNECTION_STRING": "Driver=x;", "AZURE_SQL_SERVER": "s"}) == "Driver=x;"


def test_build_connection_string_requires_settings():
    with pytest.raises(ValueError):
        build_connection_string({})


def test_ensure_schema_runs_all_statements():
    conn = FakeConnection()
    AzureSqlStore(conn).ensure_schema()
    assert len(conn.executed) == len(SCHEMA_STATEMENTS)
    assert conn.commits == 1


def test_save_report_inserts_then_skips_unchanged_then_replaces(permit_pdf):
    conn = FakeConnection()
    store = AzureSqlStore(conn)
    records = parse_pdf(permit_pdf)

    assert store.save_report("https://x/a.pdf", "Jan", "a" * 64, records) is True
    assert len(conn.permits) == 2
    assert conn.permits[0][0] == 1 and conn.permits[0][3] == "BP-2024-0001"

    assert store.save_report("https://x/a.pdf", "Jan", "a" * 64, records) is False
    assert len(conn.permits) == 2

    assert store.save_report("https://x/a.pdf", "Jan", "b" * 64, records[:1]) is True
    assert len(conn.permits) == 1
    assert conn.reports["https://x/a.pdf"] == (1, "b" * 64)


def test_run_saves_new_reports_and_skips_existing(monkeypatch, permit_pdf):
    links = [scraper.ReportLink("https://x/new.pdf", "New"), scraper.ReportLink("https://x/old.pdf", "Old")]
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: links)
    downloaded = []

    def fake_download(url, session=None):
        downloaded.append(url)
        return permit_pdf

    monkeypatch.setattr(scraper, "download_pdf", fake_download)
    conn = FakeConnection()
    conn.reports["https://x/old.pdf"] = (99, "z" * 64)

    summary = cli.run("https://page", AzureSqlStore(conn))

    assert summary == {"found": 2, "saved": 1, "skipped": 1, "failed": 0, "reports": []}
    assert downloaded == ["https://x/new.pdf"]
    assert len(conn.permits) == 2


def test_run_dry_run_returns_parsed_permits(monkeypatch, permit_pdf):
    monkeypatch.setattr(scraper, "find_report_links", lambda url, session=None: [scraper.ReportLink("u", "T")])
    monkeypatch.setattr(scraper, "download_pdf", lambda url, session=None: permit_pdf)

    summary = cli.run("https://page", store=None)

    assert summary["reports"][0]["permits"][0]["issue_date"] == "2024-01-05"
    assert summary["reports"][0]["permits"][0]["valuation"] == "350000.00"
