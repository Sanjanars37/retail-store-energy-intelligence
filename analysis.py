#!/usr/bin/env python3
"""
RSEI - Retail Store Energy Intelligence
=======================================
Pipeline: load -> validate -> clean -> detect anomalies -> EUI & carbon -> savings/ROI.

Data source
    python analysis.py                 # synthetic CSVs (identical to load_energy_data.py output)
    RSEI_PASSWORD=... python analysis.py --source mysql   # read the RSEI database instead

All data is SYNTHETIC. Emission factors and savings assumptions are PLACEHOLDERS
(see ASSUMPTIONS below) and must be replaced with sourced values before you quote them.
"""
import argparse
import json
import os

import numpy as np
import pandas as pd

OUT = "outputs"

# ----------------------------------------------------------------- ASSUMPTIONS
# Everything a reviewer may want to change lives here.
KPI_WINDOW = ("2025-10-01", "2026-09-01")   # 12 complete months used for annual KPIs
DIESEL_PRICE_INR_PER_L = 90.0               # ASSUMPTION: only used to price diesel anomalies
DEFAULT_EF = {"electricity": 0.727, "diesel": 2.68}   # kgCO2e per kWh (CEA v20.0, FY2023-24) / per litre (diesel: GHG Protocol default)
DEFAULT_MEASURES = pd.DataFrame(
    [
        ("LED retrofit", "electricity", 12.0, 400.0, "Placeholder - replace with sourced estimate"),
        ("HVAC scheduling", "electricity", 8.0, 150.0, "Placeholder - replace with sourced estimate"),
        ("Rooftop solar", "electricity", 20.0, 3000.0, "Placeholder - replace with sourced estimate"),
    ],
    columns=["measure_name", "applies_to", "savings_pct", "capex_inr_per_m2", "assumption_note"],
)
# scenario: (multiplier on savings %, multiplier on capex)
SCENARIOS = {"conservative": (0.7, 1.3), "expected": (1.0, 1.0), "optimistic": (1.2, 0.9)}

# detection thresholds
SHIFT_MIN_PCT = 0.10      # a sustained level change must be >= 10%
SHIFT_MIN_GAIN = 0.75     # ...and cut the L1 error to <75% of a single-level fit
SHIFT_NOISE_MULT = 3.0    # ...and be at least 3x the month-to-month noise (avoids fitting noise)
SPIKE_MIN_Z = 4.0         # robust z-score (MAD based)
SPIKE_MIN_PCT = 0.15      # ...and at least 15% off the fitted level

METRICS = {  # column -> (unit, scale by floor area?)
    "electricity_kwh": ("kWh", True),
    "water_kl": ("kL", True),
    "diesel_l": ("L", False),
}


# ------------------------------------------------------------------- loading
def _fetch(cur, sql):
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def load(source):
    if source == "mysql":
        import mysql.connector

        conn = mysql.connector.connect(
            host=os.getenv("RSEI_HOST", "localhost"),
            user=os.getenv("RSEI_USER", "root"),
            password=os.environ["RSEI_PASSWORD"],
            database=os.getenv("RSEI_DB", "RSEI"),
        )
        cur = conn.cursor()
        stores = _fetch(cur, "SELECT store_id, store_name, city, opening_hours, floor_area_m2 FROM stores")
        util = _fetch(cur, "SELECT store_id, month, electricity_kwh, water_kl, diesel_l, "
                           "electricity_cost_inr FROM utility_data")
        ef = _fetch(cur, "SELECT source, factor FROM emission_factors")
        meas = _fetch(cur, "SELECT measure_name, applies_to, savings_pct, capex_inr_per_m2, assumption_note "
                           "FROM savings_measures")
        conn.close()
        util["month"] = pd.to_datetime(util["month"])
        for c in ["electricity_kwh", "water_kl", "diesel_l", "electricity_cost_inr"]:
            util[c] = util[c].astype(float)
        stores["floor_area_m2"] = stores["floor_area_m2"].astype(float)
        efd = dict(zip(ef["source"], ef["factor"].astype(float))) or DEFAULT_EF
        meas[["savings_pct", "capex_inr_per_m2"]] = meas[["savings_pct", "capex_inr_per_m2"]].astype(float)
        return stores, util, efd, meas
    # synthetic CSVs
    if not (os.path.exists("data_stores.csv") and os.path.exists("data_utility.csv")):
        import generate_data
        s, u = generate_data.build()
        s.to_csv("data_stores.csv", index=False)
        u.to_csv("data_utility.csv", index=False)
    stores = pd.read_csv("data_stores.csv")
    util = pd.read_csv("data_utility.csv", parse_dates=["month"])
    return stores, util, dict(DEFAULT_EF), DEFAULT_MEASURES.copy()


# ---------------------------------------------------------------- validation
def quality_report(util, stores):
    names = stores.set_index("store_id")["store_name"]
    months = pd.date_range(util["month"].min(), util["month"].max(), freq="MS")
    rows = []

    def add(sid, month, issue, detail):
        rows.append({"store_id": sid, "store_name": names[sid],
                     "month": None if month is None else pd.Timestamp(month).strftime("%Y-%m"),
                     "issue": issue, "detail": detail})

    for sid in stores["store_id"]:
        u = util[util["store_id"] == sid]
        for m, g in u[u.duplicated("month", keep=False)].groupby("month"):
            same = len(g.drop_duplicates()) == 1
            add(sid, m, "Duplicate bill", f"{len(g)} rows, " + ("identical (safe to drop)" if same else "CONFLICTING values"))
        for m in months.difference(pd.DatetimeIndex(u["month"])):
            add(sid, m, "Missing month", "no row for this store/month")
        uu = u.drop_duplicates("month")
        for _, r in uu.iterrows():
            for col in ["electricity_kwh", "water_kl", "diesel_l", "electricity_cost_inr"]:
                if pd.isna(r[col]):
                    add(sid, r["month"], "Missing reading (NULL)", col)
                elif r[col] < 0:
                    add(sid, r["month"], "Negative value", f"{col} = {r[col]}")
                elif r[col] == 0:
                    add(sid, r["month"], "Zero value", col)
        tariff = (uu["electricity_cost_inr"] / uu["electricity_kwh"]).replace([np.inf, -np.inf], np.nan)
        med = tariff.median()
        for i in tariff.index[(tariff - med).abs() > 0.15 * med]:
            add(sid, uu.loc[i, "month"], "Tariff mismatch", f"implied INR/kWh {tariff[i]:.2f} vs median {med:.2f}")
    cols = ["store_id", "store_name", "month", "issue", "detail"]
    return pd.DataFrame(rows, columns=cols).sort_values(["store_id", "month", "issue"]).reset_index(drop=True)


def clean(util, stores):
    months = pd.date_range(util["month"].min(), util["month"].max(), freq="MS")
    u = util.drop_duplicates(["store_id", "month"], keep="first")
    grid = pd.MultiIndex.from_product([stores["store_id"], months], names=["store_id", "month"]).to_frame(index=False)
    return grid.merge(u, how="left", on=["store_id", "month"]).sort_values(["store_id", "month"]).reset_index(drop=True)


# ----------------------------------------------------------------- detection
def peer_factor(inten):
    """Common month-to-month factor (weather/season) seen by each store's PEERS.
    Each peer is first divided by its own median level, then the median across peers is taken,
    so differences in store size/intensity cancel and only the shared seasonal pattern remains."""
    norm = inten / inten.median()
    out = pd.DataFrame(index=inten.index, columns=inten.columns, dtype=float)
    for c in inten.columns:
        out[c] = norm.drop(columns=c).median(axis=1, skipna=True)
    return out


def _l1_split(y, min_seg=3):
    base = np.abs(y - np.median(y)).sum()
    best_cost, best_k = base, None
    for k in range(min_seg, len(y) - min_seg + 1):
        cost = np.abs(y[:k] - np.median(y[:k])).sum() + np.abs(y[k:] - np.median(y[k:])).sum()
        if cost < best_cost:
            best_cost, best_k = cost, k
    return base, best_cost, best_k


def detect(y):
    """y: 1-D array (NaN allowed). Returns baseline, fitted level per month, z, flags, shift position."""
    ok = ~np.isnan(y)
    pos = np.where(ok)[0]
    yy = y[pos]
    base_cost, cost, k = _l1_split(yy)
    shift_pos = None
    baseline = float(np.median(yy))
    after = baseline
    if k is not None:
        m1, m2 = float(np.median(yy[:k])), float(np.median(yy[k:]))
        fit = np.where(np.arange(len(yy)) < k, m1, m2)
        r = yy / fit - 1
        noise = max(1.4826 * np.median(np.abs(r - np.median(r))), 0.01)   # month-to-month noise
        if (abs(m2 / m1 - 1) >= max(SHIFT_MIN_PCT, SHIFT_NOISE_MULT * noise)
                and cost < SHIFT_MIN_GAIN * base_cost):
            shift_pos, baseline, after = int(pos[k]), m1, m2
    t = np.arange(len(y))
    level = np.where((shift_pos is not None) & (t >= (shift_pos if shift_pos is not None else 0)), after, baseline)
    resid = np.full(len(y), np.nan)
    resid[ok] = y[ok] / level[ok] - 1
    sigma = max(1.4826 * np.nanmedian(np.abs(resid - np.nanmedian(resid))), 0.01)
    z = resid / sigma
    spike = ok & (np.abs(z) >= SPIKE_MIN_Z) & (np.abs(resid) >= SPIKE_MIN_PCT)
    flag = np.full(len(y), "", dtype=object)
    if shift_pos is not None:
        flag[t >= shift_pos] = "level shift"
    flag[spike] = "spike"
    flag[~ok] = "no data"
    return baseline, level, z, flag, shift_pos


def detect_all(c, stores):
    area = stores.set_index("store_id")["floor_area_m2"]
    frames = []
    for col, (unit, per_area) in METRICS.items():
        wide = c.pivot(index="month", columns="store_id", values=col).sort_index()
        if per_area:
            inten = wide.div(area, axis=1)
            pf = peer_factor(inten)
        for sid in wide.columns:
            if per_area:
                y = (inten[sid] / pf[sid]).to_numpy(float)
                scale = (pf[sid] * area[sid]).to_numpy(float)
            else:
                y = wide[sid].to_numpy(float)
                scale = np.ones(len(wide))
            baseline, level, z, flag, shift_pos = detect(y)
            frames.append(pd.DataFrame({
                "store_id": sid, "metric": col, "unit": unit, "month": wide.index,
                "actual": wide[sid].to_numpy(float),
                "expected": baseline * scale,      # what the store "should" use (pre-shift baseline)
                "fitted": level * scale,           # level incl. any sustained shift
                "z_score": z, "flag": flag,
            }))
    d = pd.concat(frames, ignore_index=True)
    d["deviation_pct"] = (d["actual"] / d["expected"] - 1) * 100
    return d


def build_findings(d, stores, c):
    names = stores.set_index("store_id")["store_name"]
    tariff = (c["electricity_cost_inr"] / c["electricity_kwh"]).groupby(c["store_id"]).median()

    def price(sid, metric, qty):
        if metric == "electricity_kwh":
            return qty * tariff[sid]
        if metric == "diesel_l":
            return qty * DIESEL_PRICE_INR_PER_L
        return np.nan   # no water tariff in the data

    rows = []
    for (sid, metric), g in d.groupby(["store_id", "metric"]):
        g = g.sort_values("month")
        for _, r in g[g["flag"] == "spike"].iterrows():
            ex = r["actual"] - r["fitted"] if r["flag"] == "spike" else 0
            rows.append({"store_id": sid, "store_name": names[sid], "metric": metric, "type": "spike",
                         "start_month": r["month"].strftime("%Y-%m"), "months_affected": 1,
                         "expected": r["expected"], "actual": r["actual"], "change_pct": (r["actual"] / r["fitted"] - 1) * 100,
                         "excess_qty": ex, "excess_cost_inr": price(sid, metric, ex)})
        sh = g[g["flag"] == "level shift"]
        sh_all = g[g["flag"].isin(["level shift"]) | ((g["flag"] == "spike") & (g["month"] >= sh["month"].min()))] if len(sh) else sh
        if len(sh):
            ex = (sh_all["actual"] - sh_all["expected"]).sum()
            rows.append({"store_id": sid, "store_name": names[sid], "metric": metric, "type": "level shift",
                         "start_month": sh["month"].min().strftime("%Y-%m"), "months_affected": len(sh_all),
                         "expected": sh_all["expected"].mean(), "actual": sh_all["actual"].mean(),
                         "change_pct": (sh_all["fitted"].mean() / sh_all["expected"].mean() - 1) * 100,
                         "excess_qty": ex, "excess_cost_inr": price(sid, metric, ex)})
    f = pd.DataFrame(rows)
    return f.sort_values(["store_id", "metric", "start_month"]).reset_index(drop=True)


# -------------------------------------------------------------- EUI & carbon
def annual_kpis(c, d, stores, ef):
    lo, hi = pd.Timestamp(KPI_WINDOW[0]), pd.Timestamp(KPI_WINDOW[1])
    tariff = (c["electricity_cost_inr"] / c["electricity_kwh"]).groupby(c["store_id"]).median()
    e = d[(d["metric"] == "electricity_kwh") & d["month"].between(lo, hi)].copy()
    e["imputed"] = e["actual"].isna()
    e["kwh"] = e["actual"].fillna(e["fitted"])      # gaps filled with the fitted level, flagged below
    w = c[c["month"].between(lo, hi)]
    k = stores.copy()
    k["annual_kwh"] = k["store_id"].map(e.groupby("store_id")["kwh"].sum())
    k["imputed_months"] = k["store_id"].map(e.groupby("store_id")["imputed"].sum()).astype(int)
    k["annual_cost_inr"] = k["annual_kwh"] * k["store_id"].map(tariff)
    k["water_kl"] = k["store_id"].map(w.groupby("store_id")["water_kl"].sum())
    k["diesel_l"] = k["store_id"].map(w.groupby("store_id")["diesel_l"].sum())
    k["eui_kwh_m2"] = k["annual_kwh"] / k["floor_area_m2"]
    med = k["eui_kwh_m2"].median()
    k["eui_vs_median_pct"] = (k["eui_kwh_m2"] / med - 1) * 100
    k["tco2e_electricity"] = k["annual_kwh"] * ef["electricity"] / 1000
    k["tco2e_diesel"] = k["diesel_l"] * ef["diesel"] / 1000
    k["tco2e_total"] = k["tco2e_electricity"] + k["tco2e_diesel"]
    k["kgco2e_per_m2"] = k["tco2e_total"] * 1000 / k["floor_area_m2"]
    k["tariff_inr_per_kwh"] = k["store_id"].map(tariff)
    k["eui_rank"] = k["eui_kwh_m2"].rank(ascending=False).astype(int)
    return k.sort_values("eui_rank").reset_index(drop=True)


# ----------------------------------------------------------------- savings
def savings_table(kpi, meas, findings, ef):
    rows = []
    elec = meas[meas["applies_to"] == "electricity"]
    for _, s in kpi.iterrows():
        for _, m in elec.iterrows():
            for scen, (sav_mult, capex_mult) in SCENARIOS.items():
                kwh_saved = s["annual_kwh"] * m["savings_pct"] / 100 * sav_mult
                inr = kwh_saved * s["tariff_inr_per_kwh"]
                capex = m["capex_inr_per_m2"] * capex_mult * s["floor_area_m2"]
                rows.append({"store_id": s["store_id"], "store_name": s["store_name"], "measure": m["measure_name"],
                             "scenario": scen, "baseline_kwh": s["annual_kwh"], "kwh_saved": kwh_saved,
                             "inr_saved_per_year": inr, "capex_inr": capex,
                             "payback_years": capex / inr if inr else np.nan,
                             "tco2e_avoided": kwh_saved * ef["electricity"] / 1000})
    # operational fix for sustained electricity level shifts (no capex assumed)
    shifts = findings[(findings["metric"] == "electricity_kwh") & (findings["type"] == "level shift")]
    for _, f in shifts.iterrows():
        s = kpi[kpi["store_id"] == f["store_id"]].iloc[0]
        annual_excess = s["annual_kwh"] - s["annual_kwh"] / (1 + f["change_pct"] / 100)
        for scen, (sav_mult, _) in SCENARIOS.items():
            kwh_saved = annual_excess * min(sav_mult, 1.0)   # never claim more than the excess itself
            rows.append({"store_id": s["store_id"], "store_name": s["store_name"],
                         "measure": "Fix baseload jump (operational)", "scenario": scen,
                         "baseline_kwh": s["annual_kwh"], "kwh_saved": kwh_saved,
                         "inr_saved_per_year": kwh_saved * s["tariff_inr_per_kwh"], "capex_inr": 0.0,
                         "payback_years": 0.0, "tco2e_avoided": kwh_saved * ef["electricity"] / 1000})
    return pd.DataFrame(rows)


# ------------------------------------------------------------- verification
def answer_key_check(findings, quality):
    def has_f(sid, metric, typ, month):
        return bool(((findings["store_id"] == sid) & (findings["metric"] == metric) & (findings["type"] == typ)
                     & (findings["start_month"] == month)).any())

    def has_q(sid, issue, month):
        return bool(((quality["store_id"] == sid) & (quality["issue"] == issue) & (quality["month"] == month)).any())

    key = [
        ("Store 3 electricity spike 2025-07", has_f(3, "electricity_kwh", "spike", "2025-07")),
        ("Store 5 electricity level shift from 2025-09", has_f(5, "electricity_kwh", "level shift", "2025-09")),
        ("Store 8 diesel spike 2025-06", has_f(8, "diesel_l", "spike", "2025-06")),
        ("Store 6 water spike 2025-10", has_f(6, "water_kl", "spike", "2025-10")),
        ("Store 7 missing month 2025-03", has_q(7, "Missing month", "2025-03")),
        ("Store 9 NULL electricity 2026-01", has_q(9, "Missing reading (NULL)", "2026-01")),
        ("Store 2 duplicate bill 2025-05", has_q(2, "Duplicate bill", "2025-05")),
    ]
    expected_f = {(3, "electricity_kwh", "spike"), (5, "electricity_kwh", "level shift"),
                  (8, "diesel_l", "spike"), (6, "water_kl", "spike")}
    extra = findings[[(r.store_id, r.metric, r.type) not in expected_f for r in findings.itertuples()]]
    chk = pd.DataFrame(key, columns=["injected_problem", "detected"])
    return chk, extra


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["csv", "mysql"], default="csv")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    stores, util, ef, meas = load(args.source)
    quality = quality_report(util, stores)
    c = clean(util, stores)
    d = detect_all(c, stores)
    findings = build_findings(d, stores, c)
    kpi = annual_kpis(c, d, stores, ef)
    roi = savings_table(kpi, meas, findings, ef)
    chk, extra = answer_key_check(findings, quality)

    # monthly electricity table for dashboards (clean + expected + flag + carbon)
    m = d[d["metric"] == "electricity_kwh"][["store_id", "month", "actual", "expected", "fitted", "z_score", "flag"]]
    m = m.rename(columns={"actual": "electricity_kwh", "expected": "expected_kwh", "fitted": "fitted_kwh",
                          "flag": "electricity_flag"})
    for col in ["water_kl", "diesel_l", "electricity_cost_inr"]:
        m = m.merge(c[["store_id", "month", col]], on=["store_id", "month"], how="left")
    for col, short in [("water_kl", "water_flag"), ("diesel_l", "diesel_flag")]:
        m = m.merge(d[d["metric"] == col][["store_id", "month", "flag"]].rename(columns={"flag": short}),
                    on=["store_id", "month"], how="left")
    m = m.merge(stores[["store_id", "store_name", "city", "floor_area_m2"]], on="store_id")
    m["tco2e"] = (m["electricity_kwh"].fillna(0) * ef["electricity"] + m["diesel_l"].fillna(0) * ef["diesel"]) / 1000
    m["excess_kwh"] = np.where(m["electricity_flag"].isin(["spike", "level shift"]),
                               m["electricity_kwh"] - m["expected_kwh"], 0.0)

    m["excess_cost_inr"] = m["excess_kwh"] * m["store_id"].map(kpi.set_index("store_id")["tariff_inr_per_kwh"])

    quality.to_csv(f"{OUT}/data_quality.csv", index=False)
    findings.to_csv(f"{OUT}/anomaly_findings.csv", index=False)
    d[d["flag"].isin(["spike", "level shift"])].to_csv(f"{OUT}/anomaly_months.csv", index=False)
    kpi.to_csv(f"{OUT}/kpi_by_store.csv", index=False)
    roi.to_csv(f"{OUT}/savings_roi.csv", index=False)
    m.to_csv(f"{OUT}/monthly_clean.csv", index=False)
    chk.to_csv(f"{OUT}/answer_key_check.csv", index=False)
    pd.DataFrame([{"assumption": "Grid emission factor (kgCO2e/kWh)", "value": ef["electricity"], "status": "CEA v20.0, FY2023-24 (Table S)"},
                  {"assumption": "Diesel emission factor (kgCO2e/L)", "value": ef["diesel"], "status": "PLACEHOLDER - verify source"},
                  {"assumption": "Diesel price (INR/L)", "value": DIESEL_PRICE_INR_PER_L, "status": "ASSUMPTION"},
                  {"assumption": "KPI window", "value": f"{KPI_WINDOW[0][:7]} to {KPI_WINDOW[1][:7]}", "status": "choice"}]
                 ).to_csv(f"{OUT}/assumptions.csv", index=False)

    tot = {"annual_kwh": kpi["annual_kwh"].sum(), "annual_cost_inr": kpi["annual_cost_inr"].sum(),
           "tco2e_total": kpi["tco2e_total"].sum(), "median_eui": kpi["eui_kwh_m2"].median(),
           "excess_cost_inr_flagged": findings["excess_cost_inr"].sum()}
    json.dump({k: float(v) for k, v in tot.items()}, open(f"{OUT}/portfolio_totals.json", "w"), indent=2)

    pd.set_option("display.width", 200, "display.max_columns", 30, "display.float_format", "{:,.1f}".format)
    print("== Data quality issues ==\n", quality.to_string(index=False))
    print("\n== Anomaly findings ==\n", findings.drop(columns=["store_id"]).to_string(index=False))
    print("\n== Answer key check ==\n", chk.to_string(index=False))
    print("\nUnexpected extra findings (false positives):", len(extra))
    if len(extra):
        print(extra.to_string(index=False))
    print("\n== KPIs by store ==\n", kpi[["store_name", "annual_kwh", "eui_kwh_m2", "eui_vs_median_pct",
                                         "tco2e_total", "annual_cost_inr", "imputed_months"]].to_string(index=False))
    print("\nPortfolio:", {k: round(float(v), 1) for k, v in tot.items()})


if __name__ == "__main__":
    main()
