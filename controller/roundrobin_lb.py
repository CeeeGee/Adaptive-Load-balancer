"""
roundrobin_lb.py  -  BASELINE controller (static, load-blind)

Clients talk to one virtual service address (VIP). For every NEW TCP
connection to the VIP this controller picks the next server in a fixed
order (h4 -> h5 -> h6 -> h4 ...), ignoring load and health.

It installs two flow rules per connection:
  forward: client -> VIP     becomes   client -> chosen server
  reverse: server -> client  becomes   VIP    -> client
so the client never knows a real server exists.

Everything that is not traffic to the VIP is handled like a normal
learning switch, so ping, ARP and other traffic keep working.

Run:  ryu-manager controller/roundrobin_lb.py
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types

# ----------------------------------------------------------------------
# Config (must match topology/topo.py and app/server.py)
# ----------------------------------------------------------------------
VIP = "10.0.0.100"                 # virtual service address seen by clients
VMAC = "00:00:00:00:00:64"         # virtual MAC for the VIP
SERVICE_PORT = 8000                # TCP port of the service

# port = switch port the server is plugged into (h4 -> port 4, etc.)
# Requires topo.py to add links in order h1..h6 and to set these MACs.
SERVERS = [
    {"name": "h4", "ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "port": 4},
    {"name": "h5", "ip": "10.0.0.5", "mac": "00:00:00:00:00:05", "port": 5},
    {"name": "h6", "ip": "10.0.0.6", "mac": "00:00:00:00:00:06", "port": 6},
]

FLOW_PRIORITY = 100                # VIP flows (beat the L2 learning flows)
IDLE_TIMEOUT = 30                  # seconds of silence before a flow expires


class RoundRobinLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RoundRobinLB, self).__init__(*args, **kwargs)
        self.mac_to_port = {}          # dpid -> {mac: port}   (learning switch)
        self.next_server = 0           # round robin pointer
        self.conn_table = {}           # (client_ip, client_port) -> server dict
        self.assign_count = {s["name"]: 0 for s in SERVERS}
        self.logger.info("Controller started. VIP=%s:%d Policy=ROUND-ROBIN",
                         VIP, SERVICE_PORT)

    # ------------------------------------------------------------------
    # Switch connects: install the table-miss rule (send unknown to us)
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def add_flow(self, dp, priority, match, actions, idle=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                instructions=inst, idle_timeout=idle)
        dp.send_msg(mod)

    def send_packet(self, dp, in_port, actions, data):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    def pick_server(self):
        """Round robin: next server in fixed order, no load or health check."""
        server = SERVERS[self.next_server]
        self.next_server = (self.next_server + 1) % len(SERVERS)
        return server

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
    # New (or unknown) TCP connection to the VIP: choose server, add rules
    # ------------------------------------------------------------------
    def handle_vip_tcp(self, dp, msg, in_port, eth, ip_pkt, tcp_pkt):
        parser = dp.ofproto_parser
        key = (ip_pkt.src, tcp_pkt.src_port)
        is_syn = bool(tcp_pkt.bits & tcp.TCP_SYN)

        # A SYN is a brand new connection -> pick a server.
        # A non-SYN from a known connection (rule expired) keeps its server,
        # because TCP cannot move to another server mid-connection.
        if is_syn or key not in self.conn_table:
            server = self.pick_server()
            self.conn_table[key] = server
            self.assign_count[server["name"]] += 1
            self.logger.info("[PACKET-IN] %s:%d -> VIP:%d  => %s  (round-robin)"
                             "  totals=%s", ip_pkt.src, tcp_pkt.src_port,
                             tcp_pkt.dst_port, server["name"],
                             self.assign_count)
        else:
            server = self.conn_table[key]

        client_mac = eth.src
        client_port = in_port
        server_port = self.mac_to_port.get(dp.id, {}).get(server["mac"],
                                                         server["port"])

        # Forward rule: client -> VIP  ==>  client -> server
        fwd_match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=6,
                                    ipv4_src=ip_pkt.src, ipv4_dst=VIP,
                                    tcp_src=tcp_pkt.src_port,
                                    tcp_dst=SERVICE_PORT)
        fwd_actions = [parser.OFPActionSetField(eth_dst=server["mac"]),
                       parser.OFPActionSetField(ipv4_dst=server["ip"]),
                       parser.OFPActionOutput(server_port)]
        self.add_flow(dp, FLOW_PRIORITY, fwd_match, fwd_actions, IDLE_TIMEOUT)

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
        self.add_flow(dp, FLOW_PRIORITY, rev_match, rev_actions, IDLE_TIMEOUT)

        self.logger.info("[FLOW-INSTALLED] %s:%d <-> %s  idle_timeout=%ds",
                         ip_pkt.src, tcp_pkt.src_port, server["name"],
                         IDLE_TIMEOUT)

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

        # 3) Everything else -> ordinary learning switch
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst,
                                    eth_src=eth.src)
            self.add_flow(dp, 1, match, actions, 60)
        self.send_packet(dp, in_port, actions, msg.data)
