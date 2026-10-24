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
    ("applied_date", re.compile(r"applied\s*date")),
    ("permit_subtype", re.compile(r"permit\s*subtype|^subtype$")),
    ("parcel_number", re.compile(r"parcel\s*(#|no\b|num|number)")),
    ("total_fees", re.compile(r"total\s*fees")),
    ("total_paid", re.compile(r"total\s*paid")),
    ("status", re.compile(r"^status$")),
    ("permit_type", re.compile(r"(permit|work|construction)\s*(type|class|category)|^type$|^class$|^category$")),
    ("issue_date", re.compile(r"issued\s*date|date\s*issued|issue\s*date|^date$")),
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
    applied_date: date | None = None
    issue_date: date | None = None
    address: str | None = None
    permit_type: str | None = None
    permit_subtype: str | None = None
    description: str | None = None
    contractor: str | None = None
    owner: str | None = None
    subdivision: str | None = None
    parcel_number: str | None = None
    status: str | None = None
    valuation: Decimal | None = None
    total_fees: Decimal | None = None
    total_paid: Decimal | None = None
    square_feet: int | None = None
    contacts: dict[str, str] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, default=str)

    @property
    def contacts_json(self) -> str:
        return json.dumps(self.contacts, ensure_ascii=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.applied_date:
            data["applied_date"] = self.applied_date.isoformat()
        if self.issue_date:
            data["issue_date"] = self.issue_date.isoformat()
        for name in ("valuation", "total_fees", "total_paid"):
            if getattr(self, name) is not None:
                data[name] = str(getattr(self, name))
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


_PERMIT_NUMBER = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_PARCEL_NUMBER = re.compile(r"\b\d{2}-\d{5}-\d{3}\b")
_CONTACT_HEADER = re.compile(r"contact\s+type.*contact\s+name", re.IGNORECASE)
_CONTACT_TYPE = re.compile(r"^[A-Z][A-Z &/-]*$")


def _page_lines(page) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(page.extract_words(), key=lambda item: (item["top"], item["x0"])):
        if word["top"] < 90 or word["top"] > 720:
            continue
        if not lines or word["top"] - lines[-1][0]["top"] > 3:
            lines.append([word])
        else:
            lines[-1].append(word)
    return lines


def _column_text(words: list[dict], lower: float, upper: float) -> str:
    return _clean(" ".join(word["text"] for word in words if lower <= word["x0"] < upper))


def _append_text(current: str | None, addition: str) -> str:
    return f"{current} {addition}".strip() if current else addition


def _split_status(value: str) -> tuple[str | None, str]:
    match = re.search(r"\b(CO ISSUED|ISSUED|FINALED|APPROVED)\b", value, re.IGNORECASE)
    if match:
        return match.group().upper(), f"{value[:match.start()]} {value[match.end():]}".strip()
    return value or None, ""


def _parse_report_pages(pdf) -> list[PermitRecord]:
    records: list[PermitRecord] = []
    current: PermitRecord | None = None
    in_contacts = False
    last_contact: str | None = None

    for page_number, page in enumerate(pdf.pages, start=1):
        for words in _page_lines(page):
            text = _clean(" ".join(word["text"] for word in words))
            if text.lower().startswith(("total number of permits:", "total valuation:")):
                continue
            if _CONTACT_HEADER.search(text):
                in_contacts = True
                last_contact = None
                continue

            permit_word = next((word["text"] for word in words if _PERMIT_NUMBER.fullmatch(word["text"])), None)
            if permit_word:
                first, second, third, fourth = (
                    _column_text(words, 0, 145),
                    _column_text(words, 145, 290),
                    _column_text(words, 290, 435),
                    _column_text(words, 435, float("inf")),
                )
                current = PermitRecord(page_number=page_number, row_number=len(records) + 1)
                current.permit_number = permit_word
                current.permit_type = second
                current.address = third
                current.valuation = parse_decimal(fourth)
                current.raw.update({
                    "permit_number": permit_word,
                    "permit_type": second,
                    "address": third,
                    "valuation": fourth,
                })
                records.append(current)
                in_contacts = False
                last_contact = None
                continue

            if current is None:
                continue

            if in_contacts:
                contact_type = _column_text(words, 0, 180)
                contact_name = _column_text(words, 250, float("inf"))
                if contact_type and contact_type.lower() not in {"contact type", "contact"}:
                    if _CONTACT_TYPE.fullmatch(contact_type):
                        last_contact = contact_type
                        current.contacts[last_contact] = contact_name
                    elif contact_name:
                        last_contact = None
                elif contact_name and last_contact:
                    current.contacts[last_contact] = _append_text(
                        current.contacts[last_contact], contact_name
                    )
                continue

            first, second, third, fourth = (
                _column_text(words, 0, 145),
                _column_text(words, 145, 290),
                _column_text(words, 290, 435),
                _column_text(words, 435, float("inf")),
            )
            row_date = parse_date(first)
            if row_date and current.applied_date is None:
                current.applied_date = row_date
                current.permit_subtype = second or None
                parcel = _PARCEL_NUMBER.search(third)
                if parcel:
                    current.parcel_number = parcel.group()
                    address_continuation = third[:parcel.start()].strip()
                    if address_continuation:
                        current.address = _append_text(current.address, address_continuation)
                        current.raw["address"] = current.address
                current.total_fees = parse_decimal(fourth)
                current.raw.update({
                    "applied_date": first,
                    "permit_subtype": second,
                    "parcel_number": current.parcel_number or third,
                    "total_fees": fourth,
                })
            elif row_date and current.issue_date is None:
                current.issue_date = row_date
                current.status, status_remainder = _split_status(second)
                subdivision = " ".join(part for part in (status_remainder, third) if part)
                current.subdivision = subdivision or None
                current.total_paid = parse_decimal(fourth)
                current.raw.update({
                    "issue_date": first,
                    "status": current.status or "",
                    "subdivision": current.subdivision or "",
                    "total_paid": fourth,
                })
            elif third:
                parcel = _PARCEL_NUMBER.search(third)
                if parcel and current.parcel_number is None:
                    current.parcel_number = parcel.group()
                    address_continuation = third[:parcel.start()].strip()
                    if address_continuation:
                        current.address = _append_text(current.address, address_continuation)
                        current.raw["address"] = current.address
                elif current.issue_date:
                    current.subdivision = _append_text(current.subdivision, third)
                    current.raw["subdivision"] = current.subdivision
                elif current.applied_date is None:
                    current.address = _append_text(current.address, third)
                    current.raw["address"] = current.address
            elif second and current.applied_date and current.permit_subtype:
                current.permit_subtype = _append_text(current.permit_subtype, second)
                current.raw["permit_subtype"] = current.permit_subtype

    for record in records:
        record.contractor = record.contacts.get("CONTRACTOR")
        record.owner = record.contacts.get("OWNER")
        record.raw["contacts"] = record.contacts
    return records


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
                elif name == "applied_date":
                    record.applied_date = parse_date(value)
                elif name == "valuation":
                    record.valuation = parse_decimal(value)
                elif name == "total_fees":
                    record.total_fees = parse_decimal(value)
                elif name == "total_paid":
                    record.total_paid = parse_decimal(value)
                elif name == "square_feet":
                    record.square_feet = parse_int(value)
                else:
                    setattr(record, name, value)
            records.append(record)
    return records


def parse_pdf(content: bytes) -> list[PermitRecord]:
    """Parse the permit table(s) out of a building activity report PDF."""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        records = _parse_report_pages(pdf)
        if records:
            return records

        tables: list[tuple[int, list[list[object]]]] = []
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
