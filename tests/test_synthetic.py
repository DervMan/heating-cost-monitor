"""Sanity test with synthetic data: run with  python -m tests.test_synthetic  from the project root."""
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from app import analysis, config
from app.db import DB
from app.sources import csv_import


def make(db, switch, today, cop=3.2, non_heat=7.0, gas_slope=2.2, eff=0.85, disconnect=False, sensors=True):
    random.seed(1)
    d = date(2025, 9, 1)
    while d <= today:
        doy = d.timetuple().tm_yday
        t = 10.5 - 7.5 * math.cos(2 * math.pi * (doy - 15) / 365) + random.gauss(0, 2.5)
        hdd = max(0.0, 15.5 - t)
        ds = d.isoformat()
        db.upsert_weather([(ds, t, t - 4, t + 4)])
        base_elec = 8 + 0.08 * hdd + random.gauss(0, 1)
        if d < switch:
            gas = non_heat + gas_slope * hdd + random.gauss(0, 2)
            db.upsert_readings([(ds, "ovo", "gas", max(0, gas), None), (ds, "ovo", "electricity", base_elec, None)])
        else:
            heat = gas_slope * eff * hdd
            ac = heat / cop + 0.3 + random.gauss(0, 0.5)
            ac = max(0, ac)
            rows = [(ds, "ha", "indoor_temp", 20.5, None)]
            if disconnect:
                hw = non_heat * eff + random.gauss(0, 0.8)   # electric shower + undersink heaters
                rows.append((ds, "ovo", "electricity", base_elec + ac + hw, None))
                if sensors:
                    rows += [(ds, "ha", "hot_water", hw, None), (ds, "ha", "ac", ac, None)]
            else:
                gas = non_heat + random.gauss(0, 1)
                rows += [(ds, "ovo", "gas", gas, None), (ds, "ovo", "electricity", base_elec + ac, None),
                         (ds, "ha", "ac", ac, None)]
            db.upsert_readings(rows)
        d += timedelta(days=1)


def main():
    root = Path(__file__).resolve().parent.parent
    cfg = config.load(str(root / "config" / "config.example.yaml"))
    cfg["heating_switch_date"] = date(2026, 9, 1)
    cfg["gas"] = dict(cfg["gas"], keep_supply=True)   # scenario 1: gas kept for hot water
    today = date(2027, 2, 15)
    db = DB(":memory:")
    make(db, cfg["heating_switch_date"], today)
    out = analysis.build(cfg, db, today=today)
    m, p = out["models"], out["projection"]
    print("gas fit", m["gas"], "\nac fit", m["ac"])
    print("projection", json.dumps(p, indent=1))
    print("since", out["since_switch"])
    print("warnings", out["warnings"])
    assert abs(m["gas"]["b"] - 2.2) < 0.15, "gas slope recovered"
    assert abs(m["non_heating_gas_kwh_per_day"] - 7.0) < 1.0, "hot water baseline recovered"
    assert abs(p["effective_cop"] - 3.2) < 0.3, "COP recovered"
    assert len(out["calendar"]) == 12
    # Electricity baseline: synthetic house uses 8 + 0.08 × HDD kWh/day before the switch.
    eb = out["electricity_baseline"]
    assert abs(eb["fixed_kwh_per_day"] - 8.0) < 0.8 and abs(eb["kwh_per_hdd"] - 0.08) < 0.05, eb
    assert eb["weather_adjusted"] and 2800 < eb["annual_kwh"] < 3500, eb
    # Gas kept: whole-house electricity above baseline ≈ the air-to-air's own use.
    assert abs(out["since_switch"]["elec_extra_kwh"] - out["since_switch"]["ac_kwh"]) < 0.1 * out["since_switch"]["ac_kwh"] + 30

    # Pre-switch scenario (today): no AC data, expected projection only.
    cfg2 = dict(cfg, heating_switch_date=date(2026, 12, 1))
    db2 = DB(":memory:")
    make(db2, date(2026, 12, 1), date(2026, 11, 1))
    out2 = analysis.build(cfg2, db2, today=date(2026, 11, 1))
    assert out2["projection"]["ac_expected"] and out2["since_switch"] is None
    print("pre-switch projection:", out2["projection"]["gas"], out2["projection"]["ac_expected"])

    # CSV import
    rows = csv_import.parse(b"Date,Fuel,Consumption (kWh),Cost (\xc2\xa3)\n01/10/2025,Electricity,9.1,2.40\n01/10/2025,Gas,20.5,1.30\n", None)
    assert rows == [("2025-10-01", "csv", "electricity", 9.1, 2.4), ("2025-10-01", "csv", "gas", 20.5, 1.3)], rows
    hh = b"Start,End,kWh\n2025-10-01T00:00:00Z,2025-10-01T00:30:00Z,0.25\n2025-10-01T00:30:00Z,2025-10-01T01:00:00Z,0.5\n"
    assert csv_import.parse(hh, "gas")[0][3] == 0.75
    # Gas disconnected: no gas after the switch, hot water moves to electricity.
    cfg3 = dict(cfg, gas=dict(cfg["gas"], keep_supply=False))
    for with_sensors in (True, False):
        db3 = DB(":memory:")
        make(db3, cfg3["heating_switch_date"], today, disconnect=True, sensors=with_sensors)
        o = analysis.build(cfg3, db3, today=today)
        p3, s3 = o["projection"], o["since_switch"]
        print("disconnected, sensors=%s:" % with_sensors, {k: p3.get(k) for k in ("gas_all_in", "elec_all_in", "effective_cop", "hot_water_elec_kwh_per_day")})
        assert p3["gas_standing_saved"] > 100 and p3["gas_all_in"] > p3["gas"]
        assert abs(p3["effective_cop"] - 3.2) < 0.4, p3["effective_cop"]
        assert abs(p3["hot_water_elec_kwh_per_day"] - 7.0 * 0.85) < 0.6
        assert s3["gas_all_in_equiv"] > s3["gas_equiv_cost"]
    print("ALL OK")

    if len(sys.argv) > 1:   # dump dashboard JSON for a static preview (gas-disconnected scenario)
        out = o
        out["sources"] = [{"source": "ovo", "metric": "gas", "days": 532, "first": "2025-09-01", "last": "2027-02-15"},
                          {"source": "ha", "metric": "ac", "days": 168, "first": "2026-09-01", "last": "2027-02-15"}]
        out["sync_log"] = [{"ts": "2027-02-15T06:00:00", "source": "ovo", "ok": True, "message": "ok: 30 daily readings"}]
        Path(sys.argv[1]).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
