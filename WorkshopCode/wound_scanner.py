"""
1D Wound Scanner IoT Simulator
Simulates a depth-profile scan of a wound, applies realistic noise,
plots the result, and optionally uploads to ThingSpeak.
"""

import json
import random

import matplotlib.pyplot as plt
import numpy as np
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CHANNEL_ID    = "3256717"
WRITE_API_KEY = "GI01L24DNG172U9B"
N_POINTS      = 500   # number of scan positions

THINGSPEAK_URL = (
    f"https://api.thingspeak.com/channels/{CHANNEL_ID}/bulk_update.json"
)

WOUND_TYPE_CODE = {"laceration": 1, "incision": 2, "abrasion": 3}

# Per-wound noise profiles — each wound has a PRIMARY noise type and two
# secondary ones.  Parameter ranges are inspired by sfimmy.ipynb:
#   HF vibration → sum of N sinusoids in [lb_freq, ub_freq] cycles/scan
#   Jitter       → N narrow Gaussian spikes at random positions
#   AWGN         → SNR-based power scaling (identical to notebook's awgn())
#
# Physical coupling rationale:
#   incision   → HF primary  (blade transmits hand tremor directly into the narrow cut)
#   laceration → AWGN primary (irregular crushed bed adds broadband measurement noise)
#   abrasion   → jitter primary (rough surface causes discrete positional artifacts)
NOISE_PROFILES = {
    "incision": {
        "hf": {                                              # PRIMARY
            "lb_freq":      (15.0, 30.0),   # cycles/scan
            "ub_freq":      (35.0, 60.0),
            "n_components": 42,             # matches notebook's add_noise_1
            "amplitude":    (0.30, 0.70),   # per-component peak (mm)
        },                                  # RMS sum ≈ amp × √(N/2) ≈ 1.4–3.2 mm
        "jitter": {                                          # secondary — few, narrow spikes
            "n_spikes":  (2, 5),
            "amp_scale": (0.1, 0.5),        # mm
            "width":     (0.3, 1.5),        # samples
        },
        "awgn":   {"snr_db": (30.0, 45.0)},                 # high SNR → barely audible
    },
    "laceration": {
        "hf": {                                              # secondary — weak
            "lb_freq":      (3.0, 8.0),
            "ub_freq":      (10.0, 20.0),
            "n_components": 15,
            "amplitude":    (0.01, 0.04),
        },
        "jitter": {                                          # secondary — moderate spikes
            "n_spikes":  (4, 8),
            "amp_scale": (0.5, 2.0),
            "width":     (0.5, 3.0),
        },
        "awgn":   {"snr_db": (8.0, 18.0)},                  # PRIMARY: low SNR → noisy
    },
    "abrasion": {
        "hf": {                                              # secondary — very weak
            "lb_freq":      (2.0, 6.0),
            "ub_freq":      (8.0, 15.0),
            "n_components": 10,
            "amplitude":    (0.005, 0.02),
        },
        "jitter": {                                          # PRIMARY — many wide spikes
            "n_spikes":  (12, 20),          # matches notebook's 7 repeated × duration
            "amp_scale": (0.05, 0.15),      # mm (large relative to shallow signal)
            "width":     (1.0, 5.0),        # samples — wider than incision
        },
        "awgn":   {"snr_db": (35.0, 50.0)},                 # very high SNR → clean baseline
    },
}

# ---------------------------------------------------------------------------
# Wound generators — paper-based models
# (Computational and Forensic Morphology of Traumatic Skin Wounds, 2026)
# ---------------------------------------------------------------------------

def _smooth_noise_1d(n: int, n_ctrl: int, rng: np.random.Generator) -> np.ndarray:
    """
    Single-octave smooth value noise: interpolate between n_ctrl random
    control points spread evenly over n samples.
    """
    n_ctrl = max(2, n_ctrl)
    ctrl_vals = rng.uniform(-1.0, 1.0, n_ctrl)
    ctrl_x    = np.linspace(0, n - 1, n_ctrl)
    return np.interp(np.arange(n, dtype=float), ctrl_x, ctrl_vals)


def _fractal_noise_1d(
    n: int,
    octaves: int      = 5,
    persistence: float = 0.5,
    base_ctrl_pts: int = 4,
    lacunarity: float  = 2.0,
) -> np.ndarray:
    """
    Multi-octave value noise — 1D equivalent of the fractal Perlin noise
    prescribed by the paper (pvigier/perlin-numpy approach).
    Each successive octave doubles spatial frequency and halves amplitude.
    Returns values approximately in [-1, 1].
    """
    rng         = np.random.default_rng()
    noise       = np.zeros(n)
    amplitude   = 1.0
    n_ctrl      = float(base_ctrl_pts)
    total_amp   = 0.0

    for _ in range(octaves):
        noise     += amplitude * _smooth_noise_1d(n, int(n_ctrl), rng)
        total_amp += amplitude
        amplitude *= persistence
        n_ctrl    *= lacunarity   # more control points → higher spatial frequency

    return noise / (total_amp + 1e-9)


def generate_incision() -> np.ndarray:
    """
    Sharp-force incised wound (paper §"The Incised Wound").

    1D cross-section of the fusiform/elliptical opening:
    - Smooth boundaries (fractal dimension Df ≈ 1.0, no edge noise)
    - V-shaped depth profile: linear from 0 at wound margins to max_depth at
      centre, matching the clean V-shaped tissue bed described forensically
    - Logarithmic tailing on the blade-exit side: simulates the blade lifting
      out of the tissue, giving investigators a directional force vector
    Depth: -8 to -20 mm | Half-width: 8–20 scan points
    """
    x         = np.arange(N_POINTS, dtype=float)
    center    = random.uniform(N_POINTS * 0.3, N_POINTS * 0.7)
    max_depth = random.uniform(-20.0, -8.0)
    half_w    = random.uniform(8.0, 20.0)       # fusiform half-width (scan pts)
    tailing_k = random.uniform(1.0, 3.0)        # blade-exit decay rate

    dist   = np.abs(x - center)
    inside = dist < half_w

    # V-shaped depth: linear from 0 at edges to max_depth at centre
    profile = np.zeros(N_POINTS)
    profile[inside] = max_depth * (1.0 - dist[inside] / half_w)

    # Tailing: blade-exit side shallows with exponential decay
    tail_mask = (x > center) & inside
    profile[tail_mask] *= np.exp(
        -tailing_k * (x[tail_mask] - center) / half_w
    )

    return profile


def generate_laceration() -> np.ndarray:
    """
    Blunt-force laceration (paper §"The Laceration").

    - Jagged boundary via coherent fractal perturbation of the wound EDGE
      position (not per-sample width): the noise shifts the effective boundary
      inward/outward continuously, creating recognisable tears rather than noise
    - Parabolic depth base (deeper at centre, crushing against bone anvil) with
      a superimposed chaotic floor variation
    - Tissue bridging: partial depth recoveries simulating intact nerve/vessel
      strands that survived the blunt stretching force
    Df > 1.2 enforced by 5-octave noise (persistence=0.65).
    Depth: -5 to -20 mm | Base half-width: 15–40 scan points
    """
    x         = np.arange(N_POINTS, dtype=float)
    center    = random.uniform(N_POINTS * 0.3, N_POINTS * 0.7)
    max_depth = random.uniform(-20.0, -5.0)
    half_w    = random.uniform(15.0, 40.0)
    edge_zone = max(3.0, half_w * 0.12)   # smooth transition width at margins

    dist = np.abs(x - center)

    # 1. Coherent boundary perturbation:
    #    Compute signed distance from the nominal wound edge, then displace it
    #    with fractal noise.  Because the same noise array shifts ALL points by
    #    the same coherent field, adjacent samples stay correlated — producing
    #    macro-tears rather than independent per-sample jitter (Df > 1.2).
    edge_noise   = _fractal_noise_1d(N_POINTS, octaves=5, persistence=0.65,
                                     base_ctrl_pts=4)
    dist_to_edge = (half_w - dist) + edge_noise * (half_w * 0.30)

    inside         = dist_to_edge > 0
    jagged_envelope = np.clip(dist_to_edge / edge_zone, 0.0, 1.0)

    # 2. Depth: parabolic base (deeper at centre) + coherent chaotic variation
    depth_base  = max_depth * np.clip(1.0 - (dist / half_w) ** 1.5, 0.0, 1.0)
    floor_noise = _fractal_noise_1d(N_POINTS, octaves=4, persistence=0.50,
                                    base_ctrl_pts=8)
    profile = np.where(
        inside,
        (depth_base + max_depth * 0.35 * floor_noise) * jagged_envelope,
        0.0,
    )
    profile = np.clip(profile, max_depth * 1.3, 0.0)

    # 3. Tissue bridging: 2–5 partial depth recoveries spanning the void
    n_bridges = random.randint(2, 5)
    for _ in range(n_bridges):
        bpos   = random.uniform(center - half_w * 0.7, center + half_w * 0.7)
        bwidth = random.uniform(1.5, 5.0)
        bfrac  = random.uniform(0.30, 0.70)
        bmask  = inside & (np.abs(x - bpos) < bwidth)
        profile[bmask] *= (1.0 - bfrac)

    return profile


def generate_abrasion() -> np.ndarray:
    """
    Tangential frictional abrasion (paper §"The Abrasion").

    - Stage II partial-thickness: functionally negligible depth (-0.1–0.5 mm)
    - High-frequency fractal texture (6 octaves, high persistence) mimics
      microscopic epithelial shearing against a rough surface
    - Directional heaped epidermis: a linear gradient shifts displacement from
      slightly negative (tissue scraped away) at the drag origin to slightly
      positive (necrotic epidermal heap) at the terminal edge — the forensic
      directionality vector described in the paper
    Depth: -0.1 to -0.5 mm | Coverage: 40–80 % of scan
    """
    max_depth = random.uniform(-0.5, -0.1)
    coverage  = random.uniform(0.4, 0.8)
    zone_w    = int(N_POINTS * coverage)
    max_start = max(int(N_POINTS * 0.05), int(N_POINTS * 0.95 - zone_w))
    zone_s    = random.randint(int(N_POINTS * 0.05), max(int(N_POINTS * 0.05) + 1,
                                                          max_start))
    zone_e    = min(zone_s + zone_w, N_POINTS)
    rlen      = zone_e - zone_s

    # High-frequency scraping texture (paper: 64×64 period, 6 octaves, persistence 0.75)
    scrape = _fractal_noise_1d(N_POINTS, octaves=6, persistence=0.75,
                               base_ctrl_pts=32)[zone_s:zone_e]

    # Drag gradient: full depth at entry → zero at exit
    drag_grad = np.linspace(1.0, 0.0, rlen)
    # Heap gradient: zero at entry → positive elevation at terminal end
    heap_grad = np.linspace(0.0, 1.0, rlen)
    heap_amp  = abs(max_depth) * 0.35

    profile = np.zeros(N_POINTS)
    profile[zone_s:zone_e] = (
        max_depth * (scrape * 0.4 + 0.6) * (0.5 + 0.5 * drag_grad)  # scraping loss
        + heap_amp * heap_grad * np.maximum(scrape, 0)                # terminal heaping
    )

    return profile


# ---------------------------------------------------------------------------
# Noise functions
# ---------------------------------------------------------------------------

def add_hf_vibration(
    signal: np.ndarray,
    lb_freq: float,
    ub_freq: float,
    n_components: int = 42,
    amplitude: float = 0.03,
) -> np.ndarray:
    """
    HF vibration noise: sum of n_components random sinusoids in [lb_freq, ub_freq]
    cycles/scan, each with an independent random phase and amplitude up to
    `amplitude` mm.  Mirrors notebook cell add_noise_1 (42-component sine sum).
    """
    n = len(signal)
    x = np.linspace(0.0, 1.0, n)
    noise = np.zeros(n)
    for _ in range(n_components):
        freq  = random.uniform(lb_freq, ub_freq)
        phase = random.uniform(0.0, 2.0 * np.pi)
        amp   = random.uniform(amplitude * 0.4, amplitude)
        noise += amp * np.sin(2.0 * np.pi * freq * x + phase)
    return signal + noise


def add_jitter(
    signal: np.ndarray,
    n_spikes: int,
    amp_scale: float,
    width: float = 1.0,
) -> np.ndarray:
    """
    Jitter noise: sum of n_spikes narrow Gaussian pulses at random scan
    positions with random polarity.  Mirrors notebook cell add_noise_2
    (Gaussian-shaped jitter artifacts rather than sample-position interpolation).
    amp_scale — maximum spike displacement in mm
    width     — spike half-width in samples
    """
    n = len(signal)
    x = np.arange(n, dtype=float)
    noise = np.zeros(n)
    for _ in range(n_spikes):
        pos = random.uniform(0.0, float(n))
        amp = random.uniform(-amp_scale, amp_scale)
        noise += amp * np.exp(-((x - pos) ** 2) / (2.0 * width ** 2))
    return signal + noise


def add_awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    """
    Additive White Gaussian Noise at a specified SNR.
    snr_db — desired signal-to-noise ratio in dB (15–35)
    """
    signal_power = np.mean(signal ** 2)
    if signal_power == 0:
        signal_power = 1e-6
    noise_power = signal_power / (10 ** (snr_db / 10))
    noise = np.random.normal(0, np.sqrt(noise_power), len(signal))
    return signal + noise


# ---------------------------------------------------------------------------
# ThingSpeak uploader
# ---------------------------------------------------------------------------

def upload_to_thingspeak(signal: np.ndarray, wound_type: str) -> bool:
    """
    Bulk-upload the scan array to ThingSpeak.
    field1 — depth value (mm) at each scan point
    field2 — wound type code (first entry only)
    Returns True on success.
    """
    updates = []
    type_code = WOUND_TYPE_CODE[wound_type]

    for i, value in enumerate(signal):
        entry = {"delta_t": i, "field1": round(float(value), 4)}
        if i == 0:
            entry["field2"] = type_code
        updates.append(entry)

    payload = {"write_api_key": WRITE_API_KEY, "updates": updates}

    try:
        response = requests.post(
            THINGSPEAK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=30,
        )
        if response.status_code == 202:
            print(f"Upload successful (HTTP 202). {len(updates)} points sent.")
            return True
        else:
            print(f"Upload failed — HTTP {response.status_code}: {response.text}")
            return False
    except requests.RequestException as exc:
        print(f"Upload error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Pick a random wound type
    generators = {
        "laceration": generate_laceration,
        "incision":   generate_incision,
        "abrasion":   generate_abrasion,
    }
    wound_type = random.choice(list(generators))
    clean_signal = generators[wound_type]()

    # 2. Draw noise parameters from the per-wound profile
    p = NOISE_PROFILES[wound_type]

    hf_lb    = random.uniform(*p["hf"]["lb_freq"])
    hf_ub    = random.uniform(*p["hf"]["ub_freq"])
    hf_amp   = random.uniform(*p["hf"]["amplitude"])
    hf_n     = p["hf"]["n_components"]

    jit_n    = random.randint(*p["jitter"]["n_spikes"])
    jit_amp  = random.uniform(*p["jitter"]["amp_scale"])
    jit_w    = random.uniform(*p["jitter"]["width"])

    snr_db   = random.uniform(*p["awgn"]["snr_db"])

    # 3. Apply noises sequentially
    noisy = add_hf_vibration(clean_signal, lb_freq=hf_lb, ub_freq=hf_ub,
                              n_components=hf_n, amplitude=hf_amp)
    noisy = add_jitter(noisy, n_spikes=jit_n, amp_scale=jit_amp, width=jit_w)
    noisy = add_awgn(noisy, snr_db=snr_db)

    # 4. Print summary (primary noise flagged with *)
    primary  = {"incision": "hf", "laceration": "awgn", "abrasion": "jitter"}[wound_type]
    hf_tag   = " *PRIMARY*" if primary == "hf"     else ""
    jit_tag  = " *PRIMARY*" if primary == "jitter" else ""
    awgn_tag = " *PRIMARY*" if primary == "awgn"   else ""
    print("=" * 60)
    print(f"  Wound type    : {wound_type}")
    print(f"  HF vibration  : {hf_n} components, {hf_lb:.0f}–{hf_ub:.0f} cyc/scan, "
          f"amp={hf_amp:.3f} mm{hf_tag}")
    print(f"  Jitter        : {jit_n} spikes, amp={jit_amp:.3f} mm, "
          f"width={jit_w:.1f} smp{jit_tag}")
    print(f"  AWGN          : SNR={snr_db:.1f} dB{awgn_tag}")
    print(f"  Signal range  : [{noisy.min():.2f}, {noisy.max():.2f}] mm")
    print("=" * 60)

    # 5. Plot
    x_mm = np.linspace(0, N_POINTS - 1, N_POINTS)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

    axes[0].plot(x_mm, clean_signal, color="steelblue", linewidth=1.5)
    axes[0].set_ylabel("Depth (mm)")
    axes[0].set_title(f"Clean wound profile — {wound_type}")
    axes[0].axhline(0, color="gray", linewidth=0.7, linestyle="--")
    axes[0].invert_yaxis()

    axes[1].plot(x_mm, noisy, color="tomato", linewidth=1.0)
    axes[1].set_ylabel("Depth (mm)")
    axes[1].set_xlabel("Scan position (samples)")
    labels = {
        "hf":     f"HF: {hf_n}×sin {hf_lb:.0f}–{hf_ub:.0f} cyc, {hf_amp:.3f} mm",
        "jitter": f"Jitter: {jit_n} spikes ±{jit_amp:.3f} mm",
        "awgn":   f"AWGN: {snr_db:.0f} dB",
    }
    title_parts = [f"[{v}]" if k == primary else v for k, v in labels.items()]
    axes[1].set_title("After noise  ([ ] = primary)  |  " + "  |  ".join(title_parts))
    axes[1].axhline(0, color="gray", linewidth=0.7, linestyle="--")
    axes[1].invert_yaxis()

    plt.tight_layout()
    plt.show(block=False)
    plt.pause(0.1)  # allow the window to render before the prompt

    # 6. Confirm upload
    answer = input("\nUpload to ThingSpeak? [y/N] ").strip().lower()
    if answer == "y":
        upload_to_thingspeak(noisy, wound_type)
    else:
        print("Upload skipped.")

    plt.show()  # keep the window open until the user closes it


if __name__ == "__main__":
    main()
