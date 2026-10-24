# realty_data_engine
gets realty data and parses it

The engine visits the City of Bentonville
[Building Activity Reports](https://www.bentonville.ar.gov/152/Building-Activity-Reports) page,
finds every permit report PDF linked from it (Document Center links, Archive Center
`Archive.aspx?ADID=` links, Archive Center listings and direct `.pdf` links), extracts the
permit tables from each PDF and stores them in an Azure SQL database.

## Setup

```bash
pip install -r requirements.txt
```

Writing to Azure SQL requires the [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).
Configure the connection with environment variables (see `.env.example`):

| Variable | Description |
| --- | --- |
| `AZURE_SQL_CONNECTION_STRING` | Full ODBC connection string (overrides everything below) |
| `AZURE_SQL_SERVER` | e.g. `myserver.database.windows.net` |
| `AZURE_SQL_DATABASE` | Database name |
| `AZURE_SQL_USERNAME` / `AZURE_SQL_PASSWORD` | SQL authentication credentials |
| `AZURE_SQL_AUTHENTICATION` | Optional, e.g. `ActiveDirectoryMsi` or `ActiveDirectoryPassword` |
| `AZURE_SQL_DRIVER` | Optional, defaults to `ODBC Driver 18 for SQL Server` |

## Usage

```bash
# Scrape, parse and load into Azure SQL (tables are created automatically)
python -m realty_data_engine

# Parse the PDFs and print the permits as JSON without touching the database
python -m realty_data_engine --dry-run --limit 2

# Re-download reports already stored; changed PDFs replace their previous permits
python -m realty_data_engine --force
```

Reports already in the database are skipped unless `--force` is given.

## Database schema

- `dbo.building_activity_reports` – one row per PDF (`source_url` is unique, `content_sha256`, `title`, `permit_count`, `ingested_at`).
- `dbo.building_permits` – one row per permit, linked by `report_id`, with normalized columns
  (`permit_number`, `applied_date`, `issue_date`, `address`, `permit_type`, `permit_subtype`,
  `parcel_number`, `status`, `subdivision`, `valuation`, `total_fees`, `total_paid`, `contractor`,
  `owner`) plus `contacts_json` for all contact types and `raw_data` for the extracted source values.

The Bentonville report parser reads the permit fields from the aligned white rows, associates the
light-blue contact rows with each permit, and ignores the monthly summary. Existing databases are
updated with the additional columns when the schema is ensured.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
