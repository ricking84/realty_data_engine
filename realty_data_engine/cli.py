"""Command line entry point: scrape Bentonville building activity PDFs and load them into Azure SQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys

from . import scraper
from .archive import BlobPdfArchive
from .parser import parse_pdf
from .storage import AzureSqlStore, connect

logger = logging.getLogger("realty_data_engine")
_REPORT_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def run(
    page_url: str,
    store: AzureSqlStore | None,
    force: bool = False,
    limit: int | None = None,
    category: str = "all",
    archive: BlobPdfArchive | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict:
    """Run the pipeline. When ``store`` is None the parsed permits are returned instead of saved."""
    session = scraper._new_session()
    links = scraper.find_report_links(page_url, session=session)
    if category != "all":
        links = [link for link in links if link.category == category]
    if year_from is not None or year_to is not None:
        filtered_links = []
        for link in links:
            match = _REPORT_YEAR.search(link.title)
            if not match:
                continue
            report_year = int(match.group())
            if year_from is not None and report_year < year_from:
                continue
            if year_to is not None and report_year > year_to:
                continue
            filtered_links.append(link)
        links = filtered_links
    logger.info("Found %d %s report PDF link(s) on %s", len(links), category, page_url)
    if limit is not None:
        links = links[:limit]

    summary = {"found": len(links), "saved": 0, "skipped": 0, "failed": 0, "reports": []}
    for link in links:
        if store is not None and not force and store.get_report(link.url):
            logger.info("Already ingested %s; skipping (use --force to re-download)", link.url)
            summary["skipped"] += 1
            continue
        try:
            content = scraper.download_pdf(link.url, session=session)
            sha256 = hashlib.sha256(content).hexdigest()
            blob_uri = archive.archive_pdf(content, link.category, sha256) if archive else None
            records = parse_pdf(content)
        except Exception:  # Keep going so one bad document doesn't stop the run.
            logger.exception("Failed to process %s", link.url)
            summary["failed"] += 1
            continue

        logger.info("Parsed %d permit(s) from %s (%s)", len(records), link.title, link.url)
        if store is None:
            summary["reports"].append(
                {
                    "url": link.url,
                    "title": link.title,
                    "category": link.category,
                    "blob_uri": blob_uri,
                    "permits": [r.to_dict() for r in records],
                }
            )
            continue
        if store.save_report(
            link.url,
            link.title,
            sha256,
            records,
            category=link.category,
            blob_uri=blob_uri,
            force=force,
        ):
            summary["saved"] += 1
        else:
            summary["skipped"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=scraper.REPORTS_PAGE_URL, help="Building activity reports page URL")
    parser.add_argument("--dry-run", action="store_true", help="Parse PDFs and print JSON instead of saving")
    parser.add_argument("--category", choices=("all", "monthly", "biweekly", "yearly", "other"), default="all", help="Only process reports in this section")
    parser.add_argument("--year-from", type=int, help="Include reports starting with this year")
    parser.add_argument("--year-to", type=int, help="Include reports through this year")
    parser.add_argument("--force", action="store_true", help="Replace stored records even when the PDF is unchanged")
    parser.add_argument("--limit", type=int, help="Only process the first N reports")
    parser.add_argument("--ensure-schema", action="store_true", help="Create or migrate the database schema, then exit")
    parser.add_argument("--refresh-parcel-locations", action="store_true", help="Resolve missing parcel coordinates and cache them in SQL")
    parser.add_argument("--skip-blob-archive", action="store_true", help="Skip PDF archival (local development only)")
    parser.add_argument("--serve", action="store_true", help="Serve the local browser preview with a read-only SQL API")
    parser.add_argument("--port", type=int, default=8765, help="Local preview server port (default: 8765)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)
    if args.year_from is not None and args.year_to is not None and args.year_from > args.year_to:
        parser.error("--year-from must be less than or equal to --year-to")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.serve:
        from .web import serve

        try:
            serve(args.port)
        except OSError as error:
            logger.error("Could not start local preview server: %s", error)
            return 2
        return 0

    if args.dry_run:
        summary = run(
            args.url,
            store=None,
            limit=args.limit,
            category=args.category,
            year_from=args.year_from,
            year_to=args.year_to,
        )
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 1 if summary["failed"] else 0

    try:
        connection = connect()
    except ValueError as error:
        logger.error("%s", error)
        return 2
    try:
        store = AzureSqlStore(connection)
        if args.ensure_schema:
            store.ensure_schema()
            logger.info("Database schema is ready")
            return 0
        if args.refresh_parcel_locations:
            from .geocoder import lookup_parcel_locations

            parcel_numbers = store.get_unlocated_parcel_numbers(force=args.force)
            if not parcel_numbers:
                logger.info("All permit parcel numbers already have cached locations")
                return 0
            locations = lookup_parcel_locations(parcel_numbers)
            store.save_parcel_locations(locations)
            located = sum(location.status == "located" for location in locations.values())
            logger.info(
                "Parcel location refresh complete: %d checked, %d located, %d not located",
                len(locations), located, len(locations) - located,
            )
            return 0
        if args.skip_blob_archive:
            archive = None
        else:
            try:
                archive = BlobPdfArchive.from_environment()
            except (ValueError, RuntimeError) as error:
                logger.error("%s", error)
                return 2
        summary = run(
            args.url,
            store=store,
            force=args.force,
            limit=args.limit,
            category=args.category,
            archive=archive,
            year_from=args.year_from,
            year_to=args.year_to,
        )
    finally:
        connection.close()
    logger.info(
        "Done: %(found)d found, %(saved)d saved, %(skipped)d skipped, %(failed)d failed", summary
    )
    return 1 if summary["failed"] else 0
