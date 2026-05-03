#!/usr/bin/env python3
"""
fcn_model.py — Fully Convolutional Network (FCN) for P4CCI CCA Classification.

Architecture (from paper Section IV-B):
  - 3 Convolutional blocks: filters={128, 256, 128}, kernels={8, 5, 3}
  - Each block: Conv1d -> BatchNorm1d -> ReLU
  - Global Average Pooling (GAP)
  - Softmax output (2 classes: Loss-based vs Model-based)

Training (from paper):
  - Adam optimizer, lr=0.005
  - Categorical Cross-entropy loss
  - 100 epochs
  - Input sequence length L=20 BIF samples (paper Section IV-A)

Classes (paper Section III-F):
  0 = Loss-based (CUBIC, Reno, Westwood, Illinois, Vegas)
  1 = Model-based (BBR)
"""

import os
import math
import random

# -- PyTorch (optional — graceful fallback if not installed) ------------------
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import TensorDataset, DataLoader
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

# -- Sequence length as specified in paper ------------------------------------
SEQ_LENGTH = 20          # L = 20 BIF samples per classification window
NUM_CLASSES = 2          # Loss-based (0) vs Model-based (1)
LEARNING_RATE = 0.005    # Adam optimizer lr from paper
NUM_EPOCHS = 100         # Training epochs from paper
BATCH_SIZE = 32


# ------------------------------------------------------------------------------
# FCN Model Definition
# ------------------------------------------------------------------------------

if TORCH_AVAILABLE:
    class FCN(nn.Module):
        """
        1D Fully Convolutional Network as described in P4CCI (Section IV-B).

        Input shape:  (batch_size, 1, SEQ_LENGTH)   i.e., (N, 1, 20)
        Output shape: (batch_size, NUM_CLASSES)      i.e., (N, 2)
        """

        def __init__(self, num_classes=NUM_CLASSES, in_channels=1):
            super(FCN, self).__init__()

            # Block 1: 128 filters, kernel=8
            self.conv1 = nn.Conv1d(in_channels, 128, kernel_size=8, padding='same')
            self.bn1   = nn.BatchNorm1d(128)

            # Block 2: 256 filters, kernel=5
            self.conv2 = nn.Conv1d(128, 256, kernel_size=5, padding='same')
            self.bn2   = nn.BatchNorm1d(256)

            # Block 3: 128 filters, kernel=3
            self.conv3 = nn.Conv1d(256, 128, kernel_size=3, padding='same')
            self.bn3   = nn.BatchNorm1d(128)

            # Output fully-connected layer (after GAP -> 128-dim vector)
            self.fc = nn.Linear(128, num_classes)

        def forward(self, x):
            # Block 1
            x = F.relu(self.bn1(self.conv1(x)))
            # Block 2
            x = F.relu(self.bn2(self.conv2(x)))
            # Block 3
            x = F.relu(self.bn3(self.conv3(x)))
            # Global Average Pooling: mean across time dimension (paper Section IV-B)
            x = x.mean(dim=2)           # (N, 128)
            # Output: return RAW LOGITS. CrossEntropyLoss applies LogSoftmax internally.
            # For inference, apply softmax explicitly via predict_single().
            x = self.fc(x)              # (N, 2)
            return x

    def build_model(num_classes=NUM_CLASSES):
        """Return a fresh (untrained) FCN model instance."""
        return FCN(num_classes=num_classes)

else:
    class FCN:  # type: ignore
        """Stub when PyTorch is unavailable."""
        def __init__(self, *a, **kw):
            raise ImportError("PyTorch not available — install torch to use FCN.")

    def build_model(num_classes=NUM_CLASSES):
        raise ImportError("PyTorch not available — install torch to use FCN.")


# ------------------------------------------------------------------------------
# Synthetic BIF Dataset Generation
# ------------------------------------------------------------------------------

def _generate_cubic_bif(n_samples, rng, bw_scale=1.0, noise_scale=500):
    """
    CUBIC: sawtooth window growth followed by multiplicative decrease on loss.
    High variance BIF -> Loss-based (label=0).
    """
    bif_vals = []
    for i in range(n_samples):
        ramp = (i % 20) + 1
        bif  = ramp * 1460 * bw_scale + rng.gauss(0, noise_scale)
        bif_vals.append(max(0.0, bif))
    return bif_vals


def _generate_bbr_bif(n_samples, rng, bw_scale=1.0, noise_scale=200):
    """
    BBR: rate-paced, near-constant BIF.
    Low variance -> Model-based (label=1).
    """
    base = 8000 * bw_scale
    return [max(0.0, base + rng.gauss(0, noise_scale)) for _ in range(n_samples)]


def _generate_reno_bif(n_samples, rng, bw_scale=1.0, noise_scale=400):
    """
    Reno: AIMD sawtooth similar to CUBIC but steeper drops.
    Loss-based (label=0).
    """
    bif_vals = []
    for i in range(n_samples):
        ramp = (i % 15) + 1
        bif  = ramp * 1200 * bw_scale + rng.gauss(0, noise_scale)
        bif_vals.append(max(0.0, bif))
    return bif_vals


def generate_synthetic_dataset(n_flows_per_class=500, seq_len=SEQ_LENGTH, seed=42):
    """
    Generate synthetic BIF time-series for FCN training.

    Diversity follows paper Section V-A:
      - BW: 500 Mbps – 10 Gbps   (represented as bw_scale 0.5–10.0)
      - RTT: 10–100 ms            (affects noise magnitude)
      - Packet loss: 0–0.5%       (affects BIF drop frequency)

    Returns:
        X: list of [seq_len] float lists   (one per sample)
        y: list of int labels              (0=loss-based, 1=model-based)
    """
    rng = random.Random(seed)
    X, y = [], []

    generators = [
        (_generate_cubic_bif, 0),   # Loss-based
        (_generate_reno_bif,  0),   # Loss-based
        (_generate_bbr_bif,   1),   # Model-based
    ]

    for gen_fn, label in generators:
        n = n_flows_per_class
        for _ in range(n):
            bw_scale    = rng.uniform(0.5, 10.0)
            noise_scale = rng.uniform(100, 1000)
            bif = gen_fn(seq_len, rng, bw_scale=bw_scale, noise_scale=noise_scale)
            X.append(bif)
            y.append(label)

    # Shuffle
    combined = list(zip(X, y))
    rng.shuffle(combined)
    X, y = zip(*combined)
    return list(X), list(y)


# ------------------------------------------------------------------------------
# Preprocessing helpers (match controller.py pipeline)
# ------------------------------------------------------------------------------

def _median(data):
    s = sorted(data)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 != 0 else (s[mid - 1] + s[mid]) / 2.0

def _mean(data):
    return sum(data) / len(data)

def _std(data):
    mu = _mean(data)
    return math.sqrt(sum((x - mu) ** 2 for x in data) / len(data))

def mad_outlier_rejection(ts, threshold=3.5):
    if len(ts) < 2:
        return ts[:]
    median = _median(ts)
    mad    = _median([abs(x - median) for x in ts])
    if mad == 0:
        return ts[:]
    filtered = [x for x in ts if abs(0.6745 * (x - median) / mad) <= threshold]
    return filtered if filtered else ts[:]

def z_normalize(ts):
    mu = _mean(ts)
    sigma = _std(ts)
    if sigma == 0:
        return [0.0] * len(ts)
    return [(x - mu) / sigma for x in ts]

def pad_or_truncate(ts, length, fill_val=0.0):
    if len(ts) < length:
        ts = ts + [fill_val] * (length - len(ts))
    return ts[:length]

def preprocess(raw_bif, seq_len=SEQ_LENGTH):
    """
    Full preprocessing pipeline (paper Section IV-A):
      1. MAD outlier rejection
      2. Pad/truncate to seq_len
      3. Z-normalization
    Returns list of float (length = seq_len).
    """
    ts = mad_outlier_rejection(raw_bif)
    fill = _median(ts) if ts else 0.0
    ts   = pad_or_truncate(ts, seq_len, fill_val=fill)
    ts   = z_normalize(ts)
    return ts


# ------------------------------------------------------------------------------
# Training
# ------------------------------------------------------------------------------

def train_fcn(X_train, y_train, X_val=None, y_val=None,
              num_classes=NUM_CLASSES, epochs=NUM_EPOCHS,
              lr=LEARNING_RATE, batch_size=BATCH_SIZE, verbose=True):
    """
    Train the FCN on BIF time-series data.

    Args:
        X_train: list of sequences (each length SEQ_LENGTH) — raw or preprocessed
        y_train: list of int labels (0=loss-based, 1=model-based)
        X_val, y_val: optional validation set
        verbose: print per-epoch loss/accuracy

    Returns:
        Trained FCN model (in eval mode).
    """
    if not TORCH_AVAILABLE:
        raise ImportError("PyTorch required for training. Install with: pip install torch")

    def to_tensor(X, y):
        # Shape: (N, 1, L)
        xt = torch.tensor([[preprocess(s)] for s in X], dtype=torch.float32)
        yt = torch.tensor(y, dtype=torch.long)
        return xt, yt

    X_t, y_t = to_tensor(X_train, y_train)
    loader = DataLoader(TensorDataset(X_t, y_t), batch_size=batch_size, shuffle=True)

    model     = FCN(num_classes=num_classes)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.CrossEntropyLoss()

    # Paper: "The learning rate was reduced when the optimizer reached a plateau."
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', factor=0.5, patience=10
    )

    if X_val is not None:
        X_v, y_v = to_tensor(X_val, y_val)

    model.train()
    for epoch in range(1, epochs + 1):
        total_loss, correct, total = 0.0, 0, 0
        for xb, yb in loader:
            optimizer.zero_grad()
            logits = model(xb)
            loss   = criterion(logits, yb)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * len(yb)
            correct    += (logits.argmax(dim=1) == yb).sum().item()
            total      += len(yb)

        avg_loss = total_loss / total
        scheduler.step(avg_loss)   # reduce lr on plateau

        if verbose and (epoch % 10 == 0 or epoch == 1):
            acc = correct / total * 100
            cur_lr = optimizer.param_groups[0]['lr']
            msg = f"  Epoch {epoch:3d}/{epochs} | Loss={avg_loss:.4f} | Train Acc={acc:.1f}% | lr={cur_lr:.6f}"
            if X_val is not None:
                model.eval()
                with torch.no_grad():
                    val_logits = model(X_v)
                    val_acc    = (val_logits.argmax(dim=1) == y_v).float().mean().item() * 100
                msg += f" | Val Acc={val_acc:.1f}%"
                model.train()
            print(msg)

    model.eval()
    return model


# ------------------------------------------------------------------------------
# Save / Load
# ------------------------------------------------------------------------------

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fcn_model.pth')

def save_model(model, path=MODEL_PATH):
    if not TORCH_AVAILABLE:
        raise ImportError("PyTorch required.")
    torch.save(model.state_dict(), path)
    print(f"[FCN] Model saved -> {path}")

def load_model(path=MODEL_PATH, num_classes=NUM_CLASSES):
    if not TORCH_AVAILABLE:
        raise ImportError("PyTorch required.")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No trained model found at {path}. Run train first.")
    model = FCN(num_classes=num_classes)
    model.load_state_dict(torch.load(path, map_location='cpu'))
    model.eval()
    print(f"[FCN] Model loaded <- {path}")
    return model

def predict_single(model, raw_bif, seq_len=SEQ_LENGTH):
    """
    Run inference on a single BIF time-series.
    Returns: (class_id: int, confidence: float)
    """
    ts = preprocess(raw_bif, seq_len=seq_len)
    x  = torch.tensor([[ts]], dtype=torch.float32)   # (1, 1, L)
    with torch.no_grad():
        logits = model(x)                              # (1, 2)
        probs  = F.softmax(logits, dim=1)              # apply softmax for inference
    class_id   = probs.argmax(dim=1).item()
    confidence = probs.max(dim=1).values.item()
    return class_id, confidence


# ------------------------------------------------------------------------------
# CLI entry point — train and self-test
# ------------------------------------------------------------------------------

if __name__ == '__main__':
    print("=" * 60)
    print("P4CCI FCN Model — Train & Self-Test")
    print("=" * 60)

    print("\n[1] Generating synthetic BIF dataset...")
    X, y = generate_synthetic_dataset(n_flows_per_class=300, seq_len=SEQ_LENGTH)
    split = int(0.8 * len(X))
    X_train, y_train = X[:split], y[:split]
    X_val,   y_val   = X[split:], y[split:]
    print(f"    Train: {len(X_train)}  |  Val: {len(X_val)}")
    print(f"    Classes — 0 (loss-based): {y_train.count(0)}  |  1 (model-based): {y_train.count(1)}")

    if not TORCH_AVAILABLE:
        print("\n[!] PyTorch not available. Skipping FCN training.")
        print("    Install with: pip install torch")
    else:
        print(f"\n[2] Training FCN ({NUM_EPOCHS} epochs, Adam lr={LEARNING_RATE})...")
        model = train_fcn(X_train, y_train, X_val, y_val, verbose=True)

        print("\n[3] Saving model...")
        save_model(model)

        print("\n[4] Self-test on 4 synthetic flows:")
        test_cases = [
            ('CUBIC sawtooth',   _generate_cubic_bif(SEQ_LENGTH, random.Random(1)), 0),
            ('Reno sawtooth',    _generate_reno_bif(SEQ_LENGTH,  random.Random(2)), 0),
            ('BBR paced',        _generate_bbr_bif(SEQ_LENGTH,   random.Random(3)), 1),
            ('BBR paced (2)',    _generate_bbr_bif(SEQ_LENGTH,   random.Random(4)), 1),
        ]
        labels = {0: 'Loss-based (CUBIC/Reno)', 1: 'Model-based (BBR)'}
        all_correct = True
        for name, bif, expected in test_cases:
            pred, conf = predict_single(model, bif)
            correct = '[OK]' if pred == expected else '[FAIL]'
            print(f"    {correct} {name:<22} -> {labels[pred]} (conf={conf:.3f})")
            if pred != expected:
                all_correct = False

        print(f"\n{'[+] All self-tests passed!' if all_correct else '[!] Some tests failed — check thresholds.'}")
