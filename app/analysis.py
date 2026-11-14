"""Turn raw daily readings into a like-for-like heating cost comparison.

The core idea: heating energy rises roughly linearly with heating degree days (HDD).
Fitting  energy/day = baseline + slope × HDD  separately for the gas era and the
air-to-air era gives:
  * slope    → heating energy per degree day (weather-independent)
  * baseline → non-heating use (hot water / cooking for gas; standby for AC)
Multiplying each slope by the same reference year's HDD and today's prices gives a fair
annual comparison, regardless of how cold either winter was or how prices moved.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

MIN_FIT_DAYS = 14


def ols(xs, ys):
    n = len(xs)
    if n < MIN_FIT_DAYS:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-6:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - a - b * x) ** 2 for x, y in zip(xs, ys))
    return {"a": a, "b": b, "n": n, "r2": (1 - ss_res / ss_tot) if ss_tot > 0 else 0.0}


def _norm_rate(unit, standing):
    # OVO sometimes reports pence; config is £. Anything implausibly large is pence.
    if unit is not None and unit > 2:
        unit /= 100
    if standing is not None and standing > 5:
        standing /= 100
    return unit, standing


class Tariffs:
    def __init__(self, cfg_tariffs: dict, db_rates: dict):
        self.cfg = cfg_tariffs
        self.db = db_rates
        self._db_sorted = {f: sorted(v.items()) for f, v in db_rates.items()}

    def get(self, fuel: str, d: date):
        ds = d.isoformat()
        r = self.db.get(fuel, {}).get(ds)
        if r and r[0] is not None:
            u, s = _norm_rate(*r)
            return u, s or 0.0
        entries = self.cfg.get(fuel) or []
        chosen = None
        for e in entries:
            if e["from"] <= d:
                chosen = e
        if chosen is None and entries:
            chosen = entries[0]
        if chosen:
            return float(chosen.get("unit_rate", 0)), float(chosen.get("standing_charge", 0))
        prior = [v for k, v in self._db_sorted.get(fuel, []) if k <= ds and v[0] is not None]
        later = [v for k, v in self._db_sorted.get(fuel, []) if v[0] is not None]
        pick = prior[-1] if prior else (later[0] if later else None)
        if pick:
            u, s = _norm_rate(*pick)
            return u, s or 0.0
        return None, 0.0


def _pick(by_source: dict, prio: list):
    for p in prio:
        v = by_source.get(p)
        if v is not None:
            return v, p
    return None, None


def build(cfg: dict, db, today: date | None = None) -> dict:
    today = today or date.today()
    raw = db.readings()
    weather = db.weather()
    tariffs = Tariffs(cfg["tariffs"], db.rates())
    prio = cfg["source_priority"]
    switch = cfg["heating_switch_date"] or date.max
    base_t = float(cfg["hdd_base_c"])
    cool_t = float(cfg["cooling_threshold_c"])
    eff = float(cfg["boiler_efficiency"])
    scop = float(cfg["expected_scop"])
    keep_gas = bool(cfg["gas"]["keep_supply"])
    hw_factor = float(cfg["gas"].get("hot_water_electric_factor", 0.85))  # electric kWh per gas kWh replaced
    warnings: list[str] = []

    # ---------- assemble one row per day
    days = []
    for ds in sorted(raw):
        m = raw[ds]
        row = {"date": ds, "d": date.fromisoformat(ds)}
        sources = {}
        for metric in ("electricity", "gas", "ac", "ac_cooling", "hot_water", "solar", "export", "indoor_temp"):
            v, src = _pick(m.get(metric, {}), prio.get(metric, ["ha", "ovo", "csv"]))
            row[metric] = v
            if src:
                sources[metric] = src
        if row["electricity"] is None and row["gas"] is None and row["ac"] is None:
            continue
        t = weather.get(ds)
        row["temp"] = t
        row["hdd"] = max(0.0, base_t - t) if t is not None else None
        row["era"] = "gas" if row["d"] < switch else "ac"
        row["sources"] = sources
        days.append(row)

    gas_days = [r for r in days if r["era"] == "gas"]
    ac_days = [r for r in days if r["era"] == "ac"]

    # ---------- gas-era model: gas = hot water baseline + slope × HDD
    pts = [(r["hdd"], r["gas"]) for r in gas_days if r["gas"] is not None and r["hdd"] is not None]
    gas_fit = ols([p[0] for p in pts], [p[1] for p in pts])
    override = cfg["gas"].get("non_heating_kwh_per_day")
    if override is not None:
        non_heat_gas = float(override)
    elif gas_fit:
        non_heat_gas = max(0.0, gas_fit["a"])
    else:
        warm = [r["gas"] for r in gas_days if r["gas"] is not None and r["hdd"] is not None and r["hdd"] < 0.5]
        non_heat_gas = sorted(warm)[len(warm) // 2] if warm else 0.0

    # Electricity model in the gas era — used to estimate AC use if no AC sensor exists.
    pts = [(r["hdd"], r["electricity"]) for r in gas_days if r["electricity"] is not None and r["hdd"] is not None]
    elec_fit = ols([p[0] for p in pts], [p[1] for p in pts])
    have_ac_sensor = any(r["ac"] is not None for r in ac_days)
    # After the gas is disconnected, hot water/cooking moves to electricity.
    hw_measured = [r["hot_water"] for r in ac_days if r["hot_water"] is not None]
    hw_elec_expected = 0.0 if keep_gas else non_heat_gas * hw_factor
    hw_elec_day = (sum(hw_measured) / len(hw_measured)) if len(hw_measured) >= MIN_FIT_DAYS else hw_elec_expected

    # ---------- per-day costs
    now_e, now_es = tariffs.get("electricity", today)
    now_g, now_gs = tariffs.get("gas", today)
    now_x, _ = tariffs.get("export", today)
    now_x = now_x or 0.0
    if now_e is None or now_g is None:
        warnings.append("No tariff found for today. Add tariffs to config.yaml or connect OVO.")
    now_e, now_g = now_e or 0.0, now_g or 0.0

    for r in days:
        eu, es = tariffs.get("electricity", r["d"])
        gu, gs = tariffs.get("gas", r["d"])
        xu, _ = tariffs.get("export", r["d"])
        eu, gu, xu = eu or 0.0, gu or 0.0, xu or 0.0
        gas = r["gas"]
        heat_gas = max(0.0, gas - non_heat_gas) if gas is not None else 0.0

        # Share of the house's electricity that came from the grid (matters once solar arrives).
        grid_frac = 1.0
        if r["solar"] is not None and r["electricity"] is not None:
            used = r["electricity"] + r["solar"] - (r["export"] or 0)
            grid_frac = min(1.0, r["electricity"] / used) if used > 0 else 1.0
        r["grid_frac"] = grid_frac

        r["ac_estimated"] = False
        if r["era"] == "ac" and r["ac"] is None and not have_ac_sensor and elec_fit \
                and r["hdd"] is not None and r["electricity"] is not None:
            hw = r["hot_water"] if r["hot_water"] is not None else hw_elec_expected
            r["ac"] = max(0.0, r["electricity"] - (elec_fit["a"] + elec_fit["b"] * r["hdd"]) - hw)
            r["ac_estimated"] = True

        if r["ac_cooling"] is not None:
            r["ac_heat"] = r["ac"] or 0.0
            r["ac_cool"] = r["ac_cooling"]
        elif r["temp"] is not None and r["temp"] > cool_t:
            r["ac_heat"], r["ac_cool"] = 0.0, (r["ac"] or 0.0)
        else:
            r["ac_heat"], r["ac_cool"] = (r["ac"] or 0.0), 0.0

        if r["era"] == "gas":
            r["heating_cost"] = heat_gas * gu
            r["heating_cost_now"] = heat_gas * now_g
            r["heat_kwh"] = heat_gas * eff                    # useful heat delivered
        else:
            ac_rate = grid_frac * eu + (1 - grid_frac) * xu   # solar-powered kWh cost the export you gave up
            topup = heat_gas * gu if gas is not None else 0.0
            r["heating_cost"] = r["ac_heat"] * ac_rate + topup
            r["heating_cost_now"] = r["ac_heat"] * now_e + heat_gas * now_g
            r["heat_kwh"] = None
            r["gas_equiv_cost"] = (gas_fit["b"] * r["hdd"] * gu) if (gas_fit and r["hdd"] is not None) else None
            if not keep_gas:
                # Everything gas used to do: heating + hot water/cooking + standing charge,
                # vs air-to-air + electric hot water.
                hw_kwh = r["hot_water"] if r["hot_water"] is not None else hw_elec_expected
                r["hw_elec_cost"] = hw_kwh * eu
                r["gas_all_in_equiv"] = (None if r["gas_equiv_cost"] is None
                                         else r["gas_equiv_cost"] + non_heat_gas * gu + gs)

        gas_on = r["era"] == "gas" or keep_gas
        r["total_cost"] = ((r["electricity"] or 0) * eu + es
                           + (((gas or 0) * gu + gs) if gas_on else 0)
                           - (r["export"] or 0) * xu)
        r["heat_gas"] = heat_gas

    # ---------- AC-era model
    pts = [(r["hdd"], r["ac_heat"]) for r in ac_days
           if r["ac"] is not None and r["hdd"] is not None and (r["temp"] is None or r["temp"] <= cool_t)]
    ac_fit = ols([p[0] for p in pts], [p[1] for p in pts])

    # ---------- reference weather year: the 12 months before the switch, else the last 12 months
    def hdd_window(start, end):
        vals = [max(0.0, base_t - t) for ds, t in weather.items() if start.isoformat() <= ds < end.isoformat()]
        return vals

    ref_end = switch if switch != date.max and switch <= today else today
    ref_start = ref_end - timedelta(days=365)
    ref_vals = hdd_window(ref_start, ref_end)
    ref = None
    if len(ref_vals) >= 30:
        scale = 365 / len(ref_vals)
        ref = {
            "start": ref_start.isoformat(), "end": ref_end.isoformat(),
            "hdd": sum(ref_vals) * scale,
            "heating_days": sum(1 for v in ref_vals if v > 0) * scale,
            "coverage_days": len(ref_vals),
        }
        if len(ref_vals) < 330:
            warnings.append(f"Reference weather year only has {len(ref_vals)} days; scaled to 365.")

    # Monthly HDD in the reference year, for weather-adjusting AC months.
    ref_month_hdd: dict = defaultdict(float)
    for ds, t in weather.items():
        if ref_start.isoformat() <= ds < ref_end.isoformat():
            ref_month_hdd[int(ds[5:7])] += max(0.0, base_t - t)

    # ---------- projections at today's prices, reference weather
    proj = {"gas": None, "ac_expected": None, "ac_measured": None, "effective_cop": None,
            "gas_standing_saved": 0.0 if keep_gas else round(now_gs * 365, 2),
            "prices": {"electricity": now_e, "gas": now_g, "export": now_x,
                       "electricity_standing": now_es, "gas_standing": now_gs}}
    if ref and gas_fit and gas_fit["b"] > 0:
        heat_demand = gas_fit["b"] * eff * ref["hdd"]
        proj["gas"] = gas_fit["b"] * ref["hdd"] * now_g
        proj["heat_demand_kwh"] = heat_demand
        proj["ac_expected"] = heat_demand / scop * now_e
    if not keep_gas:
        proj["hot_water_gas"] = non_heat_gas * 365 * now_g
        proj["hot_water_elec"] = hw_elec_day * 365 * now_e
        proj["hot_water_elec_measured"] = len(hw_measured) >= MIN_FIT_DAYS
        proj["hot_water_elec_kwh_per_day"] = hw_elec_day
    if ref and ac_fit and ac_fit["b"] > 0:
        proj["ac_measured"] = (max(0.0, ac_fit["a"]) * ref["heating_days"] + ac_fit["b"] * ref["hdd"]) * now_e
        if gas_fit and gas_fit["b"] > 0:
            proj["effective_cop"] = gas_fit["b"] * eff / ac_fit["b"]

    if not keep_gas and proj["gas"] is not None:
        proj["gas_all_in"] = proj["gas"] + proj["hot_water_gas"] + proj["gas_standing_saved"]
        ac_year = proj["ac_measured"] if proj["ac_measured"] is not None else proj["ac_expected"]
        proj["elec_all_in"] = ac_year + proj["hot_water_elec"]

    # ---------- since the switch: actual AC heating vs what gas would have cost in the same weather
    since = None
    if ac_days:
        valid = [r for r in ac_days if r.get("gas_equiv_cost") is not None]
        since = {
            "days": len(ac_days),
            "ac_heating_cost": sum(r["heating_cost"] for r in ac_days),
            "gas_equiv_cost": sum(r["gas_equiv_cost"] for r in valid) if valid else None,
            "ac_kwh": sum(r["ac_heat"] for r in ac_days),
            "cooling_kwh": sum(r["ac_cool"] for r in ac_days),
            "estimated": any(r["ac_estimated"] for r in ac_days),
            "hdd": sum(r["hdd"] or 0 for r in ac_days),
        }
        if not keep_gas:
            since["hot_water_elec_cost"] = sum(r["hw_elec_cost"] for r in ac_days)
            since["all_in_cost"] = since["ac_heating_cost"] + since["hot_water_elec_cost"]
            eq = [r["gas_all_in_equiv"] for r in ac_days if r.get("gas_all_in_equiv") is not None]
            since["gas_all_in_equiv"] = sum(eq) if eq else None

    # ---------- monthly rollup
    months: dict = {}
    for r in days:
        k = r["date"][:7]
        mo = months.setdefault(k, {"month": k, "days": 0, "gas_days": 0, "hdd": 0.0, "gas_kwh": 0.0,
                                   "elec_kwh": 0.0, "ac_kwh": 0.0, "cool_kwh": 0.0, "solar_kwh": 0.0,
                                   "heating_cost": 0.0, "heating_cost_now": 0.0, "total_cost": 0.0,
                                   "heat_gas_kwh": 0.0, "hw_kwh": 0.0, "indoor": []})
        mo["days"] += 1
        mo["gas_days"] += r["era"] == "gas"
        mo["hdd"] += r["hdd"] or 0
        mo["gas_kwh"] += r["gas"] or 0
        mo["elec_kwh"] += r["electricity"] or 0
        mo["ac_kwh"] += r["ac_heat"]
        mo["cool_kwh"] += r["ac_cool"]
        mo["solar_kwh"] += r["solar"] or 0
        mo["heat_gas_kwh"] += r["heat_gas"]
        mo["hw_kwh"] += r["hot_water"] or 0
        mo["heating_cost"] += r["heating_cost"]
        mo["heating_cost_now"] += r["heating_cost_now"]
        mo["total_cost"] += r["total_cost"]
        if r["indoor_temp"] is not None:
            mo["indoor"].append(r["indoor_temp"])
    monthly = []
    for k in sorted(months):
        mo = months[k]
        mo["era"] = "gas" if mo["gas_days"] * 2 >= mo["days"] else "ac"
        mo["indoor_avg"] = round(sum(mo["indoor"]) / len(mo["indoor"]), 1) if mo["indoor"] else None
        del mo["indoor"]
        mo["cost_per_hdd"] = mo["heating_cost"] / mo["hdd"] if mo["hdd"] > 1 else None
        monthly.append(mo)

    # ---------- calendar-month comparison (reference gas year vs AC months), today's prices
    by_cal: dict = defaultdict(lambda: {"gas_now": None, "ac_expected": None, "ac_adj": None})
    first_full = ref_start if ref_start.day == 1 else (ref_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    first_full_m, last_excl_m = first_full.isoformat()[:7], ref_end.isoformat()[:7]
    for mo in monthly:
        cal = int(mo["month"][5:7])
        in_ref = first_full_m <= mo["month"] < last_excl_m
        if mo["era"] == "gas" and in_ref:
            by_cal[cal]["gas_now"] = mo["heat_gas_kwh"] * now_g
            by_cal[cal]["ac_expected"] = mo["heat_gas_kwh"] * eff / scop * now_e
            by_cal[cal]["month_ref"] = mo["month"]
        elif mo["era"] == "ac" and mo["hdd"] > 1:
            cost_now = mo["ac_kwh"] * now_e
            by_cal[cal]["ac_adj"] = cost_now / mo["hdd"] * ref_month_hdd.get(cal, mo["hdd"])
            by_cal[cal]["month_ac"] = mo["month"]
    order = [((first_full.month - 1 + i) % 12) + 1 for i in range(12)]
    calendar = [{"cal": c, **by_cal[c]} for c in order if c in by_cal]

    # ---------- per-day heating cost for both systems in the same weather, at today's prices.
    # Each day has one actual figure (the system that was running) and one estimate (the other system).
    def ac_estimate(r):
        if ac_fit and ac_fit["b"] > 0:
            return ((max(0.0, ac_fit["a"]) + ac_fit["b"] * r["hdd"]) if r["hdd"] > 0 else 0.0) * now_e
        if gas_fit:
            return r["heat_gas"] * eff / scop * now_e
        return None

    series = []
    for r in days:
        if r["hdd"] is None:
            continue
        if r["era"] == "gas":
            ga, ge, aa, ae = r["heating_cost_now"], None, None, ac_estimate(r)
        else:
            ga, ge = None, (gas_fit["b"] * r["hdd"] * now_g if gas_fit else None)
            aa, ae = r["heating_cost_now"], None
        series.append({"d": r["date"], "t": round(r["temp"], 1), "h": round(r["hdd"], 1),
                       "ga": None if ga is None else round(ga, 3), "ge": None if ge is None else round(ge, 3),
                       "aa": None if aa is None else round(aa, 3), "ae": None if ae is None else round(ae, 3)})

    # ---------- scatter: heating energy per day vs HDD
    scatter = {
        "gas": [[round(r["hdd"], 2), round(r["heat_gas"] * eff, 2), r["date"]] for r in gas_days
                if r["hdd"] is not None and r["gas"] is not None],
        "ac": [[round(r["hdd"], 2), round(r["ac_heat"], 2), r["date"]] for r in ac_days
               if r["hdd"] is not None and r["ac"] is not None and (r["temp"] is None or r["temp"] <= cool_t)],
    }

    if not keep_gas and ac_days and not hw_measured:
        warnings.append("No hot water sensor: electric shower/heater use is assumed to match the old gas hot water "
                        f"use × {hw_factor}. Add a hot_water statistic for a measured figure.")
    if not days:
        warnings.append("No usage data yet. Run a sync or import a CSV.")
    if gas_days and not gas_fit:
        warnings.append("Not enough gas + weather data to model the gas baseline yet (need 14+ days with temperatures).")
    if ac_days and not have_ac_sensor:
        warnings.append("No AC energy sensor: air-to-air use is estimated from whole-house electricity, so treat it as approximate.")
    if cfg["heating_switch_date"] is None:
        warnings.append("Set heating_switch_date in config.yaml so the tool knows when the air-to-air took over.")

    def rnd(x, n=2):
        return None if x is None else round(x, n)

    return {
        "generated": today.isoformat(),
        "switch_date": cfg["heating_switch_date"].isoformat() if cfg["heating_switch_date"] else None,
        "settings": {"hdd_base_c": base_t, "boiler_efficiency": eff, "expected_scop": scop,
                     "keep_gas_supply": keep_gas, "cooling_threshold_c": cool_t,
                     "hot_water_electric_factor": hw_factor},
        "models": {
            "gas": gas_fit, "electricity_gas_era": elec_fit, "ac": ac_fit,
            "non_heating_gas_kwh_per_day": rnd(non_heat_gas),
            "gas_heat_kwh_per_hdd": rnd(gas_fit["b"] * eff, 3) if gas_fit else None,
            "ac_kwh_per_hdd": rnd(ac_fit["b"], 3) if ac_fit else None,
        },
        "reference_year": ref,
        "projection": {k: (rnd(v) if isinstance(v, float) else v) for k, v in proj.items()},
        "since_switch": {k: (rnd(v) if isinstance(v, float) else v) for k, v in since.items()} if since else None,
        "monthly": [{k: (rnd(v) if isinstance(v, float) else v) for k, v in mo.items()} for mo in monthly],
        "calendar": [{k: (rnd(v) if isinstance(v, float) else v) for k, v in c.items()} for c in calendar],
        "scatter": scatter,
        "daily_costs": series,
        "ac_estimate_basis": "measured" if (ac_fit and ac_fit["b"] > 0) else "expected_scop",
        "warnings": warnings,
    }


def daily_rows(db) -> list[dict]:
    """Flat per-day table of every stored value, for CSV export."""
    raw = db.readings()
    weather = db.weather()
    out = []
    for ds in sorted(raw):
        row = {"date": ds, "temp_mean": weather.get(ds)}
        for metric, srcs in raw[ds].items():
            for src, v in srcs.items():
                row[f"{metric}_{src}"] = v
        out.append(row)
    return out
