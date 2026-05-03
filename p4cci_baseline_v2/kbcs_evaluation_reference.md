# KBCS Evaluation Methodology Report

This report details exactly how the evaluation, testing, and metric collection are carried out in the KBCS project. You can use this as a reference framework to evaluate your P4CCI implementation under identical conditions for a fair, 1-to-1 comparison.

## 1. Topologies and Bottleneck Configuration
KBCS evaluates performance across two main topologies built in Mininet:
*   **Dumbbell Topology:** 
    *   **Nodes:** 4 senders (`h1`-`h4`) and 4 receivers (`h5`-`h8`).
    *   **Switches:** 2 switches (`S1`, `S2`).
    *   **Bottleneck:** The link between `S1` and `S2` is rate-limited in the switch to **250 packets per second (~3 Mbps)**.
*   **Cross Topology:** A more complex setup with 8 senders and 4 receivers across 4 switches, with multiple intersecting bottlenecks.

## 2. Traffic Generation Profile
Instead of a single 30-second run (like your current P4CCI setup), KBCS emphasizes **long-running and statistically significant** tests.
*   **Tool:** `iperf` (version 2, typically launched via `iperf -c <ip> -t <duration> -P 1`).
*   **Duration:** 
    *   **Single Experiments:** Default is **300 seconds** (5 minutes) to observe long-term convergence and Q-Learning agent behavior.
    *   **Statistical Suite:** Default is **60 seconds** per run.
*   **CCAs Used:** The system explicitly loads multiple congestion control modules (`tcp_bbr`, `tcp_vegas`, `tcp_illinois`, and default `tcp_cubic`) via `modprobe` to guarantee heterogeneous traffic.

## 3. The 30-Run Statistical Suite (`test_suite.sh`)
To rule out variance, KBCS does not rely on a single test. It uses an automated shell script (`test_suite.sh`) to run the experiment 30 times sequentially.
**Workflow per run:**
1.  **Clean State:** Kills Mininet (`mn -c`), clears old PCAPs and logs.
2.  **Start Topology:** Launches Mininet with the compiled `kbcs_v2.json` P4 code.
3.  **Apply Rate Limits:** Uses `simple_switch_CLI` to set the queue rate on the bottleneck ports to 250 pps.
4.  **Inject Traffic:** Starts the background `iperf` servers, waits 3 seconds, and then launches the 4 clients simultaneously.
5.  **Start Controller:** Launches the RL controller (`rl_controller.py`) in the background to dynamically adjust switch parameters.
6.  **Run & Wait:** Sleeps for the duration of the test (e.g., 60 seconds).
7.  **Collect Metrics:** Executes `collect_metrics.py` (see section 4) and kills background processes.

## 4. Metric Collection (Directly from Data Plane)
Unlike P4CCI, which parses `iperf3` JSON outputs, KBCS calculates its performance metrics by **reading P4 registers directly from the switch** via Thrift (`simple_switch_CLI`) at the end of the test. 

The script `collect_metrics.py` reads:
*   `MyIngress.reg_forwarded_bytes` (Total bytes successfully forwarded per flow)
*   `MyEgress.reg_drops` (Total packets dropped per flow)
*   `MyIngress.reg_karma` (Final karma score of the flow)

From these hardware-level registers, it computes:
*   **Throughput (Mbps):** `(Forwarded_Bytes * 8) / (Duration * 1,000,000)`
*   **Jain's Fairness Index (JFI):** Calculated purely based on the `Forwarded_Bytes` of each flow.
*   **Link Utilization (%):** `(Aggregate_Throughput / 3.0 Mbps) * 100`
*   **Packet Drop Ratio (PDR %):** `(Dropped_Packets * 1500) / (Forwarded_Bytes + (Dropped_Packets * 1500)) * 100`

## 5. Storage and Analysis
All calculated metrics are appended as a single row to a CSV file (`results/dumbbell_results.csv` or `cross_results.csv`). The CSV columns include:
`run, topology, duration, jfi, agg_throughput_mbps, link_util_pct, pdr_pct, avg_karma, [individual flow metrics]`

After all 30 runs complete, a separate script (`analyze_results.py`) is used to generate averages, medians, and standard deviations for the final research paper.

## How to adapt P4CCI to match this methodology:
If you want a 1-to-1 comparison between KBCS and P4CCI, you should update your P4CCI evaluation to:
1.  **Drop the 10 Mbps / 20ms Mininet bottleneck:** Instead, use BMv2 queue rate limiting (e.g., `set_queue_rate 250`) to create a strict 3 Mbps bottleneck at the switch level.
2.  **Increase Flow Count:** Test with 4 concurrent flows (e.g., 2 CUBIC, 2 BBR) instead of just 2.
3.  **Use Hardware Metrics:** Instead of relying on `iperf3` JSON, add P4 registers in P4CCI to track forwarded bytes and drops per flow, and read them via Thrift at the end of the test to calculate JFI and PDR.
4.  **Run a Statistical Suite:** Wrap your P4CCI test in a bash script that runs it 30 times, clears Mininet in between, and appends the results to a CSV file.
