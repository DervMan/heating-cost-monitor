"""Read daily totals from Home Assistant's long-term statistics over its WebSocket API.

Long-term statistics are kept forever (unlike the recorder's short-term history), so this
can backfill as far as HA has data. Needs a long-lived access token (HA_TOKEN).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ENERGY_METRICS = {"electricity", "gas", "ac", "ac_cooling", "hot_water", "solar", "export"}
MEAN_METRICS = {"indoor_temp"}


async def _ws_statistics(url: str, token: str, ids: list[str], start: datetime, end: datetime,
                         period: str, types: list[str]) -> dict:
    import aiohttp

    ws_url = url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(ws_url, heartbeat=30, max_msg_size=0) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant auth failed: {msg.get('message', msg.get('type'))}")
            await ws.send_json({
                "id": 1,
                "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": ids,
                "period": period,
                "types": types,
                "units": {"energy": "kWh"},
            })
            while True:
                msg = await ws.receive_json()
                if msg.get("id") == 1:
                    break
            if not msg.get("success"):
                raise RuntimeError(f"statistics query failed: {json.dumps(msg.get('error'))}")
            return msg.get("result") or {}


def _local_date(ts, tz: ZoneInfo) -> str:
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, tz)
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(tz)
    return dt.date().isoformat()


async def sync(cfg: dict, db, start: date) -> str:
    ha = cfg["home_assistant"]
    token = cfg["secrets"]["ha_token"]
    if not token:
        return "skipped: HA_TOKEN not set"
    stats = {m: v for m, v in ha["statistics"].items() if v}
    if not stats:
        return "skipped: no statistics configured"

    tz = ZoneInfo(cfg["timezone"])
    # Full backfill once, then just the last 10 days each run.
    full_start = start
    done_from = db.get_kv("ha_backfilled_from")
    if done_from and done_from <= start.isoformat():   # already backfilled this far; just refresh recent days
        start = max(start, date.today() - timedelta(days=10))
    end_dt = datetime.combine(date.today() + timedelta(days=1), time.min, tz)

    # Group statistic ids by how they must be queried.
    by_mode: dict[tuple, list[str]] = defaultdict(list)
    for metric, entries in stats.items():
        for e in entries:
            if metric in MEAN_METRICS:
                key = ("day", "mean")
            elif e["mode"] == "daily_max":   # sensors that reset at midnight but lack state_class
                key = ("hour", "max")
            else:
                key = ("day", "change")
            by_mode[key].append(e["id"])

    values: dict = defaultdict(lambda: defaultdict(list))   # (date, metric) -> [values]
    chunk = timedelta(days=90)
    cur = datetime.combine(start, time.min, tz)
    while cur < end_dt:
        nxt = min(cur + chunk, end_dt)
        for (period, typ), ids in by_mode.items():
            result = await _ws_statistics(ha["url"], token, sorted(set(ids)), cur, nxt, period, [typ])
            for metric, entries in stats.items():
                for e in entries:
                    if e["id"] not in by_mode[(period, typ)]:
                        continue
                    per_day: dict = defaultdict(list)
                    for row in result.get(e["id"], []):
                        v = row.get(typ)
                        if v is not None:
                            per_day[_local_date(row["start"], tz)].append(float(v))
                    for d, vs in per_day.items():
                        if typ == "max":
                            values[(d, metric)]["sum"].append(max(vs))
                        elif typ == "mean":
                            values[(d, metric)]["mean"].append(sum(vs) / len(vs))
                        else:
                            values[(d, metric)]["sum"].append(sum(vs))
        cur = nxt

    gas_factor = 1.0
    if ha.get("gas_unit", "kWh").lower() in ("m3", "m³"):
        gas_factor = 1.02264 * float(ha.get("gas_calorific_value", 39.5)) / 3.6

    rows = []
    for (d, metric), parts in values.items():
        if metric in MEAN_METRICS:
            v = sum(parts["mean"]) / len(parts["mean"])
        else:
            v = sum(parts["sum"])
            if metric == "gas":
                v *= gas_factor
            if v < 0:   # meter swaps / resets can produce negative changes
                continue
        rows.append((d, "ha", metric, v, None))
    n = db.upsert_readings(rows)
    db.set_kv("ha_backfilled_from", min(full_start.isoformat(), done_from or "9999"))
    return f"ok: {n} daily values"
