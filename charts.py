"""Static charts for the README (run after analysis.py). Palette: validated reference palette, light mode."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.facecolor": SURFACE, "figure.facecolor": SURFACE,
                     "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "text.color": INK, "axes.spines.top": False, "axes.spines.right": False})

kpi = pd.read_csv("outputs/kpi_by_store.csv")
months = pd.read_csv("outputs/monthly_clean.csv", parse_dates=["month"])
roi = pd.read_csv("outputs/savings_roi.csv")
flagged = set(pd.read_csv("outputs/anomaly_findings.csv").query("metric == 'electricity_kwh'")["store_id"])

# 1 --- EUI by store
k = kpi.sort_values("eui_kwh_m2")
fig, ax = plt.subplots(figsize=(8, 4.6))
cols = [ORANGE if s in flagged else BLUE for s in k["store_id"]]
bars = ax.barh(k["store_name"], k["eui_kwh_m2"], color=cols, height=0.62)
med = kpi["eui_kwh_m2"].median()
ax.axvline(med, color=INK2, lw=1, ls="--")
ax.text(med - 2, len(k) - 0.35, f"portfolio median {med:.0f}", color=INK2, fontsize=8.5, va="bottom", ha="right")
for b, v in zip(bars, k["eui_kwh_m2"]):
    ax.text(v + 2, b.get_y() + b.get_height() / 2, f"{v:.0f}", va="center", fontsize=9, color=INK)
ax.set_xlabel("Energy Use Intensity, kWh per m² per year (Oct 2025 - Sep 2026)")
ax.set_title("Store 5 is the least efficient store - and has a sustained baseload jump", loc="left", fontsize=11, fontweight="bold")
ax.xaxis.grid(True, color=GRID, lw=0.6); ax.set_axisbelow(True); ax.set_xlim(0, k["eui_kwh_m2"].max() * 1.12); ax.set_ylim(-0.6, len(k) + 0.1)
ax.text(0, -0.17, "Orange = store with a detected electricity anomaly. Synthetic data.", transform=ax.transAxes, fontsize=8, color=INK2)
fig.tight_layout(); fig.savefig("charts/eui_by_store.png", dpi=170); plt.close(fig)

# 2 --- trends with anomaly markers
fig, axes = plt.subplots(1, 2, figsize=(11, 4.1), sharey=False)
F = pd.read_csv("outputs/anomaly_findings.csv").query("metric == 'electricity_kwh'").set_index("store_id")
note5 = f"sustained {F.loc[5, 'change_pct']:+.0f}% baseload shift from Sep 2025"
note3 = f"one-off {F.loc[3, 'change_pct']:+.0f}% spike in Jul 2025"
for ax, sid, note in [(axes[0], 5, note5), (axes[1], 3, note3)]:
    d = months[months["store_id"] == sid].sort_values("month")
    ax.plot(d["month"], d["electricity_kwh"] / 1000, color=BLUE, lw=2, label="Actual")
    ax.plot(d["month"], d["expected_kwh"] / 1000, color=INK2, lw=1.4, ls="--", label="Expected (peer-adjusted baseline)")
    f = d[d["electricity_flag"].isin(["spike", "level shift"])]
    ax.scatter(f["month"], f["electricity_kwh"] / 1000, s=34, color=ORANGE, edgecolor=SURFACE, linewidth=1.5, zorder=3, label="Flagged month")
    ax.set_title(f"Store {sid}: {note}", loc="left", fontsize=10, fontweight="bold")
    ax.set_ylabel("MWh per month"); ax.yaxis.grid(True, color=GRID, lw=0.6); ax.set_axisbelow(True)
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10])); ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    ax.tick_params(axis="x", labelrotation=30)
axes[0].legend(frameon=False, fontsize=8.5, loc="upper left")
fig.tight_layout(); fig.savefig("charts/anomaly_trends.png", dpi=170); plt.close(fig)

# 3 --- payback by measure (portfolio, with scenario range)
g = roi[roi["measure"] != "Fix baseload jump (operational)"].groupby(["measure", "scenario"])[["capex_inr", "inr_saved_per_year"]].sum()
g["payback"] = g["capex_inr"] / g["inr_saved_per_year"]
order = ["HVAC scheduling", "LED retrofit", "Rooftop solar"]
fig, ax = plt.subplots(figsize=(8, 3.6))
for i, m in enumerate(order):
    lo, ex, hi = (g.loc[(m, s), "payback"] for s in ["optimistic", "expected", "conservative"])
    ax.plot([lo, hi], [i, i], color=GRID, lw=7, solid_capstyle="round", zorder=1)
    ax.scatter([ex], [i], s=90, color=BLUE, edgecolor=SURFACE, linewidth=2, zorder=3)
    ax.text(hi + 0.35, i, f"{ex:.1f} yrs expected  ({lo:.1f} - {hi:.1f})", va="center", fontsize=9)
ax.set_yticks(range(len(order))); ax.set_yticklabels(order); ax.set_ylim(-0.6, len(order) - 0.4)
ax.set_xlim(0, g["payback"].max() * 1.75); ax.set_xlabel("Simple payback, years (portfolio of 10 stores; bar = optimistic to conservative)")
ax.xaxis.grid(True, color=GRID, lw=0.6); ax.set_axisbelow(True)
ax.set_title("Quick wins first: scheduling and LEDs pay back in about 1-2 years", loc="left", fontsize=11, fontweight="bold")
ax.text(0, -0.30, "Savings % and capex are placeholder assumptions - replace with sourced values.", transform=ax.transAxes, fontsize=8, color=INK2)
fig.tight_layout(); fig.savefig("charts/payback_by_measure.png", dpi=170); plt.close(fig)
print("charts written")
