from realty_data_engine.geocoder import lookup_parcel_locations


class FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "features": [
                {
                    "attributes": {"PARCELID": "01-08559-004", "PH_ADD": "2700 SE S ST"},
                    "geometry": {"rings": [[[-94.20, 36.34], [-94.18, 36.34], [-94.18, 36.36], [-94.20, 36.36]]]},
                },
                {
                    "attributes": {"PARCELID": "01-03126-000-00419-1", "PH_ADD": "4 GREYSTONE"},
                    "geometry": {"x": -94.21, "y": 36.37},
                },
            ]
        }


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse()


def test_lookup_parcel_locations_batches_and_centers_geometry():
    session = FakeSession()
    locations = lookup_parcel_locations(
        ["01-08559-004", "01-03126-000-00419-1", "01-08559-004"],
        session=session,
        batch_size=2,
        batch_delay=0,
    )

    assert len(session.calls) == 1
    assert "01-08559-004" in session.calls[0][1]["where"]
    assert len(locations) == 2
    assert locations["01-08559-004"].latitude == 36.35
    assert locations["01-08559-004"].longitude == -94.19
    assert locations["01-08559-004"].matched_address == "2700 SE S ST"
    assert locations["01-03126-000-00419-1"].latitude == 36.37
    assert locations["01-03126-000-00419-1"].longitude == -94.21


def test_lookup_parcel_locations_marks_unmatched_parcels():
    class EmptyResponse(FakeResponse):
        def json(self):
            return {"features": []}

    class EmptySession(FakeSession):
        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return EmptyResponse()

    result = lookup_parcel_locations(["01-08559-004"], session=EmptySession(), batch_delay=0)

    assert result["01-08559-004"].status == "not_found"
    assert result["01-08559-004"].latitude is None
