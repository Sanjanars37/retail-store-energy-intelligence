"""
Re-creates the SAME synthetic dataset as load_energy_data.py (seed 42, identical
random-call order) but writes CSVs instead of needing MySQL. This lets the
analysis run anywhere and gives numbers that match your RSEI database.
All data is SYNTHETIC.
"""
import math, random
import pandas as pd

STORES = [
    (1, "Store 1", "Bengaluru", 12, 14000), (2, "Store 2", "Mumbai", 12, 11000),
    (3, "Store 3", "Hyderabad", 11, 9500),  (4, "Store 4", "Pune", 12, 10000),
    (5, "Store 5", "Chennai", 12, 12500),   (6, "Store 6", "Delhi", 13, 13000),
    (7, "Store 7", "Bengaluru", 11, 8000),  (8, "Store 8", "Kolkata", 12, 9000),
    (9, "Store 9", "Ahmedabad", 11, 8500),  (10, "Store 10", "Kochi", 12, 7500),
]

def build():
    random.seed(42)
    months, y, m = [], 2024, 11
    for _ in range(24):
        months.append(f"{y}-{m:02d}-01")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    noise = lambda p: 1 + random.uniform(-p, p)
    seasonal = lambda s: 1 + 0.15 * math.cos(2 * math.pi * (int(s[5:7]) - 4.5) / 12)
    rows = []
    for sid, _, _, _, area in STORES:
        intensity = random.uniform(13, 18)
        tariff = random.uniform(8.5, 10.5)
        wpm = random.uniform(0.025, 0.035)
        dbase = random.uniform(120, 300)
        for mo in months:
            kwh = area * intensity * seasonal(mo) * noise(0.04)
            water = area * wpm * noise(0.06)
            diesel = dbase * noise(0.20)
            if sid == 3 and mo == "2025-07-01": kwh *= 1.6
            if sid == 5 and mo >= "2025-09-01": kwh *= 1.30
            if sid == 8 and mo == "2025-06-01": diesel *= 8
            if sid == 6 and mo == "2025-10-01": water *= 2.5
            cost = kwh * tariff
            if sid == 7 and mo == "2025-03-01": continue
            if sid == 9 and mo == "2026-01-01": kwh, cost = None, None
            rows.append((sid, mo, kwh and round(kwh, 1), round(water, 1), round(diesel, 1), cost and round(cost, 2)))
            if sid == 2 and mo == "2025-05-01": rows.append(rows[-1])
    stores = pd.DataFrame(STORES, columns=["store_id", "store_name", "city", "opening_hours", "floor_area_m2"])
    util = pd.DataFrame(rows, columns=["store_id", "month", "electricity_kwh", "water_kl", "diesel_l", "electricity_cost_inr"])
    util["month"] = pd.to_datetime(util["month"])
    return stores, util

if __name__ == "__main__":
    s, u = build()
    s.to_csv("data_stores.csv", index=False); u.to_csv("data_utility.csv", index=False)
    print(len(s), "stores,", len(u), "utility rows")
