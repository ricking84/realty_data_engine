"""Batch parcel lookups against Benton County's public ArcGIS parcel layer."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from time import sleep
from urllib.parse import urljoin

import requests

PARCEL_QUERY_URL = "https://gis.bentoncountyar.gov/arcgis/rest/services/Assessor/ParcelMobile/MapServer/1/query"
PARCEL_ID_PATTERN = re.compile(r"^\d{2}-[\d-]+$")
DEFAULT_BATCH_SIZE = 50
DEFAULT_TIMEOUT = 60
BATCH_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class ParcelLocation:
    latitude: float | None
    longitude: float | None
    matched_address: str | None
    status: str


def _center_from_geometry(geometry: dict | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    if geometry.get("x") is not None and geometry.get("y") is not None:
        return float(geometry["y"]), float(geometry["x"])

    points = [point for ring in geometry.get("rings", []) for point in ring if len(point) >= 2]
    if not points:
        return None
    longitudes = [float(point[0]) for point in points]
    latitudes = [float(point[1]) for point in points]
    return (min(latitudes) + max(latitudes)) / 2, (min(longitudes) + max(longitudes)) / 2


def lookup_parcel_locations(
    parcel_numbers: Iterable[str],
    session: requests.Session | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
    batch_delay: float = BATCH_DELAY_SECONDS,
) -> dict[str, ParcelLocation]:
    """Return one location result for every unique valid parcel ID requested.

    Unmatched IDs are returned with status ``not_found`` so callers can cache the result
    instead of retrying every time the map is opened.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    parcel_ids = sorted({str(value).strip().upper() for value in parcel_numbers if value})
    parcel_ids = [parcel_id for parcel_id in parcel_ids if PARCEL_ID_PATTERN.fullmatch(parcel_id)]
    if not parcel_ids:
        return {}

    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "realty-data-engine/0.1")
    locations: dict[str, ParcelLocation] = {}

    for batch_index in range(0, len(parcel_ids), batch_size):
        batch = parcel_ids[batch_index:batch_index + batch_size]
        quoted_ids = ",".join("'" + parcel_id.replace("'", "''") + "'" for parcel_id in batch)
        response = session.get(
            PARCEL_QUERY_URL,
            params={
                "where": f"PARCELID IN ({quoted_ids})",
                "outFields": "PARCELID,PH_ADD",
                "returnGeometry": "true",
                "returnCentroid": "true",
                "outSR": "4326",
                "f": "json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RuntimeError(data["error"].get("message", "Parcel lookup failed"))

        found: set[str] = set()
        for feature in data.get("features", []):
            attributes = feature.get("attributes") or {}
            parcel_id = str(attributes.get("PARCELID") or "").strip().upper()
            if parcel_id not in batch:
                continue
            center = _center_from_geometry(feature.get("geometry"))
            found.add(parcel_id)
            if center:
                locations[parcel_id] = ParcelLocation(
                    latitude=center[0],
                    longitude=center[1],
                    matched_address=attributes.get("PH_ADD"),
                    status="located",
                )
            else:
                locations[parcel_id] = ParcelLocation(None, None, attributes.get("PH_ADD"), "no_geometry")

        for parcel_id in batch:
            if parcel_id not in found:
                locations[parcel_id] = ParcelLocation(None, None, None, "not_found")
        if batch_delay and batch_index + batch_size < len(parcel_ids):
            sleep(batch_delay)

    return locations
