#!/usr/bin/env python3
"""
plot_results.py  -  turn the raw experiment CSVs into a summary table + graphs
 
Reads  results/raw/*_run<N>.csv       (written by run_tests.py)
Writes results/summary.csv            (one row per scenario and policy)
       results/failover.csv           (crash scenario: detection / recovery)
       results/avg_latency.png        average and p95 response time
       results/request_share.png      requests served per server
       results/load_imbalance.png     std dev of requests per server
       results/failed_requests.png    failed requests
       results/crash_timeline.png     latency and failures over time
 
Usage (no sudo):
    python3 experiments/plot_results.py
Needs: pandas, matplotlib   (sudo apt install python3-pandas python3-matplotlib)
"""
 
import glob
import os
import re
import sys
 
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402
import pandas as pd                      # noqa: E402
 
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "results", "raw")
OUT_DIR = os.path.join(ROOT, "results")
 
SERVERS = ["h4", "h5", "h6"]
POLICIES = ["roundrobin", "adaptive"]
LABELS = {"roundrobin": "Round robin (static)", "adaptive": "Adaptive"}
COLORS = {"roundrobin": "#d9822b", "adaptive": "#2b7bd9"}
SCENARIO_ORDER = ["light", "heavy", "slow_server", "crash"]
 
 
def load_runs():
    frames = []
    for path in sorted(glob.glob(os.path.join(RAW_DIR, "*_run*.csv"))):
        name = os.path.basename(path)
        if name.endswith("_events.csv") or name.endswith("_ctrlevents.csv"):
            continue
        if re.search(r"_run\d+\.csv$", name):
            frames.append(pd.read_csv(path))
    if not frames:
        sys.exit("No data found in %s. Run experiments/run_tests.py first."
                 % RAW_DIR)
    df = pd.concat(frames, ignore_index=True)
    df["ok"] = df["status"] == "OK"
    return df
 
 
def p95(series):
    return float(np.percentile(series, 95)) if len(series) else float("nan")
 
 
def per_run_summary(df):
    rows = []
    for (scenario, policy, run), g in df.groupby(["scenario", "policy", "run"]):
        ok = g[g["ok"]]
        counts = ok.groupby("server").size().reindex(SERVERS, fill_value=0)
        span = max(g["timestamp"].max() - g["timestamp"].min(), 1e-9)
        row = {
            "scenario": scenario, "policy": policy, "run": run,
            "requests": len(g), "ok": len(ok), "failed": int((~g["ok"]).sum()),
            "avg_latency_ms": ok["latency_ms"].mean() if len(ok) else np.nan,
            "p95_latency_ms": p95(ok["latency_ms"]),
            "throughput_rps": len(ok) / span,
            "imbalance_std": float(counts.std(ddof=0)),
        }
        for s in SERVERS:
            row["share_" + s] = int(counts[s])
        rows.append(row)
    return pd.DataFrame(rows)
 
 
def ordered(values):
    return [s for s in SCENARIO_ORDER if s in values] + \
           [s for s in values if s not in SCENARIO_ORDER]
 
 
def grouped_bars(ax, summary, column, ylabel, title):
    scenarios = ordered(summary["scenario"].unique())
    x = np.arange(len(scenarios))
    width = 0.38
    for i, policy in enumerate(POLICIES):
        vals = []
        for sc in scenarios:
            sel = summary[(summary["scenario"] == sc) &
                          (summary["policy"] == policy)]
            vals.append(sel[column].mean() if len(sel) else 0)
        bars = ax.bar(x + (i - 0.5) * width, vals, width,
                      label=LABELS[policy], color=COLORS[policy])
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    "%.0f" % v if v >= 10 else "%.1f" % v,
                    ha="center", va="bottom", fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
 
 
def plot_latency(summary):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    grouped_bars(axes[0], summary, "avg_latency_ms", "ms",
                 "Average response time")
    grouped_bars(axes[1], summary, "p95_latency_ms", "ms",
                 "95th percentile response time")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "avg_latency.png"), dpi=150)
    plt.close(fig)
 
 
def plot_simple(summary, column, ylabel, title, filename):
    fig, ax = plt.subplots(figsize=(6.5, 4))
    grouped_bars(ax, summary, column, ylabel, title)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, filename), dpi=150)
    plt.close(fig)
 
 
def plot_share(summary):
    scenarios = ordered(summary["scenario"].unique())
    fig, axes = plt.subplots(1, len(scenarios),
                             figsize=(3.4 * len(scenarios), 3.8),
                             squeeze=False)
    x = np.arange(len(SERVERS))
    width = 0.38
    for ax, sc in zip(axes[0], scenarios):
        for i, policy in enumerate(POLICIES):
            sel = summary[(summary["scenario"] == sc) &
                          (summary["policy"] == policy)]
            vals = [sel["share_" + s].mean() if len(sel) else 0
                    for s in SERVERS]
            ax.bar(x + (i - 0.5) * width, vals, width,
                   label=LABELS[policy], color=COLORS[policy])
        ax.set_xticks(x)
        ax.set_xticklabels(SERVERS)
        ax.set_title(sc)
        ax.set_ylabel("requests served")
    axes[0][0].legend(fontsize=8)
    fig.suptitle("Requests served per server")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "request_share.png"), dpi=150)
    plt.close(fig)
 
 
def read_events(scenario, policy, run):
    path = os.path.join(RAW_DIR, "%s_%s_run%d_events.csv"
                        % (scenario, policy, run))
    return pd.read_csv(path) if os.path.exists(path) else pd.DataFrame()
 
 
def plot_crash_timeline(df):
    crash = df[df["scenario"] == "crash"]
    if crash.empty:
        return
    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    run = int(crash["run"].min())
    marked = set()
    for policy in POLICIES:
        g = crash[(crash["policy"] == policy) & (crash["run"] == run)]
        if g.empty:
            continue
        ev = read_events("crash", policy, run)
        t0 = g["timestamp"].min()
        if len(ev) and (ev["event"] == "workload_start").any():
            t0 = ev.loc[ev["event"] == "workload_start", "timestamp"].iloc[0]
        g = g.assign(t=(g["timestamp"] - t0).astype(float))
        g["bin"] = np.floor(g["t"]).astype(int)
        lat = g[g["ok"]].groupby("bin")["latency_ms"].mean()
        fails = g[~g["ok"]].groupby("bin").size()
        axes[0].plot(lat.index, lat.values, label=LABELS[policy],
                     color=COLORS[policy])
        axes[1].plot(fails.index, fails.values, label=LABELS[policy],
                     color=COLORS[policy])
        for _, e in ev.iterrows():
            if e["event"] in ("kill", "revive"):
                x_pos = round(e["timestamp"] - t0)
                if (e["event"], x_pos) in marked:
                    continue                      # draw each marker once
                marked.add((e["event"], x_pos))
                for ax in axes:
                    ax.axvline(e["timestamp"] - t0, color="grey",
                               linestyle="--", linewidth=1)
                axes[0].text(e["timestamp"] - t0, axes[0].get_ylim()[1],
                             " %s %s" % (e["event"], e["server"]),
                             fontsize=8, va="top")
    axes[0].set_ylabel("avg response time (ms)")
    axes[0].set_title("Server crash scenario (run %d): h5 killed, then restarted"
                      % run)
    axes[0].legend()
    axes[1].set_ylabel("failed requests per second")
    axes[1].set_xlabel("seconds since workload start")
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "crash_timeline.png"), dpi=150)
    plt.close(fig)
 
 
def failover_table():
    rows = []
    for path in sorted(glob.glob(os.path.join(
            RAW_DIR, "crash_adaptive_run*_events.csv"))):
        run = int(re.search(r"run(\d+)_events", path).group(1))
        ev = pd.read_csv(path)
        ctrl_path = path.replace("_events.csv", "_ctrlevents.csv")
        if not os.path.exists(ctrl_path):
            continue
        ctrl = pd.read_csv(ctrl_path)
        kill = ev[ev["event"] == "kill"]
        revive = ev[ev["event"] == "revive"]
        if kill.empty:
            continue
        srv = kill["server"].iloc[0]
        t_kill = kill["timestamp"].iloc[0]
        dead = ctrl[(ctrl["event"] == "DEAD") & (ctrl["server"] == srv) &
                    (ctrl["timestamp"] > t_kill)]
        detect = dead["timestamp"].iloc[0] - t_kill if len(dead) else np.nan
        recover = np.nan
        if len(revive):
            t_rev = revive["timestamp"].iloc[0]
            alive = ctrl[(ctrl["event"] == "ALIVE") & (ctrl["server"] == srv) &
                         (ctrl["timestamp"] > t_rev)]
            recover = alive["timestamp"].iloc[0] - t_rev if len(alive) else np.nan
        rows.append({"run": run, "server": srv,
                     "failure_detected_after_s": round(detect, 2),
                     "readmitted_after_restart_s": round(recover, 2)})
    table = pd.DataFrame(rows)
    if len(table):
        table.to_csv(os.path.join(OUT_DIR, "failover.csv"), index=False)
    return table
 
 
def main():
    df = load_runs()
    runs = per_run_summary(df)
    summary = (runs.groupby(["scenario", "policy"])
               .mean(numeric_only=True).drop(columns=["run"]).round(2)
               .reset_index())
    summary["scenario"] = pd.Categorical(
        summary["scenario"], categories=ordered(summary["scenario"].unique()),
        ordered=True)
    summary = summary.sort_values(["scenario", "policy"])
    summary.to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False)
 
    cols = ["scenario", "policy", "avg_latency_ms", "p95_latency_ms",
            "throughput_rps", "imbalance_std", "failed"]
    print("\nSummary (mean over runs)")
    print(summary[cols].to_string(index=False))
 
    plot_latency(summary)
    plot_share(summary)
    plot_simple(summary, "imbalance_std", "std dev of requests per server",
                "Load imbalance (lower is better)", "load_imbalance.png")
    plot_simple(summary, "failed", "failed requests",
                "Failed requests", "failed_requests.png")
    plot_crash_timeline(df)
 
    table = failover_table()
    if len(table):
        print("\nFailover (adaptive, crash scenario)")
        print(table.to_string(index=False))
    print("\nGraphs and tables written to %s" % OUT_DIR)
 
 
if __name__ == "__main__":
    main()

