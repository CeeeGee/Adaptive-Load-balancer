"""
client.py  -  TCP client and workload generator (runs on h1, h2, h3)

Connects to the service address (default 10.0.0.100:8000), sends requests,
measures the response time of each one and records which server answered.
By default every request uses a NEW TCP connection, so the load balancer
makes a fresh decision per request. Use --keepalive to reuse one connection
per worker instead.

Examples:
    python3 app/client.py --requests 20                    # quick test
    python3 app/client.py --concurrency 8 --duration 30    # sustained load
    python3 app/client.py --task-size 5 --csv results/h1.csv

Message protocol (matches app/server.py):
    request : REQUEST|<client>|task_size=<n>
    reply   : RESPONSE|server=<name>|status=OK

CSV columns: timestamp,client,server,latency_ms,status
"""

import argparse
import csv
import os
import socket
import threading
import time
from collections import Counter

FIELDS = ["timestamp", "client", "server", "latency_ms", "status"]


def local_ip(vip, port):
    """This host's address on the path to the service (used as default name)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((vip, port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "client"


def read_line(sock):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionResetError("server closed the connection")
        buf += chunk
        if len(buf) > 65536:
            break
    return buf.decode("utf-8", errors="replace").strip()


def parse_reply(line):
    """RESPONSE|server=h5|status=OK  ->  ('h5', 'OK')"""
    fields = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
    return fields.get("server", "-"), fields.get("status", "ERROR")


class Recorder:
    """Collects results from all worker threads (and writes the CSV)."""

    def __init__(self, name, csv_path, quiet):
        self.name = name
        self.quiet = quiet
        self.lock = threading.Lock()
        self.rows = []
        self.counter = 0
        self.file = None
        self.writer = None
        if csv_path:
            folder = os.path.dirname(csv_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
            self.file = open(csv_path, "a", newline="")
            self.writer = csv.writer(self.file, lineterminator="\n")
            if new:
                self.writer.writerow(FIELDS)

    def add(self, ts, server, latency_ms, status):
        with self.lock:
            self.counter += 1
            n = self.counter
            self.rows.append((server, latency_ms, status))
            if self.writer:
                self.writer.writerow(["%.3f" % ts, self.name, server,
                                      "%.2f" % latency_ms, status])
                self.file.flush()
        if not self.quiet:
            if status == "OK":
                print("[client %s] req %d  -> served by %s  time=%.1fms"
                      % (self.name, n, server, latency_ms), flush=True)
            else:
                print("[client %s] req %d  -> FAILED (%s)  after %.1fms"
                      % (self.name, n, status, latency_ms), flush=True)


def one_request(args, name, sock):
    """Send one request. Returns (sock_or_None, server, latency_ms, status)."""
    t0 = time.time()
    try:
        if sock is None:
            sock = socket.create_connection((args.vip, args.port),
                                            timeout=args.timeout)
        sock.settimeout(args.timeout)
        sock.sendall(("REQUEST|%s|task_size=%d\n"
                      % (name, args.task_size)).encode())
        server, status = parse_reply(read_line(sock))
        latency = (time.time() - t0) * 1000.0
        if not args.keepalive:
            sock.close()
            sock = None
        return sock, server, latency, status
    except socket.timeout:
        err = "FAIL:timeout"
    except (ConnectionError, OSError) as exc:
        err = "FAIL:" + type(exc).__name__
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    return None, "-", (time.time() - t0) * 1000.0, err


def worker(args, name, recorder, end_time):
    sock = None
    sent = 0
    while True:
        if end_time is not None:
            if time.time() >= end_time:
                break
        elif sent >= args.requests:
            break
        sent += 1
        ts = time.time()
        sock, server, latency, status = one_request(args, name, sock)
        recorder.add(ts, server, latency, status)
        if args.think_ms > 0:
            time.sleep(args.think_ms / 1000.0)
    if sock is not None:
        sock.close()


def summarize(name, rows, elapsed):
    ok = [r for r in rows if r[2] == "OK"]
    failed = len(rows) - len(ok)
    print("\n=== client %s summary ===" % name)
    print("Requests: %d   OK: %d   Failed: %d   Time: %.1fs"
          % (len(rows), len(ok), failed, elapsed))
    if ok:
        lat = sorted(r[1] for r in ok)
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        print("Latency: avg=%.1fms  p95=%.1fms  max=%.1fms"
              % (sum(lat) / len(lat), p95, lat[-1]))
        counts = Counter(r[0] for r in ok)
        total = sum(counts.values())
        share = "   ".join("%s: %d (%d%%)" % (s, c, round(100.0 * c / total))
                           for s, c in sorted(counts.items()))
        print("Requests served:   " + share)


def main():
    ap = argparse.ArgumentParser(description="TCP client / workload generator")
    ap.add_argument("--vip", default="10.0.0.100", help="service address")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--requests", type=int, default=20,
                    help="requests per worker (ignored with --duration)")
    ap.add_argument("--duration", type=float, default=0,
                    help="run for this many seconds instead of a fixed count")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel workers (threads)")
    ap.add_argument("--task-size", type=int, default=1,
                    help="work per request (server work = task_size x 10 ms)")
    ap.add_argument("--think-ms", type=float, default=0,
                    help="pause between requests of one worker")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="connect/reply timeout in seconds")
    ap.add_argument("--keepalive", action="store_true",
                    help="reuse one connection per worker")
    ap.add_argument("--name", default=None, help="client name for logs")
    ap.add_argument("--csv", default=None, help="append results to this CSV")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the final summary")
    args = ap.parse_args()

    name = args.name or local_ip(args.vip, args.port)
    recorder = Recorder(name, args.csv, args.quiet)
    start = time.time()
    end_time = start + args.duration if args.duration > 0 else None

    threads = [threading.Thread(target=worker,
                                args=(args, name, recorder, end_time))
               for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\ninterrupted")
    summarize(name, recorder.rows, time.time() - start)


if __name__ == "__main__":
    main()
