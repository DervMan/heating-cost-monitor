# Home temperature comparison (Home Assistant + Docker)

A small web dashboard that graphs 12 months of indoor temperature from your
Home Assistant sensors and compares the year **before** your air-to-air install
with the year **after**.

It reads HA's *long-term statistics* (hourly mean/min/max that the recorder keeps
forever), so it works even though normal history is purged after 10 days.

## Setup

1. **Create a token** in Home Assistant: your profile → *Security* →
   *Long-lived access tokens* → *Create token*.
2. **Find your sensor IDs**: *Developer tools → States*, filter on `temperature`.
   The tado room sensors usually look like `sensor.<room>_temperature`, and
   tado also exposes `sensor.<home>_outdoor_temperature`.
3. **Check they have history**: *Developer tools → Statistics*. Anything listed
   there has long-term data and will appear in the dashboard.
4. Copy the config and fill it in:
   ```bash
   cp .env.example .env
   ```
   - `SENSORS` – the tado rooms you want ticked by default
   - `OUTDOOR_SENSOR` – outdoor temperature, for context (a colder winter skews any comparison)
   - `EXCLUDE=meross` – hides the Meross sensors
   - `INSTALL_DATE` – when the air-to-air goes live; this is the before/after split
5. Start it:
   ```bash
   docker compose up -d --build
   ```
6. Open **http://<docker-host>:8085**

If HA runs on the same machine as Docker, set `HA_URL=http://host.docker.internal:8123`.

## What you get

- **12-month timeline** – daily (or weekly/monthly/hourly) mean per room, a bold
  house average, optional min/max bands, the outdoor temperature, and a marker
  on the install date. CSV download included.
- **Before vs after** – 12 months either side of the install date:
  summary cards (average indoor/outdoor, indoor-outdoor gap, coldest day,
  days below 18 °C), monthly bars with outdoor lines, a year-over-year daily
  overlay, and a monthly table. The "after" year fills in automatically as data
  arrives.

## Energy & cost tab

Tracks electricity and gas use so you can see the real cost difference once the
air-to-air is in.

- **Baseline** – the 12 months before the install date: kWh and cost per fuel,
  plus average daily electricity (the number to watch after install).
- **After vs same days last year** – only days with data in both years are
  compared. Shows kWh and £ per fuel, net saving, and energy per heating
  degree-day (base 15.5 °C) so a colder winter doesn't distort the result.
- Monthly stacked cost, cumulative cost since install, a 7-day-average daily
  overlay, and a monthly table.

Setup: set `ELECTRICITY_SENSORS`, `GAS_SENSORS`, `ELEC_RATE_P` and `GAS_RATE_P`
in `.env`. If the sensors are left blank the tab lists the candidate meters it
finds. Gas meters reporting m³ are converted with `GAS_KWH_PER_M3`. Rates can be
changed live in the tab (remembered in your browser). The same rates apply to
both years, so the comparison reflects usage, not tariff changes. Standing
charges are excluded. Extra endpoints: `/api/energy` and `/api/energy.csv`.

## Notes

- Only sensors with `state_class: measurement` get long-term statistics. tado's
  temperature sensors do. A `climate.*` entity's `current_temperature` attribute does not.
- The "indoor − outdoor gap" is the fairest single comparison, because it
  allows for one winter being colder than the other.
- Endpoints: `/api/config`, `/api/stats?ids=…&start=YYYY-MM-DD&end=YYYY-MM-DD&period=day`,
  `/api/export.csv` (same params).
