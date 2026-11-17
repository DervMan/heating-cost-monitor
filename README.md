# Heating Cost Tracker

Compares what heating costs you with the gas boiler against the Panasonic air-to-air system, adjusted for the weather, at today's prices. It runs as one Docker container next to Home Assistant.

## Data sources

| Source | What it gives | Notes |
|---|---|---|
| OVO account | Daily electricity and gas kWh, plus OVO's daily unit rates and standing charges | Uses the community `ovoenergy` library (OVO's unofficial API). Backfills from `backfill_from`. |
| Home Assistant | Air-to-air kWh, indoor temperature, and later solar and export | Reads long-term statistics, so it backfills as far as HA has data. |
| CSV import | Anything OVO or HA can't provide | Upload from the dashboard. Half-hourly files are summed into days. |
| Open-Meteo | Daily outdoor temperature for your location | Free, no key needed. |

If more than one source has the same value, the order in `source_priority` decides which one is used.

## Setup

1. Copy `.env.example` to `.env` and add your OVO login and a Home Assistant long-lived token.
2. Copy `config/config.example.yaml` to `config/config.yaml` and edit it. At minimum, set `heating_switch_date`, `location` and current `tariffs`.
3. Run `docker compose up -d --build`, then open `http://<host>:8099`.

The first sync backfills about 15 months, which takes a minute or two. After that it syncs every 6 hours, or when you click **Sync now**.

To show it inside Home Assistant, add a **Webpage** dashboard card pointing at `http://<host>:8099`.

## Panasonic air-to-air metering

Everything depends on knowing the air-to-air's own electricity use. There are two options:

1. **Panasonic Comfort Cloud.** Install the `panasonic_cc` HACS integration and tick *Enable daily energy sensors*. It creates *Daily Heating Energy* and *Daily Cooling Energy* sensors for each unit. Put the heating sensors under `ac` and the cooling sensors under `ac_cooling`. If the sensors have no `state_class` (check in Developer tools > Statistics), use `mode: daily_max`.
2. **CT clamp** (recommended long-term). A Shelly EM or Pro EM on the air-to-air circuit is independent of Panasonic's cloud, and stays accurate once solar and battery are fitted.

Without either, the tool estimates air-to-air use from whole-house electricity minus your gas-era baseline. That estimate is rough, and it stops being reliable once solar arrives.

## How the comparison works

- **Heating degree days (HDD).** Each day's HDD is 15.5°C minus the mean outdoor temperature, or 0 if that's negative. Heating energy scales roughly linearly with HDD.
- **Electricity baseline.** Whole-house electricity in the gas era is fitted the same way (`kWh/day = fixed use + slope × HDD`). That is what the house uses without the air-to-air. After the switch, the dashboard compares actual electricity with what the baseline expects in each day's weather. The difference is the extra electricity (the air-to-air, plus electric hot water if gas is disconnected). It is shown as a card, a chart against degree days, and a monthly column. The monthly heating-cost chart also draws total grid electricity as purple lines (gas-year months dashed, air-to-air months solid, a hollow dot for the month in progress). Those lines use the rate recorded for each day, so they never re-price when your tariff changes; the bars use today's prices. Once solar arrives, whole-house import falls for reasons unrelated to heating, so rely on the air-to-air's own sensor then.
- **Gas era fit.** The gas era is fitted as `gas kWh/day = hot water baseline + slope × HDD`. The baseline is hot water and cooking.
- **Gas disconnected (`keep_supply: false`).** The headline compares everything gas used to do (heating + hot water/cooking + the gas standing charge) with its all-electric replacement (air-to-air + electric shower and undersink heaters). Hot water electricity comes from a `hot_water` sensor if you have one, otherwise it's estimated as last year's gas hot water × `hot_water_electric_factor` (0.85). If the gas were kept, the comparison would cover heating only.
- **Air-to-air era fit.** Fitted the same way on the air-to-air's electricity use.
- **Typical-year cost.** Each slope × the reference year's HDD × today's unit rate. Using one weather year and one set of prices for both systems makes it like-for-like.
- **Effective COP.** Calculated as (gas slope × boiler efficiency) ÷ air-to-air slope: the real-world heat out per kWh in.
- **Since switching.** Actual air-to-air heating cost compared with the gas-era model run on the same days' weather.
- **Solar (next year).** Air-to-air kWh covered by your own solar are costed at the export rate, the income you gave up, rather than the import rate. Add the `solar` and `export` statistics and an export tariff when it's installed.

Before any air-to-air data exists, the dashboard shows an *expected* cost based on `expected_scop`, so you have a forecast from day one.

## Caveats

- OVO's API is unofficial and could change. If OVO sync fails, the sync log will show it. You can import a CSV instead, or point `electricity` and `gas` at Home Assistant sensors (for example a Hildebrand Glow or a meter integration).
- Hot water estimate. Without a meter on the shower circuit, the electric hot water figure is an estimate. A CT clamp on the shower circuit makes it measured; the undersink heaters are small and can stay estimated.
- Comfort changes the result. If the air-to-air keeps the house warmer than the boiler did, it will cost more for that reason alone. Add an `indoor_temp` sensor to see this in the monthly table.
- Treat the fits as reliable only once there are several weeks of cold-weather data in each era.

## Files

```
app/main.py              web server + sync scheduler
app/analysis.py          the comparison maths
app/sources/             ovo.py, homeassistant.py, weather.py, csv_import.py
app/static/index.html    dashboard (no external dependencies)
tests/test_synthetic.py  checks the maths recovers known values: python -m tests.test_synthetic
```
