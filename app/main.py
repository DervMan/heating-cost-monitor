"""Home Assistant temperature history dashboard.

Pulls long-term statistics (kept indefinitely by HA's recorder) over the
websocket API and serves a small Plotly front end.
"""
import csv
import io
import json
import os
import ssl
import datetime as dt
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, send_from_directory
from websocket import create_connection

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() != "false"
UNIT = os.environ.get("UNIT", "°C")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/London"))


def _csv_env(name):
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


DEFAULT_SENSORS = _csv_env("SENSORS")
OUTDOOR_SENSOR = os.environ.get("OUTDOOR_SENSOR", "").strip()
EXCLUDE = [s.lower() for s in _csv_env("EXCLUDE")]
INSTALL_DATE = os.environ.get("INSTALL_DATE", "").strip()
INSTALL_LABEL = os.environ.get("INSTALL_LABEL", "Air-to-air installed")

WS_URL = HA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

app = Flask(__name__, static_folder="static")


class HAError(Exception):
    pass


class HA:
    """Minimal synchronous Home Assistant websocket client."""

    def __enter__(self):
        sslopt = None if VERIFY_SSL else {"cert_reqs": ssl.CERT_NONE}
        self.ws = create_connection(WS_URL, timeout=60, sslopt=sslopt)
        self._id = 0
        msg = json.loads(self.ws.recv())
        if msg.get("type") != "auth_required":
            raise HAError(f"Unexpected greeting: {msg}")
        self.ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        msg = json.loads(self.ws.recv())
        if msg.get("type") != "auth_ok":
            raise HAError("Authentication failed - check HA_TOKEN")
        return self

    def __exit__(self, *exc):
        self.ws.close()

    def call(self, type_, **payload):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "type": type_, **payload}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise HAError(msg.get("error", {}).get("message", "HA call failed"))
                return msg["result"]


def _to_local(v):
    if isinstance(v, (int, float)):
        secs = v / 1000 if v > 1e11 else v
        d = dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
    else:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d.astimezone(TZ)


def _parse_day(s, default):
    if not s:
        return default
    return dt.datetime.combine(dt.date.fromisoformat(s), dt.time(), TZ)


def fetch_stats(ids, start, end, period="day"):
    with HA() as ha:
        raw = ha.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=ids,
            period=period,
            types=["mean", "min", "max"],
            units={"temperature": UNIT},
        )
    fmt = "%Y-%m-%d %H:%M" if period in ("hour", "5minute") else "%Y-%m-%d"
    out = {}
    for sid, rows in raw.items():
        series = []
        for r in rows:
            if r.get("mean") is None:
                continue
            series.append({
                "t": _to_local(r["start"]).strftime(fmt),
                "mean": round(r["mean"], 2),
                "min": None if r.get("min") is None else round(r["min"], 2),
                "max": None if r.get("max") is None else round(r["max"], 2),
            })
        out[sid] = series
    return out


def _request_range():
    now = dt.datetime.now(TZ)
    end = _parse_day(request.args.get("end"), now)
    start = _parse_day(request.args.get("start"), end - dt.timedelta(days=365))
    ids = [s for s in request.args.get("ids", "").split(",") if s]
    period = request.args.get("period", "day")
    if period not in ("5minute", "hour", "day", "week", "month"):
        period = "day"
    return ids, start, end, period


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    try:
        with HA() as ha:
            stat_ids = ha.call("recorder/list_statistic_ids", statistic_type="mean")
            states = {s["entity_id"]: s for s in ha.call("get_states")}
    except Exception as e:  # surface connection problems in the UI
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    sensors = []
    for s in stat_ids:
        sid = s["statistic_id"]
        unit = s.get("statistics_unit_of_measurement") or s.get("display_unit_of_measurement")
        if s.get("unit_class") != "temperature" and unit not in ("°C", "°F"):
            continue
        if any(x in sid.lower() for x in EXCLUDE):
            continue
        name = (states.get(sid, {}).get("attributes", {}).get("friendly_name")
                or s.get("name") or sid)
        sensors.append({"id": sid, "name": name})
    sensors.sort(key=lambda x: x["name"].lower())

    return jsonify({
        "sensors": sensors,
        "default_sensors": DEFAULT_SENSORS,
        "outdoor_sensor": OUTDOOR_SENSOR,
        "install_date": INSTALL_DATE,
        "install_label": INSTALL_LABEL,
        "unit": UNIT,
    })


@app.get("/api/stats")
def stats():
    ids, start, end, period = _request_range()
    if not ids:
        return jsonify({})
    try:
        return jsonify(fetch_stats(ids, start, end, period))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502


@app.get("/api/export.csv")
def export_csv():
    ids, start, end, period = _request_range()
    data = fetch_stats(ids, start, end, period) if ids else {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sensor", "period_start", "mean", "min", "max"])
    for sid, rows in data.items():
        for r in rows:
            w.writerow([sid, r["t"], r["mean"], r["min"], r["max"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=temperatures.csv"})


@app.get("/healthz")
def healthz():
    return "ok"
