import io

import pytest
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

HEADER = ["Permit #", "Issue Date", "Address", "Permit Type", "Contractor", "Valuation", "Sq Ft"]
ROWS = [
    ["BP-2024-0001", "01/05/2024", "123 Main Street", "New Residential", "ABC Builders", "$350,000.00", "2,400"],
    ["BP-2024-0002", "01/09/2024", "456 Walton Blvd", "Commercial Remodel", "XYZ Construction", "$1,250,000", "10,000"],
]


def build_pdf(rows: list[list[str]], header: list[str] = HEADER) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter))
    table = Table([header, *rows, ["Total", "", "", "", "", "$1,600,000.00", ""]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    doc.build([table])
    return buffer.getvalue()


@pytest.fixture
def permit_pdf() -> bytes:
    return build_pdf(ROWS)
