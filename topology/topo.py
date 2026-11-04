#!/usr/bin/env python3
"""
topo.py  -  Mininet topology + demo menu for the adaptive load balancer

Network (one switch, remote Ryu controller):

    h1 h2 h3  (clients)  ports 1-3
    h4 h5 h6  (servers)  ports 4-6      -> s1 (Open vSwitch, OpenFlow 1.3)
    root ns   10.0.0.254 port 7            |
                                      Ryu controller (127.0.0.1:6653)

    IPs  : h1..h6 = 10.0.0.1 .. 10.0.0.6
    MACs : 00:00:00:00:00:01 .. 00:00:00:00:00:06
    The root namespace is attached to the switch so the servers' UDP load
    reports (to 10.0.0.254:9000) reach the controller process.

Two-terminal workflow (the controller log stays visible in Terminal 1):

    Terminal 1:  ryu-manager controller/adaptive_lb.py     (or roundrobin_lb.py)
    Terminal 2:  sudo python3 topology/topo.py --start-servers --menu

Changing controller without rebuilding the network:
    Ctrl+C Ryu in Terminal 1  ->  press f in the menu  ->  start the other Ryu

Extra typed commands at the mininet> prompt (not in the menu; press m in the
menu to get there, type 'menu' to come back):
    menu                                open the demo menu
    startservers / stopservers          start or stop h4, h5, h6
    killserver h5 / startserver h5      crash or restart one server
    slow h4 150 / unslow h4             add 150 ms to every request on h4
    netdelay h4 100 / netloss h4 10     tc netem delay (ms) / loss (%)
    netclear h4                         remove delay and loss
    cutlink h5 / restorelink h5         bring the h5 <-> s1 link down / up
    client h1 --requests 20             run app/client.py on a host
    flows                               dump the switch flow table
"""

import argparse
import os
import re
import time

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSKernelSwitch, RemoteController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "app", "server.py")
CLIENT_PY = os.path.join(ROOT, "app", "client.py")
LOG_DIR = os.path.join(ROOT, "logs")

CLIENTS = ["h1", "h2", "h3"]
SERVERS = ["h4", "h5", "h6"]
ROOT_IP = "10.0.0.254"
SLOW_FILE = "/tmp/lb_slow_{}"

# what is currently "wrong" in the network (shown in the menu header)
STATE = {"slow": {}, "killed": set(), "cut": set()}

BOLD, DIM, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m",
    "\033[0m")


# ----------------------------------------------------------------------
# Network construction
# ----------------------------------------------------------------------
def build_network(controller_ip="127.0.0.1", controller_port=6653):
    """Create, start and return the Mininet network."""
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink,
                  autoSetMacs=False, autoStaticArp=False, build=False)
    net.addController("c0", controller=RemoteController,
                      ip=controller_ip, port=controller_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure")

    # Links are added in order with explicit port numbers, so h1..h6 sit on
    # switch ports 1..6 (the controller relies on this).
    for i in range(1, 7):
        host = net.addHost("h%d" % i, ip="10.0.0.%d/24" % i,
                           mac="00:00:00:00:00:%02x" % i)
        net.addLink(host, s1, port2=i)

    # Root namespace on port 7: lets the controller process receive reports.
    root = Node("root", inNamespace=False)
    link = net.addLink(root, s1, port2=7, cls=Link)
    root.setIP("%s/24" % ROOT_IP, intf=link.intf1)

    net.start()
    try:
        net.waitConnected(timeout=10)
    except Exception:
        time.sleep(3)
    return net


# ----------------------------------------------------------------------
# Server and fault helpers (used by the CLI and by experiments/run_tests.py)
# ----------------------------------------------------------------------
def start_server(net, name):
    os.makedirs(LOG_DIR, exist_ok=True)
    net.get(name).cmd("python3 -u %s --name %s --report-ip %s "
                      "> %s/%s.log 2>&1 &"
                      % (SERVER_PY, name, ROOT_IP, LOG_DIR, name))


def stop_server(net, name):
    net.get(name).cmd("pkill -f 'server.py --name %s'" % name)


def stop_all_servers(net):
    for name in SERVERS:
        stop_server(net, name)


def set_slow(name, ms):
    with open(SLOW_FILE.format(name), "w") as f:
        f.write(str(ms))


def clear_slow(name):
    try:
        os.remove(SLOW_FILE.format(name))
    except OSError:
        pass


def net_delay(net, name, ms):
    net.get(name).cmd("tc qdisc replace dev %s-eth0 root netem delay %dms"
                      % (name, ms))


def net_loss(net, name, pct):
    net.get(name).cmd("tc qdisc replace dev %s-eth0 root netem loss %d%%"
                      % (name, pct))


def net_clear(net, name):
    net.get(name).cmd("tc qdisc del dev %s-eth0 root" % name)


def run_client(net, host, arg_string):
    return net.get(host).cmd("python3 -u %s %s" % (CLIENT_PY, arg_string))


# ----------------------------------------------------------------------
# Small helpers for the menu
# ----------------------------------------------------------------------
def say(text, style=""):
    print("%s%s%s" % (style, text, RESET if style else ""))


def note(text):
    """Show the real command an action runs."""
    say("  $ " + text, DIM)


def ask(prompt, default, cast=str):
    raw = input("  %s [%s]: " % (prompt, default)).strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        say("  not a valid value, using %s" % default, YELLOW)
        return default


def ask_choice(prompt, options, default=None):
    default = default or options[0]
    value = ask("%s (%s)" % (prompt, "/".join(options)), default)
    if value not in options:
        say("  unknown choice, using %s" % default, YELLOW)
        return default
    return value


# ----------------------------------------------------------------------
# CLI: extra commands + the demo menu
# ----------------------------------------------------------------------
class LBCLI(CLI):
    start_in_menu = False          # set by main() for --menu
    _menu_shown = False

    # ---- shared actions (used by menu and by the typed commands) -------
    def _slow(self, name, ms):
        set_slow(name, ms)
        STATE["slow"][name] = ms

    def _unslow(self, name):
        clear_slow(name)
        STATE["slow"].pop(name, None)

    def _kill(self, name):
        stop_server(self.mn, name)
        STATE["killed"].add(name)

    def _revive(self, name):
        start_server(self.mn, name)
        STATE["killed"].discard(name)

    def _link(self, name, down):
        self.mn.configLinkStatus("s1", name, "down" if down else "up")
        (STATE["cut"].add if down else STATE["cut"].discard)(name)

    def _server_arg(self, line, usage):
        parts = line.split()
        if not parts or parts[0] not in SERVERS:
            print("usage: %s   (server is one of %s)"
                  % (usage, ", ".join(SERVERS)))
            return None
        return parts

    # ---- typed commands --------------------------------------------------
    def do_startservers(self, _line):
        """Start the TCP server on h4, h5 and h6."""
        for name in SERVERS:
            self._revive(name)
        print("servers started (logs in %s)" % LOG_DIR)

    def do_stopservers(self, _line):
        """Stop the TCP servers on h4, h5 and h6."""
        stop_all_servers(self.mn)
        STATE["killed"].update(SERVERS)
        print("servers stopped")

    def do_killserver(self, line):
        """killserver h5  -  crash one server (simulates a failure)."""
        parts = self._server_arg(line, "killserver h5")
        if parts:
            self._kill(parts[0])
            print("%s killed" % parts[0])

    def do_startserver(self, line):
        """startserver h5  -  (re)start one server."""
        parts = self._server_arg(line, "startserver h5")
        if parts:
            self._revive(parts[0])
            print("%s started" % parts[0])

    def do_slow(self, line):
        """slow h4 150  -  add 150 ms to every request on h4."""
        parts = self._server_arg(line, "slow h4 150")
        if parts and len(parts) == 2:
            self._slow(parts[0], int(parts[1]))
            print("%s now +%s ms per request" % (parts[0], parts[1]))

    def do_unslow(self, line):
        """unslow h4  -  remove the extra delay."""
        parts = self._server_arg(line, "unslow h4")
        if parts:
            self._unslow(parts[0])
            print("%s back to normal speed" % parts[0])

    def do_netdelay(self, line):
        """netdelay h4 100  -  add 100 ms network delay on h4's link."""
        parts = self._server_arg(line, "netdelay h4 100")
        if parts and len(parts) == 2:
            net_delay(self.mn, parts[0], int(parts[1]))
            print("%s link delay = %s ms" % (parts[0], parts[1]))

    def do_netloss(self, line):
        """netloss h4 10  -  add 10% packet loss on h4's link."""
        parts = self._server_arg(line, "netloss h4 10")
        if parts and len(parts) == 2:
            net_loss(self.mn, parts[0], int(parts[1]))
            print("%s link loss = %s %%" % (parts[0], parts[1]))

    def do_netclear(self, line):
        """netclear h4  -  remove network delay and loss on h4."""
        parts = self._server_arg(line, "netclear h4")
        if parts:
            net_clear(self.mn, parts[0])
            print("%s link back to normal" % parts[0])

    def do_cutlink(self, line):
        """cutlink h5  -  bring the h5 <-> s1 link down."""
        parts = self._server_arg(line, "cutlink h5")
        if parts:
            self._link(parts[0], True)
            print("link s1 <-> %s DOWN" % parts[0])

    def do_restorelink(self, line):
        """restorelink h5  -  bring the h5 <-> s1 link back up."""
        parts = self._server_arg(line, "restorelink h5")
        if parts:
            self._link(parts[0], False)
            print("link s1 <-> %s UP" % parts[0])

    def do_client(self, line):
        """client h1 --requests 20 --task-size 2  -  run app/client.py on h1."""
        parts = line.split(None, 1)
        if not parts or parts[0] not in CLIENTS:
            print("usage: client h1 [client.py options]   "
                  "(host is one of %s)" % ", ".join(CLIENTS))
            return
        print(run_client(self.mn, parts[0], parts[1] if len(parts) > 1 else ""))

    def do_flows(self, _line):
        """flows  -  dump the switch flow table (shows the rewrite rules)."""
        print(self.mn.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1"))

    def do_menu(self, _line):
        """menu  -  open the demo menu (m inside it returns to this prompt)."""
        return self._menu_loop()

    # ---- demo menu -------------------------------------------------------
    MENU = """
   1) Normal traffic     send 12 requests from h1 (shows the split)
   2) Heavy traffic      12 parallel workers for 15 s (the main test)
   3) Slow a server      make one server slower
   4) Kill a server      crash one server
   5) Reset              un-slow and restart everything
   6) Switch rules       show the rules the controller put in the switch
   7) Change controller  steps for switching round robin <-> adaptive
   q) Quit               stops the network
"""

    def _header(self):
        flags = ["%s SLOW(+%dms)" % (n, ms)
                 for n, ms in sorted(STATE["slow"].items())]
        flags += ["%s KILLED" % n for n in sorted(STATE["killed"])]
        flags += ["%s LINK DOWN" % n for n in sorted(STATE["cut"])]
        say("\n" + "=" * 62, BOLD)
        say("  ADAPTIVE LOAD BALANCER DEMO", BOLD)
        print("  Conditions : %s" % (", ".join(flags) or "all normal"))
        print("  (controller decisions appear in Terminal 1)")
        say("=" * 62, BOLD)

    def _menu_loop(self):
        """Returns True to quit the CLI (stop the network), False for prompt.

        'm' (not shown in the menu) drops to the normal mininet> prompt.
        """
        actions = {"1": self._m_normal, "2": self._m_heavy, "3": self._m_slow,
                   "4": self._m_kill, "5": self._m_reset, "6": self._m_rules,
                   "7": self._m_flush}
        while True:
            self._header()
            print(self.MENU)
            try:
                choice = input("  choose> ").strip().lower()
            except EOFError:
                return True
            except KeyboardInterrupt:
                print()
                continue
            if choice == "m":
                return False
            if choice in ("q", "quit", "exit"):
                return True
            if not choice:
                continue
            action = actions.get(choice)
            if action is None:
                say("  unknown option", YELLOW)
                continue
            try:
                action()
            except KeyboardInterrupt:
                say("\n  interrupted", YELLOW)
            except Exception as exc:               # keep the demo alive
                say("  error: %s" % exc, RED)

    def preloop(self):
        """With --menu, show the menu once when the CLI starts."""
        if self.start_in_menu and not self._menu_shown:
            self._menu_shown = True
            if self._menu_loop():
                self.cmdqueue.append("exit")

    # ---- menu actions ------------------------------------------------------
    def _m_normal(self):
        note("h1: python3 app/client.py --requests 12")
        print(run_client(self.mn, "h1", "--requests 12"))

    def _m_heavy(self):
        # fixed preset so round robin and adaptive get exactly the same load:
        # 12 workers in total, 4 on each client, all three clients at once
        hosts, conc, dur, task = CLIENTS, 4, 15, 3
        note("h1, h2, h3 at the same time: python3 app/client.py "
             "--concurrency %d --duration %d --task-size %d" % (conc, dur, task))
        procs = []
        for host in hosts:
            cmd = ["python3", "-u", CLIENT_PY, "--name", host,
                   "--concurrency", str(conc), "--duration", str(dur),
                   "--task-size", str(task), "--timeout", "3", "--quiet"]
            procs.append((host, self.mn.get(host).popen(cmd)))
        start = time.time()
        try:
            while any(p.poll() is None for _h, p in procs):
                elapsed = time.time() - start
                print("\r  running load from %s  %2.0fs / %ds "
                      % (", ".join(hosts), elapsed, dur), end="", flush=True)
                if elapsed > dur + 30:
                    for _h, p in procs:
                        p.kill()
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            for _h, p in procs:
                p.kill()
            raise
        finally:
            print("\r" + " " * 60 + "\r", end="")
        for _h, p in procs:
            out, _err = p.communicate()
            print(out.decode("utf-8", errors="replace"))

    def _m_slow(self):
        name = ask_choice("server to slow down", SERVERS, "h4")
        ms = ask("extra delay per request (ms)", 300, int)
        note("echo %d > /tmp/lb_slow_%s" % (ms, name))
        self._slow(name, ms)
        say("  %s is now +%d ms per request" % (name, ms), YELLOW)

    def _m_kill(self):
        name = ask_choice("server to kill", SERVERS, "h5")
        note("pkill -f 'server.py --name %s'" % name)
        self._kill(name)
        say("  %s killed. Adaptive should mark it DEAD in about 6 s "
            "(watch Terminal 1)." % name, RED)

    def _m_reset(self):
        for name in SERVERS:
            self._unslow(name)
        for name in sorted(STATE["killed"]):
            note("python3 app/server.py --name %s &" % name)
            self._revive(name)
        for name in sorted(STATE["cut"]):
            self._link(name, False)
        say("  all servers normal and running. Adaptive marks a restarted "
            "server ALIVE after its first report (about 2 s).", GREEN)

    def _m_rules(self):
        note("ovs-ofctl -O OpenFlow13 dump-flows s1")
        out = self.mn.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        lb = [ln for ln in out.splitlines() if "priority=100" in ln]
        say("\n  %d load-balancer rules (priority=100); other rules hidden"
            % len(lb), BOLD)
        for ln in lb[:10]:
            ln = re.sub(r"\s*(duration|table|idle_age|hard_age)=[^,\s]+,?",
                        "", ln).strip()
            print("  " + ln)
        if len(lb) > 10:
            print("  ... %d more" % (len(lb) - 10))
        if not lb:
            say("  none yet - send some traffic first (rules expire after "
                "30 s idle)", YELLOW)

    def _m_flush(self):
        say("  To switch between round robin and adaptive:", BOLD)
        print("   1. Ctrl+C the running Ryu in Terminal 1")
        print("   2. answer y below (clears the old controller's rules)")
        print("   3. start the other controller in Terminal 1")
        if ask("do step 2 now? (y/n)", "n") != "y":
            return
        note("ovs-ofctl -O OpenFlow13 del-flows s1")
        self.mn.get("s1").cmd("ovs-ofctl -O OpenFlow13 del-flows s1")
        say("  done. Now start the other controller in Terminal 1.", GREEN)


def main():
    ap = argparse.ArgumentParser(description="Adaptive LB Mininet topology")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--start-servers", action="store_true",
                    help="start the TCP servers on h4-h6 automatically")
    ap.add_argument("--menu", action="store_true",
                    help="open the demo menu as soon as the network is up")
    args = ap.parse_args()

    setLogLevel("info")
    net = build_network(args.controller_ip, args.controller_port)
    try:
        if args.start_servers:
            for name in SERVERS:
                start_server(net, name)
            time.sleep(1)
            print("*** servers started on %s (logs in %s)"
                  % (", ".join(SERVERS), LOG_DIR))
        LBCLI.start_in_menu = args.menu
        LBCLI(net)
    finally:
        stop_all_servers(net)
        for name in SERVERS:
            clear_slow(name)
        net.stop()


if __name__ == "__main__":
    main()
