# Evaluation Report: P4CCI Implementation

## 1. Evaluation Objective
The primary goal of the evaluation is to measure the effectiveness of the **P4CCI (P4-Based Online TCP Congestion Control Algorithm Identification)** architecture in identifying TCP flows (CUBIC vs. BBR) and applying traffic separation via queue assignment. The evaluation focuses on **Fairness** and **Throughput** at a shared network bottleneck.

## 2. Network Topology: Dumbbell Topology
The implementation uses a standard dumbbell topology simulated in Mininet to create controlled congestion:
*   **Senders**: 
    *   `h1`: Configured with **TCP CUBIC**.
    *   `h2`: Configured with **TCP BBR**.
*   **Switch (`s1`)**: A P4-enabled BMv2 switch running the `p4cci.p4` data plane.
*   **Receiver (`h3`)**: A shared destination for both flows.
*   **Bottleneck Link**: The link between `s1` and `h3` is throttled to **10 Mbps** with a **20ms** one-way delay to induce contention.

## 3. Traffic Profile
*   **Tool**: `iperf3` is used to generate parallel TCP flows.
*   **Test Duration**: Standardized at **30 seconds** to allow TCP congestion control algorithms to reach a steady state.
*   **Workflow**:
    1.  `h3` launches two `iperf3` servers on different ports.
    2.  `h1` and `h2` launch simultaneous clients targeting `h3`.
    3.  The P4 switch extracts **Bytes-in-Flight (BIF)** telemetry and sends it to the controller.
    4.  The controller identifies the CCA using the **FCN model** and assigns `h1` and `h2` to separate hardware queues.

## 4. Key Performance Metrics
| Metric | Description |
| :--- | :--- |
| **Throughput (Mbps)** | Measured for each individual flow to see how much of the 10 Mbps bottleneck each captures. |
| **Jain's Fairness Index (JFI)** | A mathematical measure of fairness. A result of **1.0** indicates perfect fairness (5 Mbps each), while **0.5** indicates total dominance by one flow. |
| **Retransmissions** | Monitored to assess the severity of congestion and packet loss for each algorithm. |
| **Classification Accuracy** | Logged by the controller to verify that the FCN model correctly identifies CUBIC and BBR. |

## 5. Verification Workflow
The project includes an automated verification pipeline via the `Makefile` and `topology.py`:
1.  **Diagnostic Check**: A 5-second single-flow `iperf3` test is performed before the main test to ensure connectivity.
2.  **Traffic Test**: Runs the full 30s parallel test.
3.  **Result Aggregation**: `topology.py` parses the `iperf3` JSON output and calculates the Fairness Index automatically.
4.  **Reporting**: Results are saved to `results/last_test.json` for persistent record-keeping.

## 6. Summary of Results Handling
After each run, the system outputs a summary similar to this:
```json
{
  "cubic_mbps": 4.85,
  "bbr_mbps": 4.92,
  "jain_index": 0.9995,
  "duration_s": 30
}
```
