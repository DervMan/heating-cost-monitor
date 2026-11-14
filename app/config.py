"""Configuration: config.yaml for settings, environment variables for secrets."""
from __future__ import annotations

import copy
import os
from datetime import date

import yaml

DEFAULTS: dict = {
    "timezone": "Europe/London",
    "heating_switch_date": None,          # first day the air-to-air system heats the house
    "location": {"latitude": None, "longitude": None},
    "hdd_base_c": 15.5,                   # UK standard base temperature for heating degree days
    "cooling_threshold_c": 18.0,          # without a cooling sensor, AC use on days warmer than this counts as cooling
    "boiler_efficiency": 0.85,
    "expected_scop": 3.5,                 # used for the "expected" projection before real AC data exists
    "sync_interval_hours": 6,
    "backfill_from": None,                # defaults to 15 months before today
    "gas": {
        "keep_supply": True,              # false = gas supply capped after switch (standing charge saved)
        "non_heating_kwh_per_day": None,  # hot water/cooking gas; None = estimate from data
        "hot_water_electric_factor": 0.85,  # electric kWh needed per gas kWh of hot water (≈ boiler efficiency)
    },
    "source_priority": {
        "electricity": ["ovo", "csv", "ha"],
        "gas": ["ovo", "csv", "ha"],
        "ac": ["ha", "csv"],
        "ac_cooling": ["ha", "csv"],
        "hot_water": ["ha", "csv"],
        "solar": ["ha", "csv"],
        "export": ["ha", "csv"],
        "indoor_temp": ["ha"],
    },
    "ovo": {"enabled": True, "account_id": None},
    "home_assistant": {
        "enabled": True,
        "url": "http://homeassistant.local:8123",
        "gas_unit": "kWh",                # or "m3"
        "gas_calorific_value": 39.5,      # MJ/m3, only used when gas_unit is m3
        "statistics": {                   # HA statistic ids (usually sensor entity ids)
            "electricity": [],
            "gas": [],
            "ac": [],
            "ac_cooling": [],
            "hot_water": [],
            "solar": [],
            "export": [],
            "indoor_temp": [],
        },
    },
    "tariffs": {                          # £/kWh and £/day; OVO-reported daily rates win when available
        "electricity": [],
        "gas": [],
        "export": [],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _as_date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _norm_stat(entry) -> dict:
    """A statistic entry is either 'sensor.x' or {id: sensor.x, mode: change|daily_max}."""
    if isinstance(entry, str):
        return {"id": entry, "mode": "change"}
    return {"id": entry["id"], "mode": entry.get("mode", "change")}


def load(path: str | None = None) -> dict:
    path = path or os.environ.get("CONFIG_PATH", "/config/config.yaml")
    user = {}
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)
    cfg["heating_switch_date"] = _as_date(cfg["heating_switch_date"])
    cfg["backfill_from"] = _as_date(cfg["backfill_from"])
    for fuel, entries in cfg["tariffs"].items():
        cfg["tariffs"][fuel] = sorted(
            ({**e, "from": _as_date(e["from"])} for e in entries or []), key=lambda e: e["from"]
        )
    stats = cfg["home_assistant"]["statistics"]
    for k, v in list(stats.items()):
        if v is None:
            v = []
        if not isinstance(v, list):
            v = [v]
        stats[k] = [_norm_stat(e) for e in v]
    cfg["secrets"] = {
        "ovo_username": os.environ.get("OVO_USERNAME"),
        "ovo_password": os.environ.get("OVO_PASSWORD"),
        "ha_token": os.environ.get("HA_TOKEN"),
    }
    cfg["db_path"] = os.environ.get("DB_PATH", "/data/tracker.db")
    return cfg
