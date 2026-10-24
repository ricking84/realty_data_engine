"""Persists building activity reports and their permits to Azure SQL (SQL Server)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from .parser import PermitRecord

logger = logging.getLogger(__name__)

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

SCHEMA_STATEMENTS = (
    """
    IF OBJECT_ID(N'dbo.building_activity_reports', N'U') IS NULL
    CREATE TABLE dbo.building_activity_reports (
        id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        source_url NVARCHAR(450) NOT NULL,
        title NVARCHAR(500) NULL,
        content_sha256 CHAR(64) NOT NULL,
        permit_count INT NOT NULL DEFAULT 0,
        ingested_at DATETIME2 NOT NULL,
        CONSTRAINT UQ_building_activity_reports_source_url UNIQUE (source_url)
    )
    """,
    """
    IF OBJECT_ID(N'dbo.building_permits', N'U') IS NULL
    CREATE TABLE dbo.building_permits (
        id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        report_id INT NOT NULL
            CONSTRAINT FK_building_permits_report REFERENCES dbo.building_activity_reports(id) ON DELETE CASCADE,
        page_number INT NOT NULL,
        line_number INT NOT NULL,
        permit_number NVARCHAR(100) NULL,
        issue_date DATE NULL,
        address NVARCHAR(500) NULL,
        permit_type NVARCHAR(255) NULL,
        description NVARCHAR(MAX) NULL,
        contractor NVARCHAR(500) NULL,
        owner NVARCHAR(500) NULL,
        subdivision NVARCHAR(500) NULL,
        valuation DECIMAL(18, 2) NULL,
        square_feet INT NULL,
        raw_data NVARCHAR(MAX) NOT NULL
    )
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_building_permits_permit_number')
    CREATE INDEX IX_building_permits_permit_number ON dbo.building_permits (permit_number)
    """,
)

_INSERT_PERMIT_SQL = """
INSERT INTO dbo.building_permits (
    report_id, page_number, line_number, permit_number, issue_date, address, permit_type,
    description, contractor, owner, subdivision, valuation, square_feet, raw_data
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _quote_odbc_value(value: str) -> str:
    if any(char in value for char in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_connection_string(env: Mapping[str, str] | None = None) -> str:
    """Build an ODBC connection string for Azure SQL from environment variables.

    ``AZURE_SQL_CONNECTION_STRING`` is used verbatim when set. Otherwise the string is built
    from ``AZURE_SQL_SERVER``, ``AZURE_SQL_DATABASE`` and either ``AZURE_SQL_USERNAME`` /
    ``AZURE_SQL_PASSWORD`` or ``AZURE_SQL_AUTHENTICATION`` (e.g. ``ActiveDirectoryMsi``).
    """
    env = os.environ if env is None else env
    if env.get("AZURE_SQL_CONNECTION_STRING"):
        return env["AZURE_SQL_CONNECTION_STRING"]

    server = env.get("AZURE_SQL_SERVER")
    database = env.get("AZURE_SQL_DATABASE")
    if not server or not database:
        raise ValueError(
            "Set AZURE_SQL_CONNECTION_STRING, or AZURE_SQL_SERVER and AZURE_SQL_DATABASE, to connect to Azure SQL"
        )
    if "," not in server:
        server = f"{server},1433"
    if not server.lower().startswith("tcp:"):
        server = f"tcp:{server}"

    parts = {
        "Driver": "{" + env.get("AZURE_SQL_DRIVER", DEFAULT_DRIVER) + "}",
        "Server": _quote_odbc_value(server),
        "Database": _quote_odbc_value(database),
    }
    if env.get("AZURE_SQL_AUTHENTICATION"):
        parts["Authentication"] = _quote_odbc_value(env["AZURE_SQL_AUTHENTICATION"])
    if env.get("AZURE_SQL_USERNAME"):
        parts["Uid"] = _quote_odbc_value(env["AZURE_SQL_USERNAME"])
        parts["Pwd"] = _quote_odbc_value(env.get("AZURE_SQL_PASSWORD", ""))
    parts.update({"Encrypt": "yes", "TrustServerCertificate": "no", "Connection Timeout": "30"})
    return ";".join(f"{key}={value}" for key, value in parts.items()) + ";"


def connect(connection_string: str | None = None):
    import pyodbc  # Imported lazily so the scraper/parser work without the ODBC driver installed.

    return pyodbc.connect(connection_string or build_connection_string())


class AzureSqlStore:
    """Writes reports and permits through a DB-API connection (pyodbc for Azure SQL)."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        cursor = self.connection.cursor()
        for statement in SCHEMA_STATEMENTS:
            cursor.execute(statement)
        self.connection.commit()

    def get_report(self, source_url: str) -> tuple[int, str] | None:
        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT id, content_sha256 FROM dbo.building_activity_reports WHERE source_url = ?", source_url
        )
        row = cursor.fetchone()
        return (int(row[0]), str(row[1])) if row else None

    def save_report(self, source_url: str, title: str, content_sha256: str, records: Sequence[PermitRecord]) -> bool:
        """Insert or replace a report and its permits. Returns False if the stored copy is unchanged."""
        existing = self.get_report(source_url)
        if existing and existing[1] == content_sha256:
            logger.info("Report %s unchanged; skipping", source_url)
            return False

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cursor = self.connection.cursor()
        try:
            if existing:
                report_id = existing[0]
                cursor.execute("DELETE FROM dbo.building_permits WHERE report_id = ?", report_id)
                cursor.execute(
                    "UPDATE dbo.building_activity_reports SET title = ?, content_sha256 = ?, permit_count = ?, "
                    "ingested_at = ? WHERE id = ?",
                    title, content_sha256, len(records), now, report_id,
                )
            else:
                cursor.execute(
                    "INSERT INTO dbo.building_activity_reports (source_url, title, content_sha256, permit_count, "
                    "ingested_at) OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?)",
                    source_url, title, content_sha256, len(records), now,
                )
                report_id = int(cursor.fetchone()[0])

            if records:
                if hasattr(cursor, "fast_executemany"):
                    cursor.fast_executemany = True
                cursor.executemany(
                    _INSERT_PERMIT_SQL,
                    [
                        (
                            report_id, r.page_number, r.row_number, r.permit_number, r.issue_date, r.address,
                            r.permit_type, r.description, r.contractor, r.owner, r.subdivision, r.valuation,
                            r.square_feet, r.raw_json,
                        )
                        for r in records
                    ],
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return True
