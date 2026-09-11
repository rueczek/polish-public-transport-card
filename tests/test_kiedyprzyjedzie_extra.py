"""Extra tests for kiedyPrzyjedzie carrier coverage."""

import asyncio
import base64
from datetime import datetime
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from mzkzg_transport.const import (
    KIEDYPRZYJEDZIE_BASE_URLS,
    KIEDYPRZYJEDZIE_BYTOW_URL,
    KIEDYPRZYJEDZIE_CZLUCHOW_URL,
    KIEDYPRZYJEDZIE_GRYF_URL,
    KIEDYPRZYJEDZIE_MZK_KEDZIERZYN_URL,
    KIEDYPRZYJEDZIE_MZK_MALBORK_URL,
    KIEDYPRZYJEDZIE_MZK_STAROGARD_URL,
    KIEDYPRZYJEDZIE_NORD_EXPRESS_URL,
    KIEDYPRZYJEDZIE_PKS_GDYNIA_URL,
    KIEDYPRZYJEDZIE_PKS_SLUPSK_URL,
    KIEDYPRZYJEDZIE_PKS_STAROGARD_URL,
    PROVIDER_BYTOW,
    PROVIDER_CZLUCHOW,
    PROVIDER_GRYF,
    PROVIDER_MZK_KEDZIERZYN,
    PROVIDER_MZK_MALBORK,
    PROVIDER_MZK_STAROGARD,
    PROVIDER_NORD_EXPRESS,
    PROVIDER_PKS_GDYNIA,
    PROVIDER_PKS_SLUPSK,
    PROVIDER_PKS_STAROGARD,
)
from mzkzg_transport.coordinator import MzkzgTransportCoordinator


@pytest.fixture(autouse=True)
def patch_ha_frame():
    """Patch HA frame helper."""
    with patch("homeassistant.helpers.frame.report_usage"):
        yield


@pytest.fixture(autouse=True)
def patch_session():
    """Make coordinator._get_session return tracked sessions and close them after each test."""
    sessions = []

    async def _patched_get(self):
        session = aiohttp.ClientSession()
        sessions.append(session)
        return session

    with patch.object(MzkzgTransportCoordinator, "_get_session", _patched_get):
        yield

    for session in sessions:
        if not session.closed:
            asyncio.run(session.close())


@pytest.fixture
def mock_hass():
    """Create a minimal hass mock."""
    hass = MagicMock()
    hass.data = {"mzkzg_transport": {"_coordinators": {}}}
    return hass


@pytest.mark.kiedyprzyjedzie
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        {
            "id": "gryf-live",
            "provider": PROVIDER_GRYF,
            "base_url": KIEDYPRZYJEDZIE_GRYF_URL,
            "stop_id": "1001:2001",
            "response": {
                "timestamp": "now",
                "rows": [
                    {
                        "time": "3 min",
                        "static_time": "5 min",
                        "time_diff": 2,
                        "is_estimated": True,
                        "direction_id": 1,
                        "line_name": "123",
                        "vehicle_attributes": ["ac", "bike_transport", "low_floor"],
                    }
                ],
                "directions": {"1": "Test direction"},
                "station_name": "Test Stop",
            },
            "expected_departures": 1,
            "checks": [
                (0, "route", "123"),
                (0, "headsign", "Test direction"),
                (0, "bike_allowed", True),
                (0, "wheelchair_accessible", True),
                (0, "air_conditioning", True),
                (0, "realtime", True),
            ],
        },
        {
            "id": "czl-schedule",
            "provider": PROVIDER_CZLUCHOW,
            "base_url": KIEDYPRZYJEDZIE_CZLUCHOW_URL,
            "stop_id": "1010:2010",
            "response": {
                "timestamp": "now",
                "rows": [
                    {
                        "time": "23:56",
                        "static_time": "23:56",
                        "time_diff": 0,
                        "is_estimated": False,
                        "direction_id": 2,
                        "line_name": "202",
                        "vehicle_attributes": [],
                    }
                ],
                "directions": {"2": "Tczew"},
                "station_name": "Test Stop",
            },
            "expected_departures": 1,
            "checks": [
                (0, "route", "202"),
                (0, "headsign", "Tczew"),
                (0, "realtime", False),
                (0, "delay_seconds", 0),
            ],
        },
        {
            "id": "nord-empty",
            "provider": PROVIDER_NORD_EXPRESS,
            "base_url": KIEDYPRZYJEDZIE_NORD_EXPRESS_URL,
            "stop_id": "1002:2002",
            "response": {
                "timestamp": "now",
                "rows": [],
                "directions": {},
                "station_name": "Test Stop",
            },
            "expected_departures": 0,
            "checks": [],
        },
        {
            "id": "pks-gdynia-mixed",
            "provider": PROVIDER_PKS_GDYNIA,
            "base_url": KIEDYPRZYJEDZIE_PKS_GDYNIA_URL,
            "stop_id": "1011008:1067285",
            "response": {
                "timestamp": "now",
                "rows": [
                    {
                        "time": "3 min",
                        "static_time": "3 min",
                        "time_diff": 0,
                        "is_estimated": True,
                        "direction_id": 1,
                        "line_name": "660",
                        "vehicle_attributes": ["ac", "bike_transport", "low_floor"],
                    },
                    {
                        "time": "23:58",
                        "static_time": "23:58",
                        "time_diff": 0,
                        "is_estimated": False,
                        "direction_id": 2,
                        "line_name": "660",
                        "vehicle_attributes": [],
                    },
                ],
                "directions": {"1": "Wejherowo", "2": "Gdynia"},
                "station_name": "Banino, Pszenna",
            },
            "expected_departures": 2,
                "checks": [
                (0, "realtime", True),
                (0, "bike_allowed", True),
                (0, "wheelchair_accessible", True),
                (0, "air_conditioning", True),
                (1, "realtime", False),
                (1, "delay_seconds", 0),
            ],
        },
    ],
    ids=lambda case: case["id"],
)
async def test_kiedyprzyjedzie_cases(mock_hass, case):
    """Parametric coverage for kiedyPrzyjedzie payload shapes across carriers."""
    coordinator = MzkzgTransportCoordinator(mock_hass, case["stop_id"], case["provider"], "")
    response = dict(case["response"])
    if response.get("timestamp") == "now":
        response["timestamp"] = int(datetime.now().timestamp())

    with aioresponses() as mocked:
        mocked.get(f"{case['base_url']}/api/departures/{case['stop_id']}", payload=response)
        result = await coordinator._fetch_kiedyprzyjedzie()

    assert result["provider"] == case["provider"]
    assert result["stop_id"] == case["stop_id"]
    assert result["stop_name"] == response["station_name"]
    assert len(result["departures"]) == case["expected_departures"]
    for index, key, expected in case["checks"]:
        assert result["departures"][index][key] == expected


@pytest.mark.kiedyprzyjedzie
def test_kiedyprzyjedzie_extra_urls():
    """All extra kiedyPrzyjedzie base URLs should be registered."""
    expected_hosts = {
        KIEDYPRZYJEDZIE_GRYF_URL: "gryf.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_NORD_EXPRESS_URL: "nordexpress.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_PKS_GDYNIA_URL: "pksgdynia.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_MZK_MALBORK_URL: "malbork.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_PKS_SLUPSK_URL: "pksslupsk.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_MZK_STAROGARD_URL: "starogard.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_PKS_STAROGARD_URL: "pksstarogard.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_BYTOW_URL: "bytow.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_CZLUCHOW_URL: "czluchow.kiedyprzyjedzie.pl",
        KIEDYPRZYJEDZIE_MZK_KEDZIERZYN_URL: "mzkkk.kiedyprzyjedzie.pl",
    }
    for url, host in expected_hosts.items():
        assert url.endswith(host)
    assert set(expected_hosts) <= set(KIEDYPRZYJEDZIE_BASE_URLS.values())


@pytest.mark.kiedyprzyjedzie
@pytest.mark.asyncio
async def test_kiedyprzyjedzie_gps_from_trip_execution(mock_hass):
    """Live trip_execution payload should attach vehicle_lat/lng."""
    from urllib.parse import quote

    stop_id = "1765:4192"
    trip_execution_id = "101:739870:13"
    trip_index = 17
    coordinator = MzkzgTransportCoordinator(
        mock_hass, stop_id, PROVIDER_MZK_KEDZIERZYN, ""
    )
    departures = {
        "timestamp": int(datetime.now().timestamp()),
        "station_name": "1 Maja 1",
        "directions": {"4192000": "Zakłady Azotowe"},
        "rows": [
            {
                "time": "3 min",
                "static_time": "3 min",
                "time_diff": 0,
                "is_estimated": True,
                "direction_id": 4192000,
                "line_name": "1",
                "vehicle_attributes": [],
                "trip_id": 51502249,
                "trip_execution_id": trip_execution_id,
                "trip_index": trip_index,
            }
        ],
    }
    token = quote(base64.b64encode(trip_execution_id.encode("utf-8")).decode("ascii"), safe="")
    gps_url = (
        f"{KIEDYPRZYJEDZIE_MZK_KEDZIERZYN_URL}/api/trip_execution/{token}/{trip_index}"
    )

    with aioresponses() as mocked:
        mocked.get(
            f"{KIEDYPRZYJEDZIE_MZK_KEDZIERZYN_URL}/api/departures/{stop_id}",
            payload=departures,
        )
        mocked.get(
            gps_url,
            payload={"vehicle": {"lat": 50.34403, "lon": 18.20735}, "estimated": True},
        )
        result = await coordinator._fetch_kiedyprzyjedzie()

    assert result["departures"][0]["vehicle_lat"] == 50.34403
    assert result["departures"][0]["vehicle_lng"] == 18.20735
