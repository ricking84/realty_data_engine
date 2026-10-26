"""Persists building activity reports and their permits to Azure SQL (SQL Server)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from .config import load_settings
from .parser import PermitRecord

logger = logging.getLogger(__name__)

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_CONNECTION_TIMEOUT = "120"

SCHEMA_STATEMENTS = (
    """
    IF OBJECT_ID(N'dbo.building_activity_reports', N'U') IS NULL
    CREATE TABLE dbo.building_activity_reports (
        id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        source_url NVARCHAR(450) NOT NULL,
        title NVARCHAR(500) NULL,
        category NVARCHAR(20) NOT NULL CONSTRAINT DF_building_activity_reports_category DEFAULT N'other',
        blob_uri NVARCHAR(2048) NULL,
        content_sha256 CHAR(64) NOT NULL,
        permit_count INT NOT NULL DEFAULT 0,
        ingested_at DATETIME2 NOT NULL,
        CONSTRAINT UQ_building_activity_reports_source_url UNIQUE (source_url)
    )
    """,
    """
    IF COL_LENGTH(N'dbo.building_activity_reports', N'category') IS NULL
        ALTER TABLE dbo.building_activity_reports ADD category NVARCHAR(20) NOT NULL
            CONSTRAINT DF_building_activity_reports_category DEFAULT N'other';
    IF COL_LENGTH(N'dbo.building_activity_reports', N'blob_uri') IS NULL
        ALTER TABLE dbo.building_activity_reports ADD blob_uri NVARCHAR(2048) NULL;
    """,
    """
    IF OBJECT_ID(N'dbo.building_permits', N'U') IS NULL
    CREATE TABLE dbo.building_permits (
        id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
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
        raw_data NVARCHAR(MAX) NOT NULL,
        applied_date DATE NULL,
        permit_subtype NVARCHAR(255) NULL,
        parcel_number NVARCHAR(100) NULL,
        status NVARCHAR(100) NULL,
        total_fees DECIMAL(18, 2) NULL,
        total_paid DECIMAL(18, 2) NULL,
        contacts_json NVARCHAR(MAX) NOT NULL DEFAULT N'{}'
    )
    """,
    """
    IF COL_LENGTH(N'dbo.building_permits', N'applied_date') IS NULL
        ALTER TABLE dbo.building_permits ADD applied_date DATE NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'permit_subtype') IS NULL
        ALTER TABLE dbo.building_permits ADD permit_subtype NVARCHAR(255) NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'parcel_number') IS NULL
        ALTER TABLE dbo.building_permits ADD parcel_number NVARCHAR(100) NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'status') IS NULL
        ALTER TABLE dbo.building_permits ADD status NVARCHAR(100) NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'total_fees') IS NULL
        ALTER TABLE dbo.building_permits ADD total_fees DECIMAL(18, 2) NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'total_paid') IS NULL
        ALTER TABLE dbo.building_permits ADD total_paid DECIMAL(18, 2) NULL;
    IF COL_LENGTH(N'dbo.building_permits', N'contacts_json') IS NULL
        ALTER TABLE dbo.building_permits ADD contacts_json NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_building_permits_contacts_json DEFAULT N'{}';
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_building_permits_permit_number')
    CREATE INDEX IX_building_permits_permit_number ON dbo.building_permits (permit_number)
    """,
    """
    IF OBJECT_ID(N'dbo.building_permit_reports', N'U') IS NULL
    CREATE TABLE dbo.building_permit_reports (
        report_id INT NOT NULL,
        permit_id INT NOT NULL,
        CONSTRAINT PK_building_permit_reports PRIMARY KEY (report_id, permit_id)
    );
    IF COL_LENGTH(N'dbo.building_permits', N'report_id') IS NOT NULL
    BEGIN
        EXEC sys.sp_executesql N'
            INSERT INTO dbo.building_permit_reports (report_id, permit_id)
            SELECT p.report_id, p.id
            FROM dbo.building_permits p
            WHERE NOT EXISTS (
                SELECT 1 FROM dbo.building_permit_reports link
                WHERE link.report_id = p.report_id AND link.permit_id = p.id
            );';
        IF EXISTS (
            SELECT 1 FROM sys.foreign_keys
            WHERE name = N'FK_building_permits_report'
              AND parent_object_id = OBJECT_ID(N'dbo.building_permits')
        )
            ALTER TABLE dbo.building_permits DROP CONSTRAINT FK_building_permits_report;
        ALTER TABLE dbo.building_permits DROP COLUMN report_id;
    END;
    IF NOT EXISTS (
        SELECT 1 FROM sys.foreign_keys
        WHERE name = N'FK_building_permit_reports_report'
          AND parent_object_id = OBJECT_ID(N'dbo.building_permit_reports')
    )
        ALTER TABLE dbo.building_permit_reports ADD CONSTRAINT FK_building_permit_reports_report
            FOREIGN KEY (report_id) REFERENCES dbo.building_activity_reports(id) ON DELETE CASCADE;
    IF NOT EXISTS (
        SELECT 1 FROM sys.foreign_keys
        WHERE name = N'FK_building_permit_reports_permit'
          AND parent_object_id = OBJECT_ID(N'dbo.building_permit_reports')
    )
        ALTER TABLE dbo.building_permit_reports ADD CONSTRAINT FK_building_permit_reports_permit
            FOREIGN KEY (permit_id) REFERENCES dbo.building_permits(id) ON DELETE CASCADE;
    """,
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = N'UX_building_permits_permit_number'
          AND object_id = OBJECT_ID(N'dbo.building_permits')
    )
    CREATE UNIQUE INDEX UX_building_permits_permit_number
        ON dbo.building_permits (permit_number)
        WHERE permit_number IS NOT NULL AND permit_number <> N'';
    """,
    """
    IF OBJECT_ID(N'dbo.building_parcel_locations', N'U') IS NULL
    CREATE TABLE dbo.building_parcel_locations (
        parcel_number NVARCHAR(100) NOT NULL PRIMARY KEY,
        latitude DECIMAL(10, 7) NULL,
        longitude DECIMAL(10, 7) NULL,
        matched_address NVARCHAR(500) NULL,
        lookup_status NVARCHAR(20) NOT NULL,
        checked_at DATETIME2 NOT NULL
    );
    """,
)

_INSERT_PERMIT_SQL = """
INSERT INTO dbo.building_permits (
    page_number, line_number, permit_number, issue_date, address, permit_type,
    description, contractor, owner, subdivision, valuation, square_feet, raw_data,
    applied_date, permit_subtype, parcel_number, status, total_fees, total_paid, contacts_json
) OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_PERMIT_SQL = """
UPDATE dbo.building_permits SET
    page_number = ?, line_number = ?, permit_number = ?, issue_date = ?, address = ?, permit_type = ?,
    description = ?, contractor = ?, owner = ?, subdivision = ?, valuation = ?, square_feet = ?, raw_data = ?,
    applied_date = ?, permit_subtype = ?, parcel_number = ?, status = ?, total_fees = ?, total_paid = ?,
    contacts_json = ?
WHERE id = ?
"""


def _permit_values(record: PermitRecord) -> tuple:
    return (
        record.page_number, record.row_number, (record.permit_number or "").strip() or None,
        record.issue_date, record.address, record.permit_type, record.description, record.contractor,
        record.owner, record.subdivision, record.valuation, record.square_feet, record.raw_json,
        record.applied_date, record.permit_subtype, record.parcel_number, record.status,
        record.total_fees, record.total_paid, record.contacts_json,
    )


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
    env = load_settings(env)
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
    parts.update({
        "Encrypt": "yes",
        "TrustServerCertificate": "no",
        "Connection Timeout": env.get("AZURE_SQL_CONNECTION_TIMEOUT", DEFAULT_CONNECTION_TIMEOUT),
    })
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

    def get_unlocated_parcel_numbers(self, force: bool = False) -> list[str]:
        cursor = self.connection.cursor()
        if force:
            cursor.execute(
                "SELECT DISTINCT parcel_number FROM dbo.building_permits "
                "WHERE parcel_number IS NOT NULL AND parcel_number <> N'' ORDER BY parcel_number"
            )
        else:
            cursor.execute(
                "SELECT DISTINCT permits.parcel_number FROM dbo.building_permits AS permits "
                "LEFT JOIN dbo.building_parcel_locations AS locations "
                "ON locations.parcel_number = permits.parcel_number "
                "WHERE permits.parcel_number IS NOT NULL AND permits.parcel_number <> N'' "
                "AND locations.parcel_number IS NULL ORDER BY permits.parcel_number"
            )
        return [str(row[0]) for row in cursor.fetchall()]

    def save_parcel_locations(self, locations: Mapping[str, object], batch_size: int = 100) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        items = list(locations.items())
        if not items:
            return

        checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        cursor = self.connection.cursor()
        try:
            for start in range(0, len(items), batch_size):
                batch = items[start:start + batch_size]
                values_sql = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in batch)
                parameters = []
                for parcel_number, location in batch:
                    parameters.extend((
                        parcel_number,
                        location.latitude,
                        location.longitude,
                        location.matched_address,
                        location.status,
                        checked_at,
                    ))
                cursor.execute(
                    "MERGE dbo.building_parcel_locations WITH (HOLDLOCK) AS target "
                    f"USING (VALUES {values_sql}) AS source "
                    "(parcel_number, latitude, longitude, matched_address, lookup_status, checked_at) "
                    "ON target.parcel_number = source.parcel_number "
                    "WHEN MATCHED THEN UPDATE SET latitude = source.latitude, longitude = source.longitude, "
                    "matched_address = source.matched_address, lookup_status = source.lookup_status, "
                    "checked_at = source.checked_at "
                    "WHEN NOT MATCHED THEN INSERT "
                    "(parcel_number, latitude, longitude, matched_address, lookup_status, checked_at) "
                    "VALUES (source.parcel_number, source.latitude, source.longitude, source.matched_address, "
                    "source.lookup_status, source.checked_at);",
                    *parameters,
                )
                self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def save_report(
        self,
        source_url: str,
        title: str,
        content_sha256: str,
        records: Sequence[PermitRecord],
        category: str = "other",
        blob_uri: str | None = None,
        force: bool = False,
    ) -> bool:
        """Insert or replace a report and its permits. Returns False if the stored copy is unchanged."""
        existing = self.get_report(source_url)
        cursor = self.connection.cursor()
        if existing and existing[1] == content_sha256 and not force:
            cursor.execute(
                "UPDATE dbo.building_activity_reports SET title = ?, category = ?, blob_uri = ? WHERE id = ?",
                title, category, blob_uri, existing[0],
            )
            self.connection.commit()
            logger.info("Report %s unchanged; skipping", source_url)
            return False

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            if existing:
                report_id = existing[0]
                cursor.execute("DELETE FROM dbo.building_permit_reports WHERE report_id = ?", report_id)
                cursor.execute(
                    "UPDATE dbo.building_activity_reports SET title = ?, category = ?, blob_uri = ?, "
                    "content_sha256 = ?, permit_count = ?, ingested_at = ? WHERE id = ?",
                    title, category, blob_uri, content_sha256, len(records), now, report_id,
                )
            else:
                cursor.execute(
                    "INSERT INTO dbo.building_activity_reports (source_url, title, category, blob_uri, "
                    "content_sha256, permit_count, ingested_at) OUTPUT INSERTED.id VALUES (?, ?, ?, ?, ?, ?, ?)",
                    source_url, title, category, blob_uri, content_sha256, len(records), now,
                )
                report_id = int(cursor.fetchone()[0])

            for record in records:
                values = _permit_values(record)
                permit_number = values[2]
                permit_row = None
                if permit_number:
                    cursor.execute(
                        "SELECT id, issue_date FROM dbo.building_permits WHERE permit_number = ?",
                        permit_number,
                    )
                    permit_row = cursor.fetchone()

                if permit_row:
                    permit_id = int(permit_row[0])
                    existing_issue_date = permit_row[1]
                    if existing_issue_date is None or (
                        record.issue_date is not None and record.issue_date >= existing_issue_date
                    ):
                        cursor.execute(_UPDATE_PERMIT_SQL, *values, permit_id)
                else:
                    cursor.execute(_INSERT_PERMIT_SQL, *values)
                    permit_id = int(cursor.fetchone()[0])

                cursor.execute(
                    "IF NOT EXISTS (SELECT 1 FROM dbo.building_permit_reports WHERE report_id = ? AND permit_id = ?) "
                    "INSERT INTO dbo.building_permit_reports (report_id, permit_id) VALUES (?, ?)",
                    report_id, permit_id, report_id, permit_id,
                )

            cursor.execute(
                "DELETE FROM dbo.building_permits "
                "WHERE NOT EXISTS (SELECT 1 FROM dbo.building_permit_reports link "
                "WHERE link.permit_id = dbo.building_permits.id)"
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return True
