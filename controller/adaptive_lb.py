"""
adaptive_lb.py  -  ADAPTIVE controller (load- and health-aware)

Clients talk to one virtual service address (VIP). For every NEW TCP
connection the controller picks the best server using LIVE information:

  1. Server load   : servers send a JSON report over UDP every ~2 s
                     {"server":"h4","active_conns":5,"avg_resp_ms":40}
  2. Network load  : the controller polls switch port statistics and
                     computes Mbps on each server's port
  3. Health        : the report doubles as a heartbeat; a server silent for
                     HEARTBEAT_TIMEOUT seconds is marked DEAD, its flows are
                     deleted, and it is excluded until it reports again

Selection:
  - skip DEAD servers and OVERLOADED servers
  - lowest score wins:
        score = W_CONN * (active_conns + pending)
              + W_RESP * (avg_resp_ms / 10)
              + W_NET  * net_mbps
  - "pending" is +1 added at assignment time (reset by the next real report)
    so a burst of connections does not all land on the same server
  - if every server is overloaded, fall back to the least loaded one
  - the connection stays on its server for its whole life (sticky)

Everything that is not traffic to the VIP is handled like a learning switch.

Run (from ~/adaptive-lb):  ryu-manager controller/adaptive_lb.py
Writes:  results/decisions_adaptive.csv   (one row per assignment)
         results/events_adaptive.csv      (DEAD / ALIVE / overload events)
"""

import csv
import json
import os
import socket
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                    DEAD_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types
from ryu.ofproto import ofproto_v1_3

# ----------------------------------------------------------------------
# Config (addresses must match topology/topo.py and app/server.py)
# ----------------------------------------------------------------------
VIP = "10.0.0.100"                 # virtual service address seen by clients
VMAC = "00:00:00:00:00:64"         # virtual MAC for the VIP
SERVICE_PORT = 8000                # TCP port of the service

# port = switch port the server is plugged into (h4 -> port 4, etc.)
SERVERS = [
    {"name": "h4", "ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "port": 4},
    {"name": "h5", "ip": "10.0.0.5", "mac": "00:00:00:00:00:05", "port": 5},
    {"name": "h6", "ip": "10.0.0.6", "mac": "00:00:00:00:00:06", "port": 6},
]

# Load report channel (servers -> controller, UDP)
REPORT_BIND_IP = "0.0.0.0"         # controller listens on all interfaces
REPORT_PORT = 9000                 # servers send to 10.0.0.254:9000

# Scoring weights (tune during testing, state the final values in the README)
W_CONN = 0.6                       # weight of active connections
W_RESP = 0.3                       # weight of avg response time (per 10 ms)
W_NET = 0.1                        # weight of network load (per Mbps)

OVERLOAD_CONNS = 10                # above this many connections: excluded
HEARTBEAT_TIMEOUT = 6.0            # seconds of silence before DEAD
HEALTH_CHECK_INTERVAL = 1.0        # how often the health thread runs
STATS_INTERVAL = 5.0               # how often port stats are polled

FLOW_PRIORITY = 100                # VIP flows (beat the L2 learning flows)
IDLE_TIMEOUT = 30                  # seconds of silence before a flow expires

RESULTS_DIR = "results"
DECISION_LOG = os.path.join(RESULTS_DIR, "decisions_adaptive.csv")
EVENT_LOG = os.path.join(RESULTS_DIR, "events_adaptive.csv")

COOKIE_BASE = 0x100                # per-server cookie = COOKIE_BASE + index
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


class AdaptiveLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(AdaptiveLB, self).__init__(*args, **kwargs)
        self.mac_to_port = {}          # dpid -> {mac: port}  (learning switch)
        self.datapaths = {}            # dpid -> datapath
        self.conn_table = {}           # (client_ip, client_port) -> server name
        self.prev_port_bytes = {}      # (dpid, port) -> (bytes, timestamp)
        self.tie_pointer = 0           # rotates tie-breaks between equal scores
        self.assign_count = {s["name"]: 0 for s in SERVERS}

        # Per-server live state. last_seen starts at "now" so servers get a
        # grace period to send their first report after the controller starts.
        now = time.time()
        self.servers = {}
        for idx, s in enumerate(SERVERS):
            self.servers[s["name"]] = {
                "name": s["name"], "ip": s["ip"], "mac": s["mac"],
                "port": s["port"], "idx": idx,
                "cookie": COOKIE_BASE + idx,
                "active_conns": 0, "avg_resp_ms": 0.0, "net_mbps": 0.0,
                "pending": 0, "last_seen": now,
                "alive": True, "overloaded": False,
            }
        self.order = [s["name"] for s in SERVERS]

        self._open_logs()
        self.logger.info("Controller started. VIP=%s:%d Policy=ADAPTIVE",
                         VIP, SERVICE_PORT)
        for name in self.order:
            self.logger.info("Server registered: %s (%s) active=0",
                             name, self.servers[name]["ip"])

        hub.spawn(self._report_listener)
        hub.spawn(self._health_loop)
        hub.spawn(self._stats_loop)

    # ------------------------------------------------------------------
    # CSV logs (for the experiment graphs)
    # ------------------------------------------------------------------
    def _open_logs(self):
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self._dec_file = open(DECISION_LOG, "w", newline="")
        self._dec_csv = csv.writer(self._dec_file)
        header = ["timestamp", "client", "chosen", "reason"]
        for n in self.order:
            header += [n + "_score", n + "_conns", n + "_state"]
        self._dec_csv.writerow(header)
        self._dec_file.flush()

        self._evt_file = open(EVENT_LOG, "w", newline="")
        self._evt_csv = csv.writer(self._evt_file)
        self._evt_csv.writerow(["timestamp", "event", "server", "detail"])
        self._evt_file.flush()

    def _log_event(self, event, server, detail=""):
        self._evt_csv.writerow(["%.3f" % time.time(), event, server, detail])
        self._evt_file.flush()

    def _log_decision(self, client, chosen, reason):
        row = ["%.3f" % time.time(), client, chosen, reason]
        for n in self.order:
            s = self.servers[n]
            row += ["%.2f" % self._score(s),
                    s["active_conns"] + s["pending"], self._state(s)]
        self._dec_csv.writerow(row)
        self._dec_file.flush()

    # ------------------------------------------------------------------
    # Server state helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _eff_conns(s):
        return s["active_conns"] + s["pending"]

    @staticmethod
    def _state(s):
        if not s["alive"]:
            return "DEAD"
        if s["overloaded"]:
            return "OVERLOADED"
        return "OK"

    def _score(self, s):
        return (W_CONN * self._eff_conns(s)
                + W_RESP * (s["avg_resp_ms"] / 10.0)
                + W_NET * s["net_mbps"])

    def _loads_str(self):
        parts = []
        for n in self.order:
            s = self.servers[n]
            if not s["alive"]:
                parts.append("%s:DEAD" % n)
            elif s["overloaded"]:
                parts.append("%s:%.1f(OVL)" % (n, self._score(s)))
            else:
                parts.append("%s:%.1f" % (n, self._score(s)))
        return "{" + " ".join(parts) + "}"

    def _check_overload(self, s):
        over = self._eff_conns(s) > OVERLOAD_CONNS
        if over and not s["overloaded"]:
            s["overloaded"] = True
            self.logger.warning("[WARN] %s above threshold (%d > %d). "
                                "Excluded from new assignments.",
                                s["name"], self._eff_conns(s), OVERLOAD_CONNS)
            self._log_event("OVERLOADED", s["name"],
                            "conns=%d" % self._eff_conns(s))
        elif not over and s["overloaded"]:
            s["overloaded"] = False
            self.logger.info("[LOAD] %s back under threshold -> readmitted",
                             s["name"])
            self._log_event("READMITTED", s["name"],
                            "conns=%d" % self._eff_conns(s))

    # ------------------------------------------------------------------
    # Load report listener (UDP, runs in its own green thread)
    # ------------------------------------------------------------------
    def _report_listener(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((REPORT_BIND_IP, REPORT_PORT))
        except Exception as exc:
            self.logger.error("Cannot open report socket on %s:%d (%s)",
                              REPORT_BIND_IP, REPORT_PORT, exc)
            return
        self.logger.info("Listening for load reports on UDP %s:%d",
                         REPORT_BIND_IP, REPORT_PORT)
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                self._handle_report(json.loads(data.decode()), addr)
            except Exception as exc:
                self.logger.warning("Bad load report ignored (%s)", exc)

    def _handle_report(self, rep, addr):
        name = rep.get("server")
        if name not in self.servers:                  # fall back to sender IP
            name = next((n for n, s in self.servers.items()
                         if s["ip"] == addr[0]), None)
        if name is None:
            return
        s = self.servers[name]
        s["active_conns"] = int(rep.get("active_conns", 0))
        s["avg_resp_ms"] = float(rep.get("avg_resp_ms", 0.0))
        s["last_seen"] = time.time()
        s["pending"] = 0                  # real data replaces our estimate

        if not s["alive"]:
            s["alive"] = True
            self.logger.info("[HEALTH] %s heartbeat received -> marked ALIVE, "
                             "back in rotation", name)
            self._log_event("ALIVE", name, "recovered")
        self._check_overload(s)

    # ------------------------------------------------------------------
    # Health monitor: silence => DEAD, delete that server's flows
    # ------------------------------------------------------------------
    def _health_loop(self):
        while True:
            hub.sleep(HEALTH_CHECK_INTERVAL)
            now = time.time()
            for s in self.servers.values():
                silent = now - s["last_seen"]
                if s["alive"] and silent > HEARTBEAT_TIMEOUT:
                    self._mark_dead(s, silent)

    def _mark_dead(self, s, silent):
        s["alive"] = False
        s["overloaded"] = False
        s["pending"] = 0
        self.logger.warning("[HEALTH] %s missed heartbeats (%.1fs silent) "
                            "-> marked DEAD", s["name"], silent)
        pinned = [k for k, n in self.conn_table.items() if n == s["name"]]
        for k in pinned:
            del self.conn_table[k]
        self._remove_server_flows(s)
        self.logger.info("[FLOW-REMOVED] %d connections pinned to %s cleared",
                         len(pinned), s["name"])
        self.logger.info("[LOAD] %s", self._loads_str())
        self._log_event("DEAD", s["name"],
                        "silent=%.1fs flows_cleared=%d" % (silent, len(pinned)))

    def _remove_server_flows(self, s):
        for dp in list(self.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            mod = parser.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL,
                                    command=ofp.OFPFC_DELETE,
                                    out_port=ofp.OFPP_ANY,
                                    out_group=ofp.OFPG_ANY,
                                    cookie=s["cookie"],
                                    cookie_mask=COOKIE_MASK_ALL,
                                    match=parser.OFPMatch())
            dp.send_msg(mod)

    # ------------------------------------------------------------------
    # Network load: poll port statistics, compute Mbps per server port
    # ------------------------------------------------------------------
    def _stats_loop(self):
        while True:
            hub.sleep(STATS_INTERVAL)
            for dp in list(self.datapaths.values()):
                ofp, parser = dp.ofproto, dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            self._log_loads()

    def _log_loads(self):
        parts = []
        for n in self.order:
            s = self.servers[n]
            if not s["alive"]:
                parts.append("%s:DEAD" % n)
            else:
                parts.append("%s:%d (%.2fMbps)" % (n, self._eff_conns(s),
                                                  s["net_mbps"]))
        self.logger.info("[LOAD] %s", "  ".join(parts))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        port_to_server = {self._server_port(dp.id, s): s
                          for s in self.servers.values()}
        for st in ev.msg.body:
            s = port_to_server.get(st.port_no)
            if s is None:
                continue
            total = st.rx_bytes + st.tx_bytes
            key = (dp.id, st.port_no)
            prev = self.prev_port_bytes.get(key)
            if prev is not None:
                prev_bytes, prev_time = prev
                dt = now - prev_time
                if dt > 0:
                    s["net_mbps"] = max(0, total - prev_bytes) * 8 / dt / 1e6
            self.prev_port_bytes[key] = (total, now)

    # ------------------------------------------------------------------
    # Switch connects / disconnects
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)
        self.logger.info("Switch %s connected, table-miss rule installed",
                         dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER,
                                                DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ------------------------------------------------------------------
    # OpenFlow helpers
    # ------------------------------------------------------------------
    def add_flow(self, dp, priority, match, actions, idle=0, cookie=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle)
        dp.send_msg(mod)

    def send_packet(self, dp, in_port, actions, data):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    def _server_port(self, dpid, s):
        return self.mac_to_port.get(dpid, {}).get(s["mac"], s["port"])

    # ------------------------------------------------------------------
    # THE ADAPTIVE DECISION
    # ------------------------------------------------------------------
    def pick_server(self):
        """Return (server, reason). Uses live load, network load and health."""
        all_servers = [self.servers[n] for n in self.order]
        alive = [s for s in all_servers if s["alive"]]

        if alive:
            pool, reason = alive, "lowest score"
        else:
            pool, reason = all_servers, "NO healthy server -> fallback"

        eligible = [s for s in pool if not s["overloaded"]]
        if not eligible:
            eligible = pool
            if alive:
                reason = "all overloaded -> least loaded fallback"

        n = len(self.order)
        best = min(eligible,
                   key=lambda s: (round(self._score(s), 6),
                                  (s["idx"] - self.tie_pointer) % n))
        self.tie_pointer = (best["idx"] + 1) % n
        return best, reason

    # ------------------------------------------------------------------
    # ARP for the VIP: answer with the virtual MAC so clients can reach it
    # ------------------------------------------------------------------
    def reply_arp_for_vip(self, dp, in_port, eth, arp_pkt):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=eth.src, src=VMAC))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY,
                                 src_mac=VMAC, src_ip=VIP,
                                 dst_mac=arp_pkt.src_mac,
                                 dst_ip=arp_pkt.src_ip))
        pkt.serialize()
        actions = [dp.ofproto_parser.OFPActionOutput(in_port)]
        self.send_packet(dp, dp.ofproto.OFPP_CONTROLLER, actions, pkt.data)
        self.logger.info("[ARP] %s asked for VIP -> replied with virtual MAC",
                         arp_pkt.src_ip)

    # ------------------------------------------------------------------
    # TCP connection to the VIP: choose server, install rules
    # ------------------------------------------------------------------
    def handle_vip_tcp(self, dp, msg, in_port, eth, ip_pkt, tcp_pkt):
        parser = dp.ofproto_parser
        key = (ip_pkt.src, tcp_pkt.src_port)
        client = "%s:%d" % key
        is_syn = bool(tcp_pkt.bits & tcp.TCP_SYN)

        # A non-SYN packet of a known connection keeps its server (TCP cannot
        # move mid-connection) as long as that server is still alive.
        server = None
        if not is_syn and key in self.conn_table:
            cand = self.servers[self.conn_table[key]]
            if cand["alive"]:
                server = cand

        if server is None:
            loads = self._loads_str()                 # state used to decide
            server, reason = self.pick_server()
            self._log_decision(client, server["name"], reason)
            server["pending"] += 1                    # +1 correction
            self._check_overload(server)
            self.conn_table[key] = server["name"]
            self.assign_count[server["name"]] += 1
            self.logger.info("[PACKET-IN] %s -> VIP:%d  loads %s  => %s (%s)",
                             client, tcp_pkt.dst_port, loads,
                             server["name"], reason)

        client_mac = eth.src
        client_port = in_port
        server_port = self._server_port(dp.id, server)

        # Forward rule: client -> VIP  ==>  client -> server
        fwd_match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=6,
                                    ipv4_src=ip_pkt.src, ipv4_dst=VIP,
                                    tcp_src=tcp_pkt.src_port,
                                    tcp_dst=SERVICE_PORT)
        fwd_actions = [parser.OFPActionSetField(eth_dst=server["mac"]),
                       parser.OFPActionSetField(ipv4_dst=server["ip"]),
                       parser.OFPActionOutput(server_port)]
        self.add_flow(dp, FLOW_PRIORITY, fwd_match, fwd_actions,
                      IDLE_TIMEOUT, server["cookie"])

        # Reverse rule: server -> client  ==>  VIP -> client
        rev_match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=6,
                                    ipv4_src=server["ip"], ipv4_dst=ip_pkt.src,
                                    tcp_src=SERVICE_PORT,
                                    tcp_dst=tcp_pkt.src_port)
        rev_actions = [parser.OFPActionSetField(eth_src=VMAC),
                       parser.OFPActionSetField(ipv4_src=VIP),
                       parser.OFPActionSetField(eth_dst=client_mac),
                       parser.OFPActionOutput(client_port)]
        self.add_flow(dp, FLOW_PRIORITY, rev_match, rev_actions,
                      IDLE_TIMEOUT, server["cookie"])

        self.logger.info("[FLOW-INSTALLED] %s <-> %s  idle_timeout=%ds",
                         client, server["name"], IDLE_TIMEOUT)

        # Send the packet that triggered this, already rewritten
        self.send_packet(dp, in_port, fwd_actions, msg.data)

    # ------------------------------------------------------------------
    # Main packet-in handler
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Learn where this MAC lives (needed for normal L2 forwarding)
        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port

        # 1) ARP asking for the VIP -> we answer
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST \
                and arp_pkt.dst_ip == VIP:
            self.reply_arp_for_vip(dp, in_port, eth, arp_pkt)
            return

        # 2) TCP to the VIP service -> load balance
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        if ip_pkt and tcp_pkt and ip_pkt.dst == VIP \
                and tcp_pkt.dst_port == SERVICE_PORT:
            self.handle_vip_tcp(dp, msg, in_port, eth, ip_pkt, tcp_pkt)
            return

        # 3) Everything else (incl. load reports) -> ordinary learning switch
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst,
                                    eth_src=eth.src)
            self.add_flow(dp, 1, match, actions, 60)
        self.send_packet(dp, in_port, actions, msg.data)
