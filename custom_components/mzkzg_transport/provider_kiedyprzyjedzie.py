"""kiedyPrzyjedzie provider."""

from datetime import datetime, timedelta
import asyncio
import base64
import logging
import re
from urllib.parse import quote

import aiohttp

from homeassistant.util import dt as dt_util

from .const import DOMAIN, KIEDYPRZYJEDZIE_BASE_URLS
from .http_utils import fetch_with_retry

_LOGGER = logging.getLogger(__name__)

# Live GPS comes from per-trip /api/trip_execution — cap requests per refresh.
MAX_GPS_TRIPS = 8
GPS_CACHE_TTL = 20.0
# Stale entries are pruned so the cache does not grow across days of trip ids.
GPS_CACHE_MAX_AGE = 600.0


async def fetch(coord: "MzkzgTransportCoordinator") -> dict:
    """Fetch departures from kiedyPrzyjedzie carriers."""
    session = await coord._get_session()
    base_url = KIEDYPRZYJEDZIE_BASE_URLS[coord.provider]
    now = dt_util.now()

    data = await fetch_with_retry(session, f"{base_url}/api/departures/{coord.stop_id}")

    api_timestamp = data.get("timestamp")
    reference_dt = (
        datetime.fromtimestamp(api_timestamp, tz=dt_util.DEFAULT_TIME_ZONE)
        if isinstance(api_timestamp, (int, float))
        else now
    )
    directions = {
        str(k): str(v)
        for k, v in (data.get("directions") or {}).items()
        if k is not None and v is not None
    }

    if not coord.stop_name:
        coord.stop_name = str(data.get("station_name") or f"Przystanek {coord.stop_id}")

    departures = []
    for row in data.get("rows", []):
        estimated_dt, estimated_realtime = _parse_time(row.get("time"), reference_dt)
        theoretical_dt, _ = _parse_time(row.get("static_time") or row.get("time"), reference_dt)
        if estimated_dt is None:
            continue
        if theoretical_dt is None:
            theoretical_dt = estimated_dt

        time_diff = row.get("time_diff")
        delay_seconds = 0
        if time_diff not in (None, "", 0, 0.0):
            try:
                delay_minutes = int(float(time_diff))
            except (TypeError, ValueError):
                delay_minutes = 0
            if delay_minutes:
                delay_seconds = delay_minutes * 60
            estimated_dt = theoretical_dt + timedelta(seconds=delay_seconds)
        else:
            delay_seconds = int((estimated_dt - theoretical_dt).total_seconds())

        if estimated_dt < now - timedelta(minutes=1):
            continue

        direction_id = row.get("direction_id")
        direction = directions.get(str(direction_id)) or "—"
        vehicle_attributes = [str(attr) for attr in row.get("vehicle_attributes", []) if attr]
        realtime = bool(row.get("is_estimated")) or estimated_realtime or delay_seconds != 0

        departures.append({
            "route": str(row.get("line_name") or "?"),
            "headsign": direction,
            "estimated_time": estimated_dt.isoformat(),
            "theoretical_time": theoretical_dt.isoformat(),
            "delay_seconds": delay_seconds,
            "realtime": realtime,
            "vehicle_type": "bus",
            "bike_allowed": "bike_transport" in vehicle_attributes,
            "wheelchair_accessible": "low_floor" in vehicle_attributes or "wheelchair" in vehicle_attributes,
            "air_conditioning": "ac" in vehicle_attributes,
            "ticket_machine": "ticket_machine" in vehicle_attributes,
            "vehicle_attributes": vehicle_attributes,
            "platform": row.get("platform"),
            "trip_id": row.get("trip_id"),
            "trip_execution_id": row.get("trip_execution_id"),
            "trip_index": row.get("trip_index"),
            "cancelled": row.get("canceled", False),
            "provider": coord.provider,
        })

    departures.sort(key=lambda x: x.get("estimated_time") or "")
    await _enrich_gps(coord, session, base_url, departures)
    return {
        "stop_id": coord.stop_id,
        "stop_name": coord.stop_name,
        "provider": coord.provider,
        "departures": departures,
        "last_update": now.isoformat(),
    }


def _parse_time(value, reference_dt: datetime) -> tuple[datetime | None, bool]:
    """Parse kiedyPrzyjedzie time strings into absolute datetimes."""
    if value is None:
        return None, False

    text = str(value).strip()
    if not text:
        return None, False

    relative_match = re.match(r"^(\d+)\s*min(?:\.|utes?)?$", text, re.IGNORECASE)
    if relative_match:
        minutes = int(relative_match.group(1))
        return reference_dt + timedelta(minutes=minutes), True

    clock_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if clock_match:
        hour = int(clock_match.group(1))
        minute = int(clock_match.group(2))
        second = int(clock_match.group(3) or 0)
        day_add = 0
        if hour >= 24:
            hour -= 24
            day_add = 1
        dep_dt = reference_dt.replace(hour=hour, minute=minute, second=second, microsecond=0) + timedelta(days=day_add)
        if (dep_dt - reference_dt).total_seconds() < -3600:
            dep_dt += timedelta(days=1)
        return dep_dt, False

    return None, False


def _execution_token(trip_execution_id: str) -> str:
    """Encode trip_execution_id the same way the official frontend does (base64)."""
    return base64.b64encode(str(trip_execution_id).encode("utf-8")).decode("ascii")


async def _fetch_trip_execution(session: aiohttp.ClientSession, url: str) -> dict | None:
    """One-shot GET — do not retry 400/401/404 as missing GPS."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        _LOGGER.debug("kiedyPrzyjedzie GPS fetch failed for %s: %s", url, err)
        return None


async def _enrich_gps(coord, session: aiohttp.ClientSession, base_url: str, departures: list) -> None:
    """Attach vehicle_lat/lng from trip_execution for live estimated trips."""
    cache = coord.hass.data.setdefault(DOMAIN, {}).setdefault("_kp_gps", {})
    now_ts = dt_util.now().timestamp()
    for key in [k for k, v in cache.items() if now_ts - v.get("_ts", 0) > GPS_CACHE_MAX_AGE]:
        cache.pop(key, None)

    candidates = [
        dep
        for dep in departures
        if dep.get("realtime")
        and dep.get("trip_execution_id") not in (None, "")
        and dep.get("trip_index") is not None
        and not dep.get("cancelled")
    ][:MAX_GPS_TRIPS]

    async def _one(dep: dict) -> None:
        cache_key = f"{base_url}:{dep['trip_execution_id']}"
        cached = cache.get(cache_key)
        if cached and now_ts - cached.get("_ts", 0) < GPS_CACHE_TTL:
            pos = cached.get("pos")
            if pos:
                dep["vehicle_lat"] = pos["lat"]
                dep["vehicle_lng"] = pos["lng"]
            return

        token = quote(_execution_token(dep["trip_execution_id"]), safe="")
        url = f"{base_url}/api/trip_execution/{token}/{dep['trip_index']}"
        data = await _fetch_trip_execution(session, url)
        vehicle = (data or {}).get("vehicle") or {}
        try:
            lat = float(vehicle["lat"])
            lon = float(vehicle["lon"])
        except (KeyError, TypeError, ValueError):
            cache[cache_key] = {"pos": None, "_ts": now_ts}
            return

        pos = {"lat": lat, "lng": lon}
        cache[cache_key] = {"pos": pos, "_ts": now_ts}
        dep["vehicle_lat"] = lat
        dep["vehicle_lng"] = lon

    if candidates:
        await asyncio.gather(*(_one(dep) for dep in candidates))
