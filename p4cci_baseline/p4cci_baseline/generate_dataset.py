#!/usr/bin/env python3
"""
generate_dataset.py — Synthetic BIF dataset generator for P4CCI FCN training.

Follows the dataset description in paper Section V-A:
  - BW: 500 Mbps – 10 Gbps
  - RTT: 10 ms – 100 ms
  - Packet loss: 0% – 0.5%

CCA patterns generated:
  - CUBIC  (loss-based,  label=0): sawtooth window ramp with drops
  - Reno   (loss-based,  label=0): AIMD sawtooth, steeper drops
  - BBR    (model-based, label=1): rate-paced, near-constant BIF

Usage:
    python3 generate_dataset.py [--flows N] [--outdir DIR] [--seed S]
"""

import os
import math
import random
import json
import argparse

# Use numpy if available for faster I/O, else use plain lists
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

SEQ_LENGTH = 20   # L = 20 per paper Section IV-A


# ──────────────────────────────────────────────────────────────────────────────
# BIF generators per CCA pattern
# ──────────────────────────────────────────────────────────────────────────────

def generate_cubic_bif(seq_len, rng, bw_mbps=1000.0, rtt_ms=20.0, loss_pct=0.0):
    """
    CUBIC: Concave window growth with CWND halving on loss events.
    Higher BW and RTT → larger absolute BIF values.
    Loss causes sudden BIF drops (sawtooth).
    """
    # Bytes per slot roughly proportional to BW*RTT (bandwidth-delay product)
    bdp = (bw_mbps * 1e6 / 8) * (rtt_ms / 1000.0)
    window_period = max(5, int(20 / (1 + loss_pct * 20)))  # loss shrinks window period

    bif_vals = []
    for i in range(seq_len):
        ramp = (i % window_period) + 1
        base = (ramp / window_period) * bdp * 0.8
        noise = rng.gauss(0, base * 0.1 + 500)
        bif_vals.append(max(0.0, base + noise))
    return bif_vals


def generate_reno_bif(seq_len, rng, bw_mbps=1000.0, rtt_ms=20.0, loss_pct=0.0):
    """
    Reno: Linear (AIMD) window growth, more frequent drops under loss.
    """
    bdp = (bw_mbps * 1e6 / 8) * (rtt_ms / 1000.0)
    window_period = max(3, int(15 / (1 + loss_pct * 30)))

    bif_vals = []
    for i in range(seq_len):
        ramp = (i % window_period) + 1
        base = (ramp / window_period) * bdp * 0.6
        noise = rng.gauss(0, base * 0.12 + 300)
        bif_vals.append(max(0.0, base + noise))
    return bif_vals


def generate_bbr_bif(seq_len, rng, bw_mbps=1000.0, rtt_ms=20.0, loss_pct=0.0):
    """
    BBR: Paced at estimated bottleneck bandwidth. BIF near-constant at BDP.
    Loss has little effect (BBR ignores loss as signal).
    """
    bdp = (bw_mbps * 1e6 / 8) * (rtt_ms / 1000.0)
    base = bdp * 0.9  # BBR aims to keep ~1 BDP in flight
    noise_scale = base * 0.02 + 150   # Very low noise

    return [max(0.0, base + rng.gauss(0, noise_scale)) for _ in range(seq_len)]


# ──────────────────────────────────────────────────────────────────────────────
# Full dataset generation
# ──────────────────────────────────────────────────────────────────────────────

GENERATORS = [
    (generate_cubic_bif, 0, 'CUBIC'),
    (generate_reno_bif,  0, 'Reno'),
    (generate_bbr_bif,   1, 'BBR'),
]


def generate_dataset(n_flows_per_class=500, seq_len=SEQ_LENGTH, seed=42, verbose=True):
    """
    Generate a balanced synthetic BIF dataset.

    Args:
        n_flows_per_class: Number of flow samples per CCA type
        seq_len: BIF sequence length (L=20 per paper)
        seed: Random seed for reproducibility

    Returns:
        X: list of float lists, shape (N, seq_len)
        y: list of int labels, 0=loss-based, 1=model-based
        meta: list of dicts with generation parameters
    """
    rng = random.Random(seed)
    X, y, meta = [], [], []

    for gen_fn, label, cca_name in GENERATORS:
        for i in range(n_flows_per_class):
            # Sample network parameters (paper Section V-A ranges)
            bw_mbps  = rng.uniform(500.0, 10000.0)    # 500 Mbps – 10 Gbps
            rtt_ms   = rng.uniform(10.0, 100.0)       # 10 – 100 ms
            loss_pct = rng.uniform(0.0, 0.005)        # 0 – 0.5%

            bif = gen_fn(seq_len, rng, bw_mbps=bw_mbps, rtt_ms=rtt_ms, loss_pct=loss_pct)
            X.append(bif)
            y.append(label)
            meta.append({
                'cca': cca_name,
                'label': label,
                'bw_mbps': round(bw_mbps, 1),
                'rtt_ms': round(rtt_ms, 2),
                'loss_pct': round(loss_pct * 100, 4),
            })

    # Shuffle all together
    combined = list(zip(X, y, meta))
    rng.shuffle(combined)
    X, y, meta = zip(*combined)

    if verbose:
        n_loss  = sum(1 for label in y if label == 0)
        n_model = sum(1 for label in y if label == 1)
        print(f"[Dataset] Total: {len(X)} samples | Loss-based: {n_loss} | Model-based: {n_model}")

    return list(X), list(y), list(meta)


def save_dataset(X, y, meta, outdir='.', prefix='p4cci_bif'):
    """Save dataset to files. Uses .npy if numpy available, else JSON."""
    os.makedirs(outdir, exist_ok=True)

    if NUMPY_AVAILABLE:
        x_path = os.path.join(outdir, f'{prefix}_X.npy')
        y_path = os.path.join(outdir, f'{prefix}_y.npy')
        np.save(x_path, np.array(X, dtype=np.float32))
        np.save(y_path, np.array(y, dtype=np.int32))
        print(f"[Dataset] Saved X → {x_path}  ({os.path.getsize(x_path)/1024:.1f} KB)")
        print(f"[Dataset] Saved y → {y_path}  ({os.path.getsize(y_path)/1024:.1f} KB)")
    else:
        x_path = os.path.join(outdir, f'{prefix}_X.json')
        y_path = os.path.join(outdir, f'{prefix}_y.json')
        with open(x_path, 'w') as f:
            json.dump(X, f)
        with open(y_path, 'w') as f:
            json.dump(y, f)
        print(f"[Dataset] Saved X → {x_path}")
        print(f"[Dataset] Saved y → {y_path}")

    # Always save metadata as JSON
    meta_path = os.path.join(outdir, f'{prefix}_meta.json')
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)
    print(f"[Dataset] Saved meta → {meta_path}")

    return x_path, y_path, meta_path


def load_dataset(outdir='.', prefix='p4cci_bif'):
    """Load dataset from files."""
    if NUMPY_AVAILABLE:
        x_path = os.path.join(outdir, f'{prefix}_X.npy')
        y_path = os.path.join(outdir, f'{prefix}_y.npy')
        if os.path.exists(x_path):
            X = np.load(x_path).tolist()
            y = np.load(y_path).tolist()
        else:
            raise FileNotFoundError(f"Dataset not found at {x_path}. Run generate first.")
    else:
        x_path = os.path.join(outdir, f'{prefix}_X.json')
        y_path = os.path.join(outdir, f'{prefix}_y.json')
        with open(x_path) as f:
            X = json.load(f)
        with open(y_path) as f:
            y = json.load(f)

    meta_path = os.path.join(outdir, f'{prefix}_meta.json')
    meta = []
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    print(f"[Dataset] Loaded {len(X)} samples from {outdir}")
    return X, y, meta


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='P4CCI BIF Dataset Generator')
    parser.add_argument('--flows',  type=int, default=500,
                        help='Number of flows per CCA class (default: 500)')
    parser.add_argument('--outdir', type=str, default='dataset',
                        help='Output directory (default: ./dataset)')
    parser.add_argument('--seed',   type=int, default=42,
                        help='Random seed (default: 42)')
    parser.add_argument('--seqlen', type=int, default=SEQ_LENGTH,
                        help=f'BIF sequence length, default={SEQ_LENGTH} (paper value)')
    args = parser.parse_args()

    print("=" * 60)
    print("P4CCI BIF Dataset Generator")
    print("=" * 60)
    print(f"  CCA classes   : CUBIC (loss=0), Reno (loss=0), BBR (model=1)")
    print(f"  Flows/class   : {args.flows}")
    print(f"  Sequence len  : {args.seqlen}")
    print(f"  BW range      : 500 Mbps – 10 Gbps")
    print(f"  RTT range     : 10 ms – 100 ms")
    print(f"  Loss range    : 0% – 0.5%")
    print(f"  Output dir    : {args.outdir}")
    print()

    X, y, meta = generate_dataset(
        n_flows_per_class=args.flows,
        seq_len=args.seqlen,
        seed=args.seed,
        verbose=True,
    )

    save_dataset(X, y, meta, outdir=args.outdir)

    # Print sample statistics
    print("\n--- Sample BIF statistics ---")
    for cca_name in ['CUBIC', 'Reno', 'BBR']:
        samples = [X[i] for i, m in enumerate(meta) if m['cca'] == cca_name]
        all_vals = [v for s in samples for v in s]
        mean_v = sum(all_vals) / len(all_vals)
        std_v  = math.sqrt(sum((v - mean_v)**2 for v in all_vals) / len(all_vals))
        print(f"  {cca_name:<6} | n={len(samples)} | mean BIF={mean_v/1e6:.2f} MB | std={std_v/1e6:.2f} MB")

    print("\n[Done] Dataset ready for FCN training via fcn_model.py")
