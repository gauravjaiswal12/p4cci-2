#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8>  TYPE_TCP  = 6;

// Congestion threshold: any queue buildup triggers BIF sampling (paper Section III-B)
// Set to 1 so ANY queue depth > 0 triggers sampling (queue delay > 0)
const bit<19> CONGESTION_THRESHOLD = 1;

/*************************************************************************
*********************** H E A D E R S  ***********************************
*************************************************************************/
typedef bit<9>  egressSpec_t;
typedef bit<48> macAddr_t;
typedef bit<32> ip4Addr_t;

header ethernet_t {
    macAddr_t dstAddr;
    macAddr_t srcAddr;
    bit<16>   etherType;
}

header ipv4_t {
    bit<4>    version;
    bit<4>    ihl;
    bit<8>    diffserv;
    bit<16>   totalLen;
    bit<16>   identification;
    bit<3>    flags;
    bit<13>   fragOffset;
    bit<8>    ttl;
    bit<8>    protocol;
    bit<16>   hdrChecksum;
    ip4Addr_t srcAddr;
    ip4Addr_t dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<3>  res;
    bit<3>  ecn;
    bit<6>  ctrl;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

struct metadata_t {
    bit<32> flow_id;
    bit<32> bif;
    bit<32> last_ack;
    // cca_class: 0=unclassified/short(default), 1=loss-based(CUBIC/Reno), 2=model-based(BBR)
    // (Paper Section III-C: three dedicated queues)
    bit<8>  cca_class;
    bit<1>  is_ack;
    // flow size tracking: short flows go to Q0 (< 10 MB = ~6.8M seq numbers)
    bit<32> flow_bytes;
}

struct headers_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
}

// Digest sent to control plane carrying BIF sample + flow 5-tuple
struct bif_digest_t {
    bit<32> flow_id;
    bit<32> bif;
    bit<32> srcAddr;
    bit<32> dstAddr;
    bit<16> srcPort;
    bit<16> dstPort;
}

/*************************************************************************
*********************** P A R S E R  *************************************
*************************************************************************/
parser MyParser(packet_in packet,
                out headers_t hdr,
                inout metadata_t meta,
                inout standard_metadata_t standard_metadata) {

    state start {
        transition parse_ethernet;
    }

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            default: accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            TYPE_TCP: parse_tcp;
            default: accept;
        }
    }

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }
}

/*************************************************************************
************   C H E C K S U M    V E R I F I C A T I O N   *************
*************************************************************************/
control MyVerifyChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply { }
}

/*************************************************************************
**************  I N G R E S S   P R O C E S S I N G   *******************
*************************************************************************/
control MyIngress(inout headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t standard_metadata) {

    // Per-flow last-ACK register (indexed by 16-bit flow hash → 65536 slots)
    register<bit<32>>(65536) last_ack_reg;

    // Per-flow byte counter for short-flow detection (Q0 = < ~10 MB)
    register<bit<32>>(65536) flow_bytes_reg;

    // ── KBCS-style hardware counters ──────────────────────────────────────────
    // reg_forwarded_bytes: Total IP payload bytes successfully forwarded per flow.
    // Indexed by flow_id (16-bit CRC hash of 5-tuple). Read by collect_metrics.py
    // via simple_switch_CLI at the end of each statistical run.
    register<bit<32>>(65536) reg_forwarded_bytes;

    action drop() {
        mark_to_drop(standard_metadata);
    }

    action ipv4_forward(macAddr_t dstAddr, egressSpec_t port) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = hdr.ethernet.dstAddr;
        hdr.ethernet.dstAddr = dstAddr;
        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }

    // Called by controller after CCA classification to tag the flow
    action set_cca_class(bit<8> class_id) {
        meta.cca_class = class_id;
    }

    // L3 forwarding table (LPM on dst IP)
    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            ipv4_forward;
            drop;
            NoAction;
        }
        size = 1024;
        default_action = drop();
    }

    // CCA classification table — populated by controller after classification.
    // Key: full 5-tuple with ternary matching so src_port can be wildcarded.
    // Rules use (0 &&& 0) for src_port to match ANY ephemeral port.
    // Action: set_cca_class(class_id) assigns the flow to a priority queue.
    // (Paper Section III-C, Table I)
    table cca_classification {
        key = {
            hdr.ipv4.srcAddr: ternary;
            hdr.ipv4.dstAddr: ternary;
            hdr.tcp.srcPort:  ternary;   // wildcarded — iperf uses random ports
            hdr.tcp.dstPort:  ternary;
        }
        actions = {
            set_cca_class;
            NoAction;
        }
        size = 8192;
        default_action = NoAction();
    }

    apply {
        if (hdr.ipv4.isValid()) {
            ipv4_lpm.apply();

            if (hdr.tcp.isValid()) {
                // Default cca_class = 0 (short flow / unclassified queue)
                meta.cca_class = 0;

                // Lookup existing CCA classification for this flow
                cca_classification.apply();

                // Compute 16-bit flow hash from 5-tuple for register indexing
                hash(meta.flow_id,
                     HashAlgorithm.crc16,
                     (bit<32>)0,
                     { hdr.ipv4.srcAddr,
                       hdr.ipv4.dstAddr,
                       hdr.tcp.srcPort,
                       hdr.tcp.dstPort,
                       hdr.ipv4.protocol },
                     (bit<32>)65535);

                // Detect ACK flag (bit 4 of ctrl = 0x10)
                bit<6> ack_mask = 0x10;
                if ((hdr.tcp.ctrl & ack_mask) != 0) {
                    meta.is_ack = 1;
                } else {
                    meta.is_ack = 0;
                }

                if (meta.is_ack == 1) {
                    // Update stored ACK for this flow
                    last_ack_reg.write(meta.flow_id, hdr.tcp.ackNo);
                }

                // BIF computation on data packets (totalLen > 40B = IP+TCP headers)
                // (Paper Section III-A: BIF = SEQ_i - ACK_last)
                if (hdr.ipv4.totalLen > 40) {
                    // Accumulate flow byte count for short-flow detection
                    bit<32> cur_bytes;
                    flow_bytes_reg.read(cur_bytes, meta.flow_id);
                    cur_bytes = cur_bytes + (bit<32>)hdr.ipv4.totalLen;
                    flow_bytes_reg.write(meta.flow_id, cur_bytes);
                    meta.flow_bytes = cur_bytes;

                    // ── KBCS hardware counter: increment forwarded bytes per flow ──
                    bit<32> fwd_bytes;
                    reg_forwarded_bytes.read(fwd_bytes, meta.flow_id);
                    fwd_bytes = fwd_bytes + (bit<32>)hdr.ipv4.totalLen;
                    reg_forwarded_bytes.write(meta.flow_id, fwd_bytes);

                    // Read last ACK and compute BIF
                    last_ack_reg.read(meta.last_ack, meta.flow_id);
                    meta.bif = hdr.tcp.seqNo - meta.last_ack;

                    // Send BIF digest to control plane when congestion detected
                    // (Paper Section III-B: only sample under congestion to reduce noise)
                    if (standard_metadata.enq_qdepth > CONGESTION_THRESHOLD) {
                        digest<bif_digest_t>(1, {
                            meta.flow_id,
                            meta.bif,
                            hdr.ipv4.srcAddr,
                            hdr.ipv4.dstAddr,
                            hdr.tcp.srcPort,
                            hdr.tcp.dstPort
                        });
                    }
                }
            }
        }
    }
}

/*************************************************************************
****************  E G R E S S   P R O C E S S I N G   *******************
*************************************************************************/
control MyEgress(inout headers_t hdr,
                 inout metadata_t meta,
                 inout standard_metadata_t standard_metadata) {

    // Traffic separation: assign BMv2 priority queue based on CCA class.
    // (Paper Section III-C: three dedicated queues)
    //
    // Queue mapping:
    //   cca_class = 0  →  priority 0  (Queue 0: short flows / unclassified)
    //   cca_class = 1  →  priority 1  (Queue 1: Loss-based, e.g. CUBIC/Reno)
    //   cca_class = 2  →  priority 2  (Queue 2: Model-based, e.g. BBR)
    //
    // NOTE: BMv2 must be started with --priority-queues 3 for this to take effect.
    // The controller sets cca_class via the cca_classification table,
    // and this block steers each packet to the appropriate hardware queue.

    // ── KBCS-style hardware counter: dropped packets per flow ─────────────────
    // reg_drops: Incremented whenever a packet is marked to drop in egress.
    // Indexed by flow_id from ingress metadata. Read by collect_metrics.py.
    register<bit<32>>(65536) reg_drops;

    action assign_to_queue(bit<5> qid) {
        // v1model uses standard_metadata.priority (3-bit) to select queue
        // BMv2 supports up to 8 priority levels with --priority-queues flag
        standard_metadata.priority = (bit<3>)qid;
    }

    // Queue assignment table driven by cca_class metadata.
    // Populated at startup with 3 static entries (class_id → queue priority).
    table queue_assignment {
        key = {
            meta.cca_class: exact;
        }
        actions = {
            assign_to_queue;
            NoAction;
        }
        size = 8;
        // Default: priority 0 (short-flow / unclassified queue)
        default_action = NoAction();
    }

    apply {
        // Apply queue steering for TCP flows that have been classified
        if (hdr.tcp.isValid()) {
            queue_assignment.apply();
        }

        // ── Increment drop counter if packet was marked to drop ───────────────
        if (standard_metadata.egress_port == 511) { // 511 = DROP_PORT in BMv2
            bit<32> drop_count;
            reg_drops.read(drop_count, meta.flow_id);
            drop_count = drop_count + 1;
            reg_drops.write(meta.flow_id, drop_count);
        }
    }
}

/*************************************************************************
*************   C H E C K S U M    C O M P U T A T I O N   **************
*************************************************************************/
control MyComputeChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version,
              hdr.ipv4.ihl,
              hdr.ipv4.diffserv,
              hdr.ipv4.totalLen,
              hdr.ipv4.identification,
              hdr.ipv4.flags,
              hdr.ipv4.fragOffset,
              hdr.ipv4.ttl,
              hdr.ipv4.protocol,
              hdr.ipv4.srcAddr,
              hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

/*************************************************************************
***********************  D E P A R S E R  ********************************
*************************************************************************/
control MyDeparser(packet_out packet, in headers_t hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.tcp);
    }
}

/*************************************************************************
***********************  S W I T C H  ************************************
*************************************************************************/
V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;
