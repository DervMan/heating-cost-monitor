"""Import a CSV export (e.g. from your OVO online account) of electricity or gas usage.

Column detection is deliberately forgiving: it looks for a date/time column, a kWh or
consumption column, and optionally a fuel and a cost column. Half-hourly rows are summed
into days.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%d/%m/%y"]


def _parse_date(s: str) -> str | None:
    s = s.strip().strip('"')
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    head = s.split("T")[0].split(" ")[0] if any(c.isdigit() for c in s[:4]) else s
    for cand in (head, s):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _num(s: str) -> float | None:
    s = (s or "").replace("£", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _find(headers, *needles, exclude=()):
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(n in hl for n in needles) and not any(x in hl for x in exclude):
            return i
    return None


def parse(data: bytes, metric: str | None = None):
    """Return list of (date, 'csv', metric, kwh, cost). metric is used when the file has no fuel column."""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise ValueError("empty file")
    headers = [h.strip() for h in rows[0]]
    di = _find(headers, "date", "day", "start", "from", "interval", "time")
    vi = _find(headers, "kwh", "consumption", "usage", "energy", exclude=("cost", "£", "rate", "price"))
    fi = _find(headers, "fuel", "type", "meter")
    ci = _find(headers, "cost", "£", "amount", exclude=("rate",))
    if di is None or vi is None:
        raise ValueError(f"couldn't find date and kWh columns in: {headers}")

    totals: dict = defaultdict(lambda: [0.0, None])
    for r in rows[1:]:
        if len(r) <= max(di, vi):
            continue
        d, v = _parse_date(r[di]), _num(r[vi])
        if d is None or v is None:
            continue
        m = metric
        if fi is not None and fi < len(r):
            f = r[fi].lower()
            m = "gas" if "gas" in f else "electricity" if "elec" in f else metric
        if not m:
            raise ValueError("file has no fuel column: choose electricity or gas when uploading")
        t = totals[(d, m)]
        t[0] += v
        if ci is not None and ci < len(r) and _num(r[ci]) is not None:
            t[1] = (t[1] or 0) + _num(r[ci])
    return [(d, "csv", m, kwh, cost) for (d, m), (kwh, cost) in sorted(totals.items())]
