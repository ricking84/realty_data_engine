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

The CLI coordinates the pipeline and keeps network access, PDF parsing, and persistence in separate
modules:

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
| `realty_data_engine/cli.py` | Coordinates discovery, download, parsing, and either JSON output or database writes. Handles options such as `--dry-run`, `--limit`, and `--force`. |
| `realty_data_engine/scraper.py` | Fetches the reports page and linked archive listings, deduplicates report URLs, downloads PDFs, and checks that downloads are PDFs. |
| `realty_data_engine/parser.py` | Converts PDF content into `PermitRecord` objects. It first handles the Bentonville report's aligned text layout, then falls back to table extraction; normalized fields, contacts, and raw values are retained. |
| `realty_data_engine/storage.py` | Builds the Azure SQL connection, ensures the database schema, and transactionally saves reports and their permit records. |
| `preview.html` | Loads the JSON produced by `--dry-run` locally in the browser; it does not access Azure SQL. |

The parser has no network or database responsibilities, and the scraper does not interpret permit
fields. The CLI connects those stages. In normal mode it skips report URLs already in the database;
`--force` re-downloads them so changed PDFs can replace their stored permit records. In dry-run mode
the pipeline skips persistence and writes the report and permit data to standard output.

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

# Export the full parsed dataset for the browser preview
python -m realty_data_engine --dry-run > permits.json

# Re-download reports already stored; changed PDFs replace their previous permits
python -m realty_data_engine --force
```

Reports already in the database are skipped unless `--force` is given.

## Report collection schedule

The target collection workflow is:

1. On the first run, backfill all reports listed under **Monthly Reports**.
2. Every day at 8:00 AM Central Time (`America/Chicago`), check **Bi-Weekly Reports** and process
  only reports that have not already been ingested.
3. **Yearly Reports** are linked from the page's archive and are outside this initial monthly
  backfill and daily bi-weekly check.

This cadence is not yet automated by the project. The current CLI discovers all linked PDFs,
including reports in linked archive listings, on every run; it does not distinguish monthly,
bi-weekly, and yearly categories or schedule itself. A normal database run skips report URLs already
stored, so it avoids inserting the same report twice, but it does not limit processing to new
bi-weekly reports. Run the CLI manually for now; exact scheduling and category filtering require
additional automation.

## Browser preview

Open `preview.html` in a browser, then choose the `permits.json` file created by the dry-run command above. The preview supports searching and filtering reports, and selecting a permit opens its normalized fields, contacts and raw source values. The **Map** view looks up parcel numbers in Benton County's public GIS parcel layer and plots one marker per unique parcel over OpenStreetMap tiles. An internet connection is required; parcel centers are cached in the browser. Parcels without a valid GIS match are reported as not found rather than placed using a fuzzy address match. Benton County describes its GIS data as informational and as-is, and states that it is intended for non-commercial use; confirm the county's terms before using it commercially or for authoritative decisions.

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
