"""Command line entry point: scrape Bentonville building activity PDFs and load them into Azure SQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys

from . import scraper
from .parser import parse_pdf
from .storage import AzureSqlStore, connect

logger = logging.getLogger("realty_data_engine")


def run(page_url: str, store: AzureSqlStore | None, force: bool = False, limit: int | None = None) -> dict:
    """Run the pipeline. When ``store`` is None the parsed permits are returned instead of saved."""
    session = scraper._new_session()
    links = scraper.find_report_links(page_url, session=session)
    logger.info("Found %d report PDF link(s) on %s", len(links), page_url)
    if limit is not None:
        links = links[:limit]

    summary = {"found": len(links), "saved": 0, "skipped": 0, "failed": 0, "reports": []}
    for link in links:
        if store is not None and not force and store.get_report(link.url):
            logger.info("Already ingested %s; skipping (use --force to re-check)", link.url)
            summary["skipped"] += 1
            continue
        try:
            content = scraper.download_pdf(link.url, session=session)
            records = parse_pdf(content)
        except Exception:  # Keep going so one bad document doesn't stop the run.
            logger.exception("Failed to process %s", link.url)
            summary["failed"] += 1
            continue

        logger.info("Parsed %d permit(s) from %s (%s)", len(records), link.title, link.url)
        if store is None:
            summary["reports"].append(
                {"url": link.url, "title": link.title, "permits": [r.to_dict() for r in records]}
            )
            continue
        sha256 = hashlib.sha256(content).hexdigest()
        if store.save_report(link.url, link.title, sha256, records):
            summary["saved"] += 1
        else:
            summary["skipped"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=scraper.REPORTS_PAGE_URL, help="Building activity reports page URL")
    parser.add_argument("--dry-run", action="store_true", help="Parse PDFs and print JSON instead of saving")
    parser.add_argument("--force", action="store_true", help="Re-download reports that are already stored")
    parser.add_argument("--limit", type=int, help="Only process the first N reports")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        summary = run(args.url, store=None, limit=args.limit)
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
        store.ensure_schema()
        summary = run(args.url, store=store, force=args.force, limit=args.limit)
    finally:
        connection.close()
    logger.info(
        "Done: %(found)d found, %(saved)d saved, %(skipped)d skipped, %(failed)d failed", summary
    )
    return 1 if summary["failed"] else 0
