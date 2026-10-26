from datetime import date
from decimal import Decimal
from pathlib import Path

from realty_data_engine.parser import _parse_legacy_report_pages, map_header, parse_pdf, records_from_tables


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
    assert first.address == "123 Main Street, Bentonville, AR 72712"
    assert first.raw["Address"] == "123 Main Street"
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


def test_parse_legacy_annual_report_blocks_and_continuations():
    def word(text, x0, top):
        return {"text": text, "x0": x0, "top": top}

    first_page = [
        word("15-00000165", 36, 120), word("CALT", 106, 120), word("COM", 133, 120),
        word("COMMERCIAL", 155, 120), word("ALTERATION/REMODEL", 214, 120),
        word("3606", 387, 120), word("SE", 414, 120), word("METRO", 430, 120),
        word("PKWY", 463, 120), word("M", 490, 120), word("MAGELLAN", 535, 120),
        word("DEVELOPMENT", 600, 120), word("CO", 668, 120), word("LLC", 690, 120),
        word("-----------", 36, 129), word("2/05/15", 139, 129), word("$6700", 241, 129),
        word("01-07425-000-00268-", 387, 129), word("-7", 500, 129),
        word("STRUCTURE", 63, 154), word("-", 117, 154), word("000", 128, 154),
        word("000", 149, 154), word("77", 176, 154), word("SF", 192, 154),
        word("VESTIBULE", 209, 154), word("ISSUE", 398, 154), word("DATE", 430, 154),
        word("SUBCONTRACTOR(S)", 538, 154), word("PERMIT", 79, 162), word("TYPE", 117, 162),
        word("BLDG", 176, 162), word("00", 203, 162), word("BUILDING", 220, 162),
        word("PERMIT", 268, 162), word("5/31/16", 403, 162),
    ]
    second_page = [word("15-00000165", 36, 120), word("(CONTINUED)", 110, 120)]
    pdf = type("FakePdf", (), {
        "pages": [
            type("FakePage", (), {"extract_words": lambda self: first_page})(),
            type("FakePage", (), {"extract_words": lambda self: second_page})(),
        ]
    })()

    records = _parse_legacy_report_pages(pdf)

    assert len(records) == 1
    permit = records[0]
    assert permit.permit_number == "15-00000165"
    assert permit.applied_date == date(2015, 2, 5)
    assert permit.issue_date == date(2016, 5, 31)
    assert permit.address == "3606 SE METRO PKWY"
    assert permit.owner == "MAGELLAN DEVELOPMENT CO LLC"
    assert permit.valuation == Decimal("6700")
    assert permit.square_feet == 77


def test_parse_legacy_issue_row_valuation_not_parcel_suffix():
    def word(text, x0, top):
        return {"text": text, "x0": x0, "top": top}

    words = [
        word("13-00001448", 36, 120), word("SFD", 106, 120), word("RES", 133, 120),
        word("NEW", 155, 120), word("SINGLE", 176, 120), word("FAMILY", 214, 120),
        word("DWELLING", 252, 120), word("407", 387, 120), word("NW", 409, 120),
        word("LINEBARGER", 425, 120), word("LN", 484, 120),
        word("-----------", 36, 129), word("11/04/13", 133, 129),
        word("01-10342-000-00596-", 387, 129), word("-111", 500, 129),
        word("CORBY", 587, 129), word("BRADT", 619, 129),
        word("STRUCTURE", 63, 154), word("-", 117, 154), word("000", 128, 154),
        word("000", 149, 154), word("5BR,", 176, 154), word("ISSUE", 398, 154),
        word("DATE", 430, 154), word("VALUATION", 462, 154), word("SUBCONTRACTOR(S)", 538, 154),
        word("PERMIT", 79, 162), word("TYPE", 117, 162), word("BLDG", 176, 162),
        word("1/03/14", 403, 162), word("756961", 479, 162), word("CORBY", 538, 162),
        word("BRADT", 571, 162),
    ]
    pdf = type("FakePdf", (), {
        "pages": [type("FakePage", (), {"extract_words": lambda self: words})()]
    })()

    records = _parse_legacy_report_pages(pdf)

    assert len(records) == 1
    assert records[0].permit_number == "13-00001448"
    assert records[0].issue_date == date(2014, 1, 3)
    assert records[0].valuation == Decimal("756961")
    assert records[0].address == "407 NW LINEBARGER LN"


def test_parse_bentonville_report_pdf():
    content = Path(__file__).parents[1] / "docs" / "September-2026.pdf"
    records = parse_pdf(content.read_bytes())

    assert len(records) == 114
    first = records[0]
    assert first.permit_number == "CALT26-0069"
    assert first.applied_date == date(2026, 7, 10)
    assert first.issue_date == date(2026, 9, 15)
    assert first.permit_type == "ALTERATION REMODEL COM"
    assert first.permit_subtype == "INTERIOR REMODEL"
    assert first.address == "2700 SE S ST 200, Bentonville, AR 72712"
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
