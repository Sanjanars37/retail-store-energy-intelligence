"""
Load synthetic retail-store energy data into MySQL.

Run order:
  1. Create the 4 tables first (stores, utility_data, emission_factors, savings_measures).
  2. pip install mysql-connector-python
  3. Set your password, then run:
       Windows (cmd):   set RSEI_PASSWORD=your_password && python load_energy_data.py
       PowerShell:      $env:RSEI_PASSWORD='your_password'; python load_energy_data.py

The data is SYNTHETIC. State this clearly in your README.
Anomalies are injected on purpose so your SQL / Python checks have something to find.
The "answer key" at the bottom is printed so you can verify your detection works.
"""

import math
import os
import random
import mysql.connector

DB_CONFIG = {
    "host": os.getenv("RSEI_HOST", "localhost"),
    "user": os.getenv("RSEI_USER", "root"),
    "password": os.environ["RSEI_PASSWORD"],   # set it in your shell - never hard-code a password in a repo
    "database": os.getenv("RSEI_DB", "RSEI"),
}

random.seed(42)  # same data every run

# ---------------------------------------------------------------- stores
# (store_id, name, city, opening_hours, floor_area_m2)
STORES = [
    (1, "Store 1",  "Bengaluru", 12, 14000),
    (2, "Store 2",  "Mumbai",    12, 11000),
    (3, "Store 3",  "Hyderabad", 11,  9500),
    (4, "Store 4",  "Pune",      12, 10000),
    (5, "Store 5",  "Chennai",   12, 12500),
    (6, "Store 6",  "Delhi",     13, 13000),
    (7, "Store 7",  "Bengaluru", 11,  8000),
    (8, "Store 8",  "Kolkata",   12,  9000),
    (9, "Store 9",  "Ahmedabad", 11,  8500),
    (10, "Store 10", "Kochi",    12,  7500),
]

# 24 months: Nov 2024 .. Oct 2026 (always the 1st of the month)
MONTHS = []
y, m = 2024, 11
for _ in range(24):
    MONTHS.append(f"{y}-{m:02d}-01")
    m += 1
    if m == 13:
        y, m = y + 1, 1


def noise(pct):
    """Random multiplier, e.g. noise(0.04) -> between 0.96 and 1.04"""
    return 1 + random.uniform(-pct, pct)


def seasonal(month_str):
    """Cooling demand peaks around April-May (simple cosine curve)."""
    mo = int(month_str[5:7])
    return 1 + 0.15 * math.cos(2 * math.pi * (mo - 4.5) / 12)


# ---------------------------------------------------------- utility_data
rows = []  # (store_id, month, kwh, water_kl, diesel_l, cost_inr)

for sid, name, city, hours, area in STORES:
    intensity = random.uniform(13, 18)   # kWh per m2 per month (store's own level)
    tariff = random.uniform(8.5, 10.5)   # INR per kWh
    water_per_m2 = random.uniform(0.025, 0.035)
    diesel_base = random.uniform(120, 300)

    for mo in MONTHS:
        kwh = area * intensity * seasonal(mo) * noise(0.04)
        water = area * water_per_m2 * noise(0.06)
        diesel = diesel_base * noise(0.20)

        # ---- injected anomalies ----
        if sid == 3 and mo == "2025-07-01":        # one-off electricity spike
            kwh *= 1.6
        if sid == 5 and mo >= "2025-09-01":        # permanent baseload jump
            kwh *= 1.30
        if sid == 8 and mo == "2025-06-01":        # diesel surge (power cut)
            diesel *= 8
        if sid == 6 and mo == "2025-10-01":        # water leak
            water *= 2.5

        cost = kwh * tariff

        # missing month: store 7, March 2025 -> row not inserted at all
        if sid == 7 and mo == "2025-03-01":
            continue

        # missing reading: store 9, Jan 2026 -> electricity not recorded
        if sid == 9 and mo == "2026-01-01":
            kwh, cost = None, None

        rows.append((sid, mo, kwh and round(kwh, 1), round(water, 1),
                     round(diesel, 1), cost and round(cost, 2)))

        # duplicate bill: store 2, May 2025 -> same row inserted twice
        if sid == 2 and mo == "2025-05-01":
            rows.append(rows[-1])

# -------------------------------------------------------------- load it
conn = mysql.connector.connect(**DB_CONFIG)
cur = conn.cursor()

# clear old data (child table first because of the foreign key)
cur.execute("DELETE FROM utility_data")
cur.execute("DELETE FROM stores")
cur.execute("DELETE FROM emission_factors")
cur.execute("DELETE FROM savings_measures")

cur.executemany(
    "INSERT INTO stores (store_id, store_name, city, opening_hours, floor_area_m2) "
    "VALUES (%s, %s, %s, %s, %s)",
    [(sid, n, c, h, a) for sid, n, c, h, a in STORES],
)

cur.executemany(
    "INSERT INTO utility_data "
    "(store_id, month, electricity_kwh, water_kl, diesel_l, electricity_cost_inr) "
    "VALUES (%s, %s, %s, %s, %s, %s)",
    rows,
)

# Placeholder assumptions - REPLACE with sourced values and update 'reference'
cur.executemany(
    "INSERT INTO emission_factors (source, factor, unit, reference) VALUES (%s, %s, %s, %s)",
    [
        ("electricity", 0.727, "kgCO2e/kWh", "CEA CO2 Baseline Database v20.0, FY2023-24 weighted average (Table S)"),
        ("diesel", 2.68, "kgCO2e/L", "GHG Protocol / IPCC default - verify source"),
    ],
)

cur.executemany(
    "INSERT INTO savings_measures "
    "(measure_name, applies_to, savings_pct, capex_inr_per_m2, assumption_note) "
    "VALUES (%s, %s, %s, %s, %s)",
    [
        ("LED retrofit", "electricity", 12, 400, "Placeholder - replace with sourced estimate"),
        ("HVAC scheduling", "electricity", 8, 150, "Placeholder - replace with sourced estimate"),
        ("Rooftop solar", "electricity", 20, 3000, "Placeholder - replace with sourced estimate"),
    ],
)

conn.commit()
cur.close()
conn.close()

print(f"Loaded {len(STORES)} stores and {len(rows)} utility rows.\n")
print("ANSWER KEY (injected problems - your checks should find these):")
print(" - Store 3: electricity spike in 2025-07")
print(" - Store 5: ~30% electricity baseload jump from 2025-09 onward")
print(" - Store 8: diesel surge in 2025-06")
print(" - Store 6: water spike in 2025-10")
print(" - Store 7: missing month 2025-03 (no row)")
print(" - Store 9: electricity/cost NULL in 2026-01")
print(" - Store 2: duplicate bill row for 2025-05")