# realty_data_engine

`realty_data_engine` is a Python command-line pipeline for collecting and inspecting building
permit data published by the City of Bentonville. It discovers report PDFs linked from the
[Building Activity Reports](https://www.bentonville.ar.gov/152/Building-Activity-Reports) page,
extracts permit records, and either writes them to Azure SQL or prints them as JSON for review.
The included static browser preview can load that JSON without connecting to the database.

## What it does

- Finds PDF reports on the city page and linked Archive Center listings, including Document Center
  links, Archive Center document links, and direct PDF links.
- Downloads and parses the reports into normalized permit fields, retaining contact details and
  the extracted source values for traceability.
- Saves reports and permits to Azure SQL, or uses `--dry-run` to emit the parsed records as JSON.
- Provides `preview.html` to search and filter a dry-run dataset in a browser and inspect individual
  permits.

## Architecture

The CLI coordinates the pipeline and keeps network access, PDF parsing, PDF archiving, and persistence
in separate modules:

```text
python -m realty_data_engine
          |
          v
      __main__.py -> cli.py -> scraper.py -> parser.py -> storage.py -> Azure SQL
                                      parsed records -> JSON stdout (`--dry-run`)
                                                              |
                                                              v
                                                     preview.html
```

| Component | Responsibility |
| --- | --- |
| `realty_data_engine/__main__.py` | Starts the command-line interface. |
| `realty_data_engine/cli.py` | Coordinates discovery, download, parsing, Blob archival, and database writes. Handles options such as `--category`, `--dry-run`, `--limit`, and `--force`. |
| `realty_data_engine/scraper.py` | Fetches report pages and linked archive listings, categorizes reports, deduplicates URLs, downloads PDFs, and validates PDF content. |
| `realty_data_engine/parser.py` | Converts PDF content into `PermitRecord` objects. It handles current Bentonville layouts and the older 2005–2016 block layout, then falls back to generic table extraction; normalized fields, contacts, and raw values are retained. |
| `realty_data_engine/archive.py` | Saves original PDFs to Azure Blob Storage using content-addressed names and Azure identity credentials. |
| `realty_data_engine/storage.py` | Builds the Azure SQL connection, provides one-time schema setup, and transactionally saves report metadata and permit records. |
| `preview.html` | Loads the JSON produced by `--dry-run` locally in the browser; it does not access Azure SQL. |

The parser has no network or database responsibilities, and the scraper does not interpret permit
fields. The CLI connects those stages. Each run downloads selected reports and compares their SHA-256
hashes with SQL, so changed PDFs at an existing URL are detected. In dry-run mode the pipeline skips
SQL and Blob writes and prints report categories and permit data as JSON.

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
| `AZURE_STORAGE_ACCOUNT_URL` | Blob service URL, e.g. `https://account.blob.core.windows.net` |
| `AZURE_STORAGE_CONTAINER` | Optional Blob container name; defaults to `permit-reports` |

Blob access uses `DefaultAzureCredential`: run `az login` for local development, and assign the
hosted workload's managed identity the **Storage Blob Data Contributor** role on the storage account.
Create or migrate the SQL schema with a DDL-capable account after initial deployment and whenever
the schema changes. Routine ingestion can then use a least-privilege identity:

```bash
python -m realty_data_engine --ensure-schema
```

## Usage

```bash
# Backfill Monthly Reports into Azure SQL and Blob Storage
python -m realty_data_engine --category monthly

# Check Bi-Weekly Reports; already-ingested report URLs are skipped before download
python -m realty_data_engine --category biweekly

# Reprocess all Monthly Reports even if their PDF content is unchanged
python -m realty_data_engine --category monthly --force

# Import only annual reports from 2017 through 2025
python -m realty_data_engine --category yearly --year-from 2017 --year-to 2025

# Parse the PDFs and print the permits as JSON without touching the database
python -m realty_data_engine --dry-run --limit 2

# Export the full parsed dataset for the browser preview
python -m realty_data_engine --dry-run > permits.json

# Local SQL-only development without Blob Storage
python -m realty_data_engine --category monthly --skip-blob-archive

# Re-download reports already stored; changed PDFs replace their previous permits
python -m realty_data_engine --force
```

Unchanged reports are skipped after their downloaded content hash matches SQL. Changed PDFs replace
the report's permits and update its Blob URI. `--force` replaces records even when the PDF hash is
unchanged. Use `--category all` to process every discovered category.

## Report collection schedule

The target collection workflow is:

1. On the first run, backfill all reports listed under **Monthly Reports**.
2. Every day at 8:00 AM Central Time (`America/Chicago`), check **Bi-Weekly Reports** and process
  only reports that have not already been ingested.
3. **Yearly Reports** are linked from the page's archive and are outside this initial monthly
  backfill and daily bi-weekly check.

The CLI can select `monthly`, `biweekly`, or `yearly` reports, but it does not schedule itself. Run
the initial monthly backfill once, then schedule `python -m realty_data_engine --category biweekly`
daily at 8:00 AM Central Time. Existing report URLs are skipped before download, so daily runs only
download new bi-weekly URLs. `--force` explicitly re-downloads and replaces all selected reports.
For DST-aware scheduling, use a Logic Apps Recurrence trigger configured for Central Time; the Logic
App can start a hosted job.

## Browser preview

Open `preview.html` in a browser, then choose the `permits.json` file created by the dry-run command above. The preview supports searching and filtering reports, and selecting a permit opens its normalized fields, contacts and raw source values. The **Map** view looks up parcel numbers in Benton County's public GIS parcel layer and plots one marker per unique parcel over OpenStreetMap tiles. An internet connection is required; parcel centers are cached in the browser. Parcels without a valid GIS match are reported as not found rather than placed using a fuzzy address match. Benton County describes its GIS data as informational and as-is, and states that it is intended for non-commercial use; confirm the county's terms before using it commercially or for authoritative decisions.

## Database schema

- `dbo.building_activity_reports` – one row per PDF (`source_url` is unique, `title`, `category`, `content_sha256`, `blob_uri`, `permit_count`, `ingested_at`).
- `dbo.building_permits` – one canonical row per non-empty `permit_number`, protected by a unique
  filtered index, with normalized columns
  (`permit_number`, `applied_date`, `issue_date`, `address`, `permit_type`, `permit_subtype`,
  `parcel_number`, `status`, `subdivision`, `valuation`, `total_fees`, `total_paid`, `contractor`,
  `owner`) plus `contacts_json` for all contact types and `raw_data` for the extracted source values.
- `dbo.building_permit_reports` – links canonical permits to every source PDF that contains them,
  so monthly and bi-weekly reports can share a permit row without losing provenance.

The Bentonville report parser reads the permit fields from the aligned white rows, associates the
light-blue contact rows with each permit, and ignores the monthly summary. Existing databases are
updated with additional columns and report links when `--ensure-schema` is run. The migration
backfills existing report links, removes the old single `report_id` from permits, and adds a unique
index on non-empty permit numbers. Check for and resolve duplicates before applying it to an existing
database. The current Azure dataset was checked and has no duplicate permit numbers.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
