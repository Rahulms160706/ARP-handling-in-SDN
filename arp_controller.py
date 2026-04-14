from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import packet, ethernet, arp
from ryu.ofproto import ofproto_v1_3


class ARPController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ARPController, self).__init__(*args, **kwargs)
        self.arp_table = {}      # IP → MAC
        self.mac_to_port = {}    # MAC → Port

    # Table-miss rule (send unknown packets to controller)
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]

        self.add_flow(datapath, 0, match, actions)

    # Function to add flow rules
    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)

    # Install flow rule for known paths
    def install_flow(self, datapath, in_port, dst, actions):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch(in_port=in_port, eth_dst=dst)

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=1,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)

    # Main packet handler
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        # Learn MAC → Port
        self.mac_to_port[dpid][eth.src] = in_port

        # ARP Handling
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:
            src_ip = arp_pkt.src_ip
            src_mac = arp_pkt.src_mac
            dst_ip = arp_pkt.dst_ip

            # Learn IP → MAC
            self.arp_table[src_ip] = src_mac
            print(f"[ARP LEARNED] {src_ip} → {src_mac}")

            # Proxy ARP
            if dst_ip in self.arp_table:
                dst_mac = self.arp_table[dst_ip]

                print(f"[PROXY ARP] {dst_ip} is at {dst_mac}")

                arp_reply = packet.Packet()
                arp_reply.add_protocol(ethernet.ethernet(
                    ethertype=eth.ethertype,
                    dst=src_mac,
                    src=dst_mac
                ))
                arp_reply.add_protocol(arp.arp(
                    opcode=arp.ARP_REPLY,
                    src_mac=dst_mac,
                    src_ip=dst_ip,
                    dst_mac=src_mac,
                    dst_ip=src_ip
                ))

                arp_reply.serialize()

                actions = [parser.OFPActionOutput(in_port)]

                out = parser.OFPPacketOut(
                    datapath=datapath,
                    buffer_id=ofproto.OFP_NO_BUFFER,
                    in_port=ofproto.OFPP_CONTROLLER,
                    actions=actions,
                    data=arp_reply.data
                )
                datapath.send_msg(out)

                return

        # Forwarding logic
        if eth.dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][eth.dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # Install flow rule for known destination
        if out_port != ofproto.OFPP_FLOOD:
            self.install_flow(datapath, in_port, eth.dst, actions)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)
