"""Web server + background sync loop."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, timedelta
from pathlib import Path

from aiohttp import web

from . import analysis, config
from .db import DB
from .sources import csv_import, homeassistant, ovo, weather

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tracker")
STATIC = Path(__file__).parent / "static"


class Tracker:
    def __init__(self):
        self.cfg = config.load()
        self.db = DB(self.cfg["db_path"])
        self.lock = asyncio.Lock()

    def backfill_start(self) -> date:
        return self.cfg["backfill_from"] or (date.today() - timedelta(days=460))

    async def sync(self) -> list[dict]:
        if self.lock.locked():
            return [{"source": "all", "ok": False, "message": "sync already running"}]
        async with self.lock:
            self.cfg = config.load()   # pick up config edits without a restart
            start = self.backfill_start()
            results = []
            jobs = [("weather", weather.sync, True),
                    ("ovo", ovo.sync, self.cfg["ovo"]["enabled"]),
                    ("home_assistant", homeassistant.sync, self.cfg["home_assistant"]["enabled"])]
            for name, fn, enabled in jobs:
                if not enabled:
                    continue
                try:
                    msg = await fn(self.cfg, self.db, start)
                    ok = True
                except Exception as e:  # keep other sources running
                    log.exception("%s sync failed", name)
                    msg, ok = f"error: {e}", False
                self.db.log(name, ok, msg)
                results.append({"source": name, "ok": ok, "message": msg})
                log.info("%s: %s", name, msg)
            return results

    async def loop(self):
        while True:
            await self.sync()
            await asyncio.sleep(float(self.cfg["sync_interval_hours"]) * 3600)


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(STATIC / "index.html")


@routes.get("/favicon.ico")
@routes.get("/favicon-32.png")
@routes.get("/apple-touch-icon.png")
async def icons(request):
    return web.FileResponse(STATIC / request.path.lstrip("/"), headers={"Cache-Control": "public, max-age=86400"})


@routes.get("/api/dashboard")
async def dashboard(request):
    t: Tracker = request.app["tracker"]
    data = analysis.build(t.cfg, t.db)
    data["sources"] = t.db.source_summary()
    data["sync_log"] = t.db.recent_log(12)
    return web.json_response(data)


@routes.post("/api/sync")
async def sync_now(request):
    return web.json_response(await request.app["tracker"].sync())


@routes.post("/api/import")
async def import_csv(request):
    t: Tracker = request.app["tracker"]
    reader = await request.multipart()
    data, metric = None, None
    async for part in reader:
        if part.name == "file":
            data = await part.read()
        elif part.name == "metric":
            metric = (await part.text()).strip() or None
    if not data:
        return web.json_response({"ok": False, "message": "no file"}, status=400)
    try:
        rows = csv_import.parse(data, metric)
    except ValueError as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)
    n = t.db.upsert_readings(rows)
    msg = f"imported {n} days" + (f" ({rows[0][0]} to {rows[-1][0]})" if rows else "")
    t.db.log("csv", True, msg)
    return web.json_response({"ok": True, "message": msg})


@routes.get("/api/daily.csv")
async def export_csv(request):
    rows = analysis.daily_rows(request.app["tracker"].db)
    cols = sorted({k for r in rows for k in r}, key=lambda c: (c != "date", c != "temp_mean", c))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return web.Response(text=buf.getvalue(), content_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=heating-daily.csv"})


@routes.get("/health")
async def health(request):
    return web.json_response({"ok": True})


async def _start(app):
    app["task"] = asyncio.create_task(app["tracker"].loop())


async def _stop(app):
    app["task"].cancel()


def make_app() -> web.Application:
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app["tracker"] = Tracker()
    app.add_routes(routes)
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=8080)
