#!/usr/bin/env python3
"""
server.py  -  multithreaded TCP application server (runs on h4, h5, h6)

- Listens on a TCP port (default 8000), one thread per client connection.
- Each connection may carry several requests, one per line.
- Simulates work: a limited number of "worker slots" (--capacity) and a work
  time proportional to the request's task_size. When more requests arrive
  than slots, they queue, so response time really grows with load.
- Tracks its own load (active connections, average response time) and sends
  it to the SDN controller every --report-interval seconds over UDP. The
  report also works as a heartbeat: if it stops, the controller marks this
  server DEAD.

Message protocol (text, one message per line):
    request : REQUEST|<client>|task_size=<n>
    reply   : RESPONSE|server=<name>|status=OK
              RESPONSE|server=<name>|status=ERROR     (malformed request)

Load report (UDP datagram, JSON):
    {"server": "h4", "active_conns": 5, "avg_resp_ms": 40.0}

Slowing a server at run time (for experiments):
    echo 150 > /tmp/lb_slow_h4      # adds 150 ms to every request on h4
    rm /tmp/lb_slow_h4              # back to normal

Example:
    python3 app/server.py --name h4
"""

import argparse
import json
import socket
import sys
import threading
import time
from collections import deque

SLOW_FILE = "/tmp/lb_slow_{}"


class Stats:
    """Thread-safe load counters."""

    def __init__(self, window=20):
        self.lock = threading.Lock()
        self.active = 0
        self.served = 0
        self.errors = 0
        self.times = deque(maxlen=window)     # recent response times (ms)

    def conn_open(self):
        with self.lock:
            self.active += 1

    def conn_close(self):
        with self.lock:
            self.active -= 1

    def record(self, ms):
        with self.lock:
            self.served += 1
            self.times.append(ms)

    def error(self):
        with self.lock:
            self.errors += 1

    def snapshot(self):
        with self.lock:
            avg = sum(self.times) / len(self.times) if self.times else 0.0
            return self.active, avg, self.served, self.errors


def extra_delay_ms(name, static_ms):
    """Static --slow-ms plus whatever is in the run-time slow file."""
    extra = float(static_ms)
    try:
        with open(SLOW_FILE.format(name)) as f:
            extra += float(f.read().strip() or 0)
    except (OSError, ValueError):
        pass
    return extra


def parse_request(line):
    """Return (client, task_size) or None if the request is malformed."""
    parts = line.strip().split("|")
    if len(parts) < 2 or parts[0] != "REQUEST":
        return None
    task = 1
    for part in parts[2:]:
        if part.startswith("task_size="):
            try:
                task = max(0, int(part.split("=", 1)[1]))
            except ValueError:
                return None
    return parts[1], task


def handle_client(conn, addr, args, stats, capacity):
    """Serve one client connection until it closes, errors or goes idle."""
    conn.settimeout(args.idle_timeout)
    stats.conn_open()
    try:
        reader = conn.makefile("rb")
        while True:
            raw = reader.readline()
            if not raw:                       # client closed the connection
                break
            t0 = time.time()
            parsed = parse_request(raw.decode("utf-8", errors="replace"))
            if parsed is None:
                stats.error()
                reply = "RESPONSE|server=%s|status=ERROR\n" % args.name
            else:
                _client, task = parsed
                work_ms = task * args.unit_ms + extra_delay_ms(args.name,
                                                               args.slow_ms)
                with capacity:                # wait for a free worker slot
                    time.sleep(work_ms / 1000.0)
                reply = "RESPONSE|server=%s|status=OK\n" % args.name
            conn.sendall(reply.encode())
            stats.record((time.time() - t0) * 1000.0)
    except socket.timeout:
        pass                                  # idle client, just close
    except (ConnectionError, OSError):
        stats.error()                         # client vanished mid-request
    finally:
        stats.conn_close()
        try:
            conn.close()
        except OSError:
            pass


def reporter(args, stats, stop):
    """Send a load report to the controller every --report-interval s."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.report_ip, args.report_port)
    while not stop.is_set():
        active, avg, served, errors = stats.snapshot()
        report = {"server": args.name, "active_conns": active,
                  "avg_resp_ms": round(avg, 1)}
        try:
            sock.sendto(json.dumps(report).encode(), dest)
        except OSError:
            pass                              # controller not reachable yet
        if not args.quiet:
            print("[%s] active=%d avg_resp=%.1fms served=%d errors=%d"
                  % (args.name, active, avg, served, errors), flush=True)
        stop.wait(args.report_interval)


def main():
    ap = argparse.ArgumentParser(description="Threaded TCP application server")
    ap.add_argument("--name", required=True, help="server name, e.g. h4")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    ap.add_argument("--port", type=int, default=8000, help="service TCP port")
    ap.add_argument("--report-ip", default="10.0.0.254",
                    help="controller address for load reports")
    ap.add_argument("--report-port", type=int, default=9000)
    ap.add_argument("--report-interval", type=float, default=2.0)
    ap.add_argument("--capacity", type=int, default=4,
                    help="requests processed in parallel (worker slots)")
    ap.add_argument("--unit-ms", type=float, default=10.0,
                    help="work time per unit of task_size")
    ap.add_argument("--slow-ms", type=float, default=0.0,
                    help="static extra delay per request")
    ap.add_argument("--idle-timeout", type=float, default=30.0,
                    help="close connections idle for this long")
    ap.add_argument("--quiet", action="store_true", help="no periodic output")
    args = ap.parse_args()

    stats = Stats()
    capacity = threading.Semaphore(args.capacity)
    stop = threading.Event()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(128)
    print("[%s] listening on %s:%d (capacity=%d, reports -> %s:%d)"
          % (args.name, args.bind, args.port, args.capacity,
             args.report_ip, args.report_port), flush=True)

    threading.Thread(target=reporter, args=(args, stats, stop),
                     daemon=True).start()

    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle_client,
                             args=(conn, addr, args, stats, capacity),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n[%s] shutting down" % args.name)
    finally:
        stop.set()
        srv.close()


if __name__ == "__main__":
    sys.exit(main())
