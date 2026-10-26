"""Serve the local permit preview and a read-only Azure SQL dataset endpoint."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_PREVIEW_PATH = Path(__file__).resolve().parent.parent / "preview.html"


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _decode_json(value, fallback):
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def load_dataset(connection) -> dict:
    """Return the preview-compatible dataset shape using read-only SQL queries."""
    cursor = connection.cursor()
    cursor.execute(
        "SELECT id, source_url, title, category FROM dbo.building_activity_reports "
        "ORDER BY category, title, id"
    )
    reports = {}
    for row in cursor.fetchall():
        report_id = int(row[0])
        reports[report_id] = {
            "url": row[1],
            "title": row[2] or row[1],
            "category": row[3],
            "permits": [],
        }

    if reports:
        cursor.execute(
            "SELECT links.report_id, permits.id, permits.page_number, permits.line_number, "
            "permits.permit_number, permits.applied_date, permits.issue_date, permits.address, "
            "permits.permit_type, permits.permit_subtype, permits.description, permits.contractor, "
            "permits.owner, permits.subdivision, permits.parcel_number, permits.status, "
            "permits.valuation, permits.total_fees, permits.total_paid, permits.square_feet, "
            "permits.contacts_json, permits.raw_data, locations.latitude, locations.longitude, "
            "locations.lookup_status "
            "FROM dbo.building_permit_reports AS links "
            "INNER JOIN dbo.building_permits AS permits ON permits.id = links.permit_id "
            "LEFT JOIN dbo.building_parcel_locations AS locations "
            "ON locations.parcel_number = permits.parcel_number "
            "ORDER BY links.report_id, permits.issue_date, permits.permit_number"
        )
        for row in cursor.fetchall():
            report = reports.get(int(row[0]))
            if report is None:
                continue
            report["permits"].append({
                "id": int(row[1]),
                "page_number": row[2],
                "row_number": row[3],
                "permit_number": row[4],
                "applied_date": _json_value(row[5]),
                "issue_date": _json_value(row[6]),
                "address": row[7],
                "permit_type": row[8],
                "permit_subtype": row[9],
                "description": row[10],
                "contractor": row[11],
                "owner": row[12],
                "subdivision": row[13],
                "parcel_number": row[14],
                "status": row[15],
                "valuation": _json_value(row[16]),
                "total_fees": _json_value(row[17]),
                "total_paid": _json_value(row[18]),
                "square_feet": row[19],
                "contacts": _decode_json(row[20], {}),
                "raw": _decode_json(row[21], {}),
                "latitude": _json_value(row[22]),
                "longitude": _json_value(row[23]),
                "location_status": row[24],
            })

    report_list = list(reports.values())
    return {
        "found": len(report_list),
        "saved": 0,
        "skipped": 0,
        "failed": 0,
        "reports": report_list,
    }


class _PreviewHandler(BaseHTTPRequestHandler):
    server_version = "RealtyDataEngine/0.1"

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/preview.html"):
            try:
                content = _PREVIEW_PATH.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path != "/api/dataset":
            self.send_error(404)
            return

        from .storage import connect

        connection = None
        try:
            connection = connect()
            payload = json.dumps(load_dataset(connection), default=_json_value).encode("utf-8")
        except Exception:
            logger.exception("Failed to load permit dataset from Azure SQL")
            payload = json.dumps({"error": "Could not load the database dataset. Check the local server log."}).encode("utf-8")
            self.send_response(500)
        else:
            self.send_response(200)
        finally:
            if connection is not None:
                connection.close()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_string, *args):
        logger.info("%s - %s", self.address_string(), format_string % args)


def serve(port: int = 8765) -> None:
    """Serve only the preview and read-only dataset API on loopback."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _PreviewHandler)
    logger.info("Local permit preview: http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping local permit preview")
    finally:
        server.server_close()
