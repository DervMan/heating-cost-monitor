"""Pull daily usage, costs and rates from your OVO account.

Uses the community `ovoenergy` library (the same one behind Home Assistant's OVO
integration). It talks to OVO's unofficial API, so it can break if OVO changes things;
the CSV import and Home Assistant sources are the fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


async def sync(cfg: dict, db, start: date) -> str:
    import aiohttp
    from ovoenergy import OVOEnergy

    user, pw = cfg["secrets"]["ovo_username"], cfg["secrets"]["ovo_password"]
    if not user or not pw:
        return "skipped: OVO_USERNAME / OVO_PASSWORD not set"

    today = date.today()
    # Months older than ~5 weeks are treated as final and not fetched again.
    final_before = (today - timedelta(days=35)).strftime("%Y-%m")
    done = set(json.loads(db.get_kv("ovo_months_done", "[]")))
    n_rows = 0

    async with aiohttp.ClientSession() as session:
        client = OVOEnergy(session)
        if not await client.authenticate(user, pw):
            raise RuntimeError("OVO login failed (check username/password)")
        try:
            await client.bootstrap_accounts()
        except Exception as e:  # account ids usually also come from the token
            log.warning("OVO bootstrap failed, using token account ids: %s", e)
        if cfg["ovo"].get("account_id"):
            client.custom_account_id = int(cfg["ovo"]["account_id"])

        for month in _months(start, today):
            if month in done:
                continue
            usage = await client.get_daily_usage(month)
            readings, rates = [], []
            for fuel, items in (("electricity", usage.electricity), ("gas", usage.gas)):
                for u in items or []:
                    if u is None or u.interval is None or u.consumption is None:
                        continue
                    d = u.interval.start.date().isoformat()
                    cost = float(u.cost.amount) if u.cost and u.cost.amount is not None else None
                    readings.append((d, "ovo", fuel, float(u.consumption), cost))
                    if u.rates and u.rates.anytime is not None:
                        rates.append((d, fuel, float(u.rates.anytime),
                                      float(u.rates.standing) if u.rates.standing is not None else None, "ovo"))
            n_rows += db.upsert_readings(readings)
            db.upsert_rates(rates)
            if month < final_before and readings:
                done.add(month)
            await asyncio.sleep(0.5)  # be gentle with OVO's API

    db.set_kv("ovo_months_done", json.dumps(sorted(done)))
    return f"ok: {n_rows} daily readings"
