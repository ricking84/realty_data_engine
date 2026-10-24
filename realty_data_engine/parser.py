"""Extracts permit rows from building activity report PDFs."""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

logger = logging.getLogger(__name__)

# Ordered (field, pattern) rules used to map a PDF column header onto a normalized field.
# The first matching rule wins, so more specific patterns come first.
_HEADER_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("permit_type", re.compile(r"(permit|work|construction)\s*(type|class|category)|^type$|^class$|^category$")),
    ("issue_date", re.compile(r"date|issued")),
    ("permit_number", re.compile(r"permit\s*(#|no\b|num|number|id)|^permit$|^number$|^#$|^no\.?$")),
    ("address", re.compile(r"address|location|site|street")),
    ("contractor", re.compile(r"contractor|builder|applicant")),
    ("owner", re.compile(r"owner")),
    ("valuation", re.compile(r"valuation|value|cost|amount")),
    ("square_feet", re.compile(r"sq\.?\s*f|square|sf\b|footage|area")),
    ("subdivision", re.compile(r"subdivision|subdiv|addition|lot")),
    ("description", re.compile(r"description|desc|scope|project|work|use")),
]

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%b %d, %Y", "%B %d, %Y")


@dataclass
class PermitRecord:
    page_number: int
    row_number: int
    permit_number: str | None = None
    issue_date: date | None = None
    address: str | None = None
    permit_type: str | None = None
    description: str | None = None
    contractor: str | None = None
    owner: str | None = None
    subdivision: str | None = None
    valuation: Decimal | None = None
    square_feet: int | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.issue_date:
            data["issue_date"] = self.issue_date.isoformat()
        if self.valuation is not None:
            data["valuation"] = str(self.valuation)
        return data


def _clean(cell: object) -> str:
    if cell is None:
        return ""
    return " ".join(str(cell).split())


def map_header(header: str) -> str | None:
    """Return the normalized field name for a column header, or None if unrecognized."""
    text = _clean(header).lower()
    if not text:
        return None
    for name, pattern in _HEADER_RULES:
        if pattern.search(text):
            return name
    return None


def parse_date(value: str) -> date | None:
    value = _clean(value)
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_decimal(value: str) -> Decimal | None:
    text = re.sub(r"[,$\s]", "", _clean(value))
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return -number if negative else number


def parse_int(value: str) -> int | None:
    number = parse_decimal(value)
    return int(number) if number is not None else None


def _is_header_row(row: list[str]) -> bool:
    # Data rows always carry permit numbers, dates or amounts; header rows never contain digits.
    if any(char.isdigit() for cell in row for char in cell):
        return False
    mapped = {map_header(cell) for cell in row if cell}
    mapped.discard(None)
    return len(mapped) >= 2 and ("permit_number" in mapped or "address" in mapped)


def _unique_headers(row: list[str]) -> list[str]:
    headers: list[str] = []
    for index, cell in enumerate(row):
        name = cell or f"column_{index + 1}"
        if name in headers:
            name = f"{name}_{index + 1}"
        headers.append(name)
    return headers


def _is_summary_row(values: list[str]) -> bool:
    first = next((value for value in values if value), "").lower()
    return first.startswith(("total", "grand total", "page ", "count"))


def records_from_tables(tables: list[tuple[int, list[list[object]]]]) -> list[PermitRecord]:
    """Convert extracted ``(page_number, table)`` pairs into permit records.

    Tables that continue on following pages without repeating the header row reuse the
    most recent header with the same number of columns.
    """
    records: list[PermitRecord] = []
    headers: list[str] | None = None
    for page_number, table in tables:
        for raw_row in table:
            row = [_clean(cell) for cell in raw_row]
            if not any(row):
                continue
            if _is_header_row(row):
                headers = _unique_headers(row)
                continue
            if headers is None or len(row) != len(headers) or _is_summary_row(row):
                continue
            record = PermitRecord(page_number=page_number, row_number=len(records) + 1)
            record.raw = {header: value for header, value in zip(headers, row)}
            for header, value in record.raw.items():
                name = map_header(header)
                if not name or not value or getattr(record, name) is not None:
                    continue
                if name == "issue_date":
                    record.issue_date = parse_date(value)
                elif name == "valuation":
                    record.valuation = parse_decimal(value)
                elif name == "square_feet":
                    record.square_feet = parse_int(value)
                else:
                    setattr(record, name, value)
            records.append(record)
    return records


def parse_pdf(content: bytes) -> list[PermitRecord]:
    """Parse the permit table(s) out of a building activity report PDF."""
    tables: list[tuple[int, list[list[object]]]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_tables = page.extract_tables()
            if not page_tables:
                # Fall back to whitespace-aligned text detection for PDFs without ruling lines.
                page_tables = page.extract_tables({"vertical_strategy": "text", "horizontal_strategy": "text"})
            tables.extend((page_number, table) for table in page_tables)
    records = records_from_tables(tables)
    if not records:
        logger.warning("No permit rows could be extracted from PDF")
    return records
