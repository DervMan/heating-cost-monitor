"""Daily outdoor temperatures from Open-Meteo (free, no API key)."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import date, timedelta

DAILY = "temperature_2m_mean,temperature_2m_min,temperature_2m_max"


def _get(url: str, params: dict) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=30) as r:
        return json.loads(r.read())


def _rows(payload: dict):
    d = payload.get("daily") or {}
    for i, day in enumerate(d.get("time", [])):
        mean = d["temperature_2m_mean"][i]
        if mean is None:
            continue
        yield (day, mean, d["temperature_2m_min"][i], d["temperature_2m_max"][i])


async def sync(cfg: dict, db, start: date) -> str:
    lat, lon = cfg["location"]["latitude"], cfg["location"]["longitude"]
    if lat is None or lon is None:
        return "skipped: location not set"
    base = {"latitude": lat, "longitude": lon, "daily": DAILY, "timezone": cfg["timezone"]}
    today = date.today()
    full_start = start
    done_from = db.get_kv("weather_backfilled_from")
    if done_from and done_from <= start.isoformat():   # already backfilled this far; just refresh recent days
        start = max(start, today - timedelta(days=14))
    n = 0
    archive_end = today - timedelta(days=6)   # archive lags a few days
    if start <= archive_end:
        p = await asyncio.to_thread(_get, "https://archive-api.open-meteo.com/v1/archive",
                                    {**base, "start_date": start.isoformat(), "end_date": archive_end.isoformat()})
        n += db.upsert_weather(_rows(p))
    p = await asyncio.to_thread(_get, "https://api.open-meteo.com/v1/forecast",
                                {**base, "past_days": 14, "forecast_days": 1})
    n += db.upsert_weather(r for r in _rows(p) if r[0] <= today.isoformat())
    db.set_kv("weather_backfilled_from", min(full_start.isoformat(), done_from or "9999"))
    return f"ok: {n} days"
