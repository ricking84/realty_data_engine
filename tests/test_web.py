import json
from datetime import date
from decimal import Decimal

from realty_data_engine.web import load_dataset


class FakeCursor:
    def __init__(self):
        self.rows = []

    def execute(self, sql):
        if "FROM dbo.building_activity_reports" in sql:
            self.rows = [(7, "https://example.test/report.pdf", "September 2026", "monthly")]
        else:
            self.rows = [(
                7, 42, 1, 3, "BP-2026-1", date(2026, 8, 1), date(2026, 9, 2),
                "1 Main St, Bentonville, AR 72712", "RESIDENTIAL", "NEW", "New home",
                "Builder LLC", "Owner LLC", "Addition", "01-12345-678", "ISSUED",
                Decimal("125000.00"), None, None, 2400,
                json.dumps({"OWNER": "Owner LLC"}),
                json.dumps({"address": "1 Main St, Bentonville, AR 72712"}),
                Decimal("36.3450000"), Decimal("-94.1890000"), "located",
            )]
        return self

    def fetchall(self):
        return self.rows


class FakeConnection:
    def cursor(self):
        return FakeCursor()


def test_load_dataset_joins_canonical_permits_to_source_reports():
    dataset = load_dataset(FakeConnection())

    assert dataset["found"] == 1
    report = dataset["reports"][0]
    assert report["category"] == "monthly"
    assert report["title"] == "September 2026"
    assert report["permits"][0]["id"] == 42
    assert report["permits"][0]["permit_number"] == "BP-2026-1"
    assert report["permits"][0]["issue_date"] == "2026-09-02"
    assert report["permits"][0]["valuation"] == "125000.00"
    assert report["permits"][0]["contacts"] == {"OWNER": "Owner LLC"}
    assert report["permits"][0]["raw"] == {"address": "1 Main St, Bentonville, AR 72712"}
    assert report["permits"][0]["latitude"] == "36.3450000"
    assert report["permits"][0]["longitude"] == "-94.1890000"
    assert report["permits"][0]["location_status"] == "located"
