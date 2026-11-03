#!/usr/bin/env python3
"""
run_tests.py  -  run IDENTICAL workloads against both controllers

For every scenario, run and policy it:
  1. starts the controller (round robin or adaptive)
  2. builds the Mininet network and starts the servers on h4-h6
  3. applies the scenario set-up (e.g. slows h4)
  4. runs the same client workload on h1-h3 and triggers scheduled faults
  5. stores the raw results in results/raw/

Usage (from the project folder, needs sudo for Mininet):
    sudo python3 experiments/run_tests.py                 # everything, 3 runs
    sudo python3 experiments/run_tests.py --runs 1 --scenarios light crash
    sudo python3 experiments/run_tests.py --policies adaptive

Then make the graphs (no sudo):
    python3 experiments/plot_results.py

Scenarios
    light        3 clients, low concurrency
    heavy        3 clients, high concurrency, heavy tasks
    slow_server  heavy-ish load while h4 is slowed by +150 ms per request
    crash        steady load; h5 is killed at t=12 s and restarted at t=28 s
"""

import argparse
import csv
import os
import pwd
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "topology"))

from mininet.log import setLogLevel                        # noqa: E402
from topo import (CLIENT_PY, CLIENTS, SERVERS, build_network,   # noqa: E402
                  clear_slow, set_slow, start_server, stop_all_servers,
                  stop_server)

RAW_DIR = os.path.join(ROOT, "results", "raw")
LOG_DIR = os.path.join(ROOT, "logs")

POLICY_FILES = {"roundrobin": "roundrobin_lb.py", "adaptive": "adaptive_lb.py"}

# events = (seconds after workload start, action, server)
SCENARIOS = {
    "light": {"conc": 2, "task": 2, "duration": 20, "setup": [], "events": []},
    "heavy": {"conc": 8, "task": 5, "duration": 25, "setup": [], "events": []},
    "slow_server": {"conc": 6, "task": 3, "duration": 30,
                    "setup": [("slow", "h4", 150)], "events": []},
    "crash": {"conc": 4, "task": 3, "duration": 40, "setup": [],
              "events": [(12, "kill", "h5"), (28, "revive", "h5")]},
}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def sudo_ids():
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        return int(uid), int(gid)
    return None


def fix_owner(path):
    """Give files created as root back to the real user."""
    ids = sudo_ids()
    if not ids:
        return
    for base, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(base, name), *ids)
            except OSError:
                pass
    try:
        os.chown(path, *ids)
    except OSError:
        pass


def find_ryu(explicit):
    if explicit:
        return explicit
    home = pwd.getpwnam(os.environ.get("SUDO_USER", "root")).pw_dir
    for cand in (os.path.join(home, "ryu-env", "bin", "ryu-manager"),
                 os.path.join(ROOT, "ryu-env", "bin", "ryu-manager")):
        if os.access(cand, os.X_OK):
            return cand
    sys.exit("Could not find ryu-manager. Pass it with --ryu /path/to/ryu-manager")


def mn_cleanup():
    subprocess.run(["mn", "-c"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def start_controller(ryu, policy, logfile):
    cmd = [ryu, os.path.join(ROOT, "controller", POLICY_FILES[policy])]
    kwargs = {"cwd": ROOT, "stdout": logfile, "stderr": subprocess.STDOUT}
    ids = sudo_ids()
    if ids:                                   # run Ryu as the normal user
        kwargs["user"], kwargs["group"] = ids
        home = pwd.getpwuid(ids[0]).pw_dir
        kwargs["env"] = dict(os.environ, HOME=home)
    return subprocess.Popen(cmd, **kwargs)


def stop_controller(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ----------------------------------------------------------------------
# one run = one (scenario, policy, run number)
# ----------------------------------------------------------------------
def run_one(ryu, scenario, policy, run_idx, warmup):
    cfg = SCENARIOS[scenario]
    tag = "%s_%s_run%d" % (scenario, policy, run_idx)
    tmp_dir = os.path.join(RAW_DIR, "_tmp_" + tag)
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    print("\n=== %s ===" % tag, flush=True)

    mn_cleanup()
    for name in SERVERS:
        clear_slow(name)

    logfile = open(os.path.join(LOG_DIR, "controller_%s.log" % tag), "w")
    ctrl = start_controller(ryu, policy, logfile)
    time.sleep(3)
    events = []
    net = None
    try:
        net = build_network()
        for name in SERVERS:
            start_server(net, name)
        print("  warming up %.0fs (servers register with the controller)"
              % warmup, flush=True)
        time.sleep(warmup)

        for action, server, value in cfg["setup"]:
            if action == "slow":
                set_slow(server, value)
                events.append((time.time(), "slow", server))

        t_start = time.time()
        events.append((t_start, "workload_start", "-"))
        procs = []
        for client in CLIENTS:
            cmd = ["python3", "-u", CLIENT_PY, "--name", client,
                   "--concurrency", str(cfg["conc"]),
                   "--task-size", str(cfg["task"]),
                   "--duration", str(cfg["duration"]),
                   "--timeout", "3", "--quiet",
                   "--csv", os.path.join(tmp_dir, client + ".csv")]
            procs.append(net.get(client).popen(cmd))
        print("  workload running for %ds ..." % cfg["duration"], flush=True)

        for at, action, server in sorted(cfg["events"]):
            delay = t_start + at - time.time()
            if delay > 0:
                time.sleep(delay)
            if action == "kill":
                stop_server(net, server)
            elif action == "revive":
                start_server(net, server)
            events.append((time.time(), action, server))
            print("  t=%2ds  %s %s" % (at, action, server), flush=True)

        for p in procs:
            try:
                p.communicate(timeout=cfg["duration"] + 30)
            except subprocess.TimeoutExpired:
                p.kill()
        events.append((time.time(), "workload_end", "-"))
    finally:
        if net is not None:
            stop_all_servers(net)
            net.stop()
        stop_controller(ctrl)
        logfile.close()
        for name in SERVERS:
            clear_slow(name)
        mn_cleanup()

    # ---- merge client CSVs -------------------------------------------
    out_csv = os.path.join(RAW_DIR, tag + ".csv")
    rows = 0
    with open(out_csv, "w", newline="") as out:
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["policy", "scenario", "run", "timestamp", "client",
                    "server", "latency_ms", "status"])
        for client in CLIENTS:
            path = os.path.join(tmp_dir, client + ".csv")
            if not os.path.exists(path):
                continue
            with open(path) as f:
                for r in csv.DictReader(f):
                    w.writerow([policy, scenario, run_idx, r["timestamp"],
                                r["client"], r["server"], r["latency_ms"],
                                r["status"]])
                    rows += 1

    with open(os.path.join(RAW_DIR, tag + "_events.csv"), "w",
              newline="") as out:
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["policy", "scenario", "run", "timestamp", "event",
                    "server"])
        for ts, ev, srv in events:
            w.writerow([policy, scenario, run_idx, "%.3f" % ts, ev, srv])

    ctrl_events = os.path.join(ROOT, "results", "events_adaptive.csv")
    if policy == "adaptive" and os.path.exists(ctrl_events):
        os.replace(ctrl_events,
                   os.path.join(RAW_DIR, tag + "_ctrlevents.csv"))

    for client in CLIENTS:
        try:
            os.remove(os.path.join(tmp_dir, client + ".csv"))
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    print("  saved %d requests -> %s" % (rows, out_csv), flush=True)


def main():
    ap = argparse.ArgumentParser(description="Round robin vs adaptive tests")
    ap.add_argument("--runs", type=int, default=3,
                    help="repetitions per scenario and policy (default 3)")
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS),
                    choices=list(SCENARIOS))
    ap.add_argument("--policies", nargs="+", default=["roundrobin", "adaptive"],
                    choices=list(POLICY_FILES))
    ap.add_argument("--warmup", type=float, default=6.0,
                    help="seconds to let servers register before the workload")
    ap.add_argument("--ryu", default=None, help="path to ryu-manager")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: sudo python3 experiments/run_tests.py")
    ryu = find_ryu(args.ryu)
    setLogLevel("warning")
    os.makedirs(RAW_DIR, exist_ok=True)
    # results/ was just created as root; Ryu runs as the normal user and must
    # be able to write results/decisions_adaptive.csv and events_adaptive.csv
    fix_owner(os.path.join(ROOT, "results"))

    started = time.time()
    try:
        for scenario in args.scenarios:
            for run_idx in range(1, args.runs + 1):
                for policy in args.policies:     # alternate to reduce drift
                    run_one(ryu, scenario, policy, run_idx, args.warmup)
    finally:
        fix_owner(os.path.join(ROOT, "results"))
        fix_owner(LOG_DIR)
    print("\nAll done in %.0f s. Raw data: %s" % (time.time() - started, RAW_DIR))
    print("Next: python3 experiments/plot_results.py")


if __name__ == "__main__":
    main()
