from datetime import date
from decimal import Decimal
from pathlib import Path

from realty_data_engine.parser import map_header, parse_pdf, records_from_tables


def test_map_header():
    assert map_header("Permit #") == "permit_number"
    assert map_header("Permit Number") == "permit_number"
    assert map_header("Permit Type") == "permit_type"
    assert map_header("Date Issued") == "issue_date"
    assert map_header("Project Address") == "address"
    assert map_header("Sq. Ft.") == "square_feet"
    assert map_header("Job Valuation") == "valuation"
    assert map_header("Work Description") == "description"
    assert map_header("Applied Date") == "applied_date"
    assert map_header("Permit Subtype") == "permit_subtype"
    assert map_header("Parcel Number") == "parcel_number"
    assert map_header("Total Fees") == "total_fees"
    assert map_header("Total Paid") == "total_paid"
    assert map_header("Status") == "status"
    assert map_header("") is None


def test_parse_pdf_extracts_permits(permit_pdf):
    records = parse_pdf(permit_pdf)

    assert len(records) == 2  # the "Total" row is ignored
    first = records[0]
    assert first.permit_number == "BP-2024-0001"
    assert first.issue_date == date(2024, 1, 5)
    assert first.address == "123 Main Street"
    assert first.permit_type == "New Residential"
    assert first.contractor == "ABC Builders"
    assert first.valuation == Decimal("350000.00")
    assert first.square_feet == 2400
    assert first.page_number == 1
    assert first.raw["Permit #"] == "BP-2024-0001"
    assert records[1].valuation == Decimal("1250000")


def test_records_reuse_header_across_pages_and_skip_noise():
    tables = [
        (1, [["Permit No", "Address", "Contractor"], ["P-1", "1 A St", "ABC Builders"], [None, "", None]]),
        (2, [["P-2", "2 B St", "Main Street Homes"], ["Page 2 of 2", "", ""], ["short row"]]),
    ]

    records = records_from_tables(tables)

    assert [(r.permit_number, r.page_number, r.row_number) for r in records] == [("P-1", 1, 1), ("P-2", 2, 2)]
    assert records[1].contractor == "Main Street Homes"


def test_rows_before_any_header_are_ignored():
    assert records_from_tables([(1, [["Bentonville Building Activity", "January 2024"]])]) == []


def test_parse_bentonville_report_pdf():
    content = Path(__file__).parents[1] / "September-2026.pdf"
    records = parse_pdf(content.read_bytes())

    assert len(records) == 114
    first = records[0]
    assert first.permit_number == "CALT26-0069"
    assert first.applied_date == date(2026, 7, 10)
    assert first.issue_date == date(2026, 9, 15)
    assert first.permit_type == "ALTERATION REMODEL COM"
    assert first.permit_subtype == "INTERIOR REMODEL"
    assert first.address == "2700 SE S ST 200"
    assert first.parcel_number == "01-08559-004"
    assert first.status == "ISSUED"
    assert first.subdivision == "BENTONVILLE IND PARK SOUTH (BIDC)"
    assert first.valuation == Decimal("300000.00")
    assert first.contacts == {
        "APPLICANT": "CROSSMAR INVESTMENTS",
        "CONTRACTOR": "CROSSMAR INVESTMENTS",
        "ELECTRICIAN": "BLACKLINE ELECTRIC",
        "MECHANICAL": "GOLD MECHANICAL JEREMY STOVALL",
        "OWNER": "CROSSMAR INVESTMENTS IV LLC",
        "PLUMBER": "DICKEY'S PLUMBING",
    }
    assert first.total_fees is None
    assert records[-1].owner == "RESPONSIVEED HOLDINGS LLC"
    assert "Total Number of Permits" not in records[-1].raw_json
    assert {record.status for record in records} <= {"ISSUED", "FINALED", "APPROVED", "CO ISSUED"}
    assert sum(record.valuation for record in records) == Decimal("26502679.70")
