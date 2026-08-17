"""CLI entry point for Tether."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import re
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .core.config import (
    TetherConfig,
    CONFIG_DIR,
    CONFIG_FILE,
    REMOTE_PROVIDERS,
    apply_provider_selection,
    provider_model,
)
from .core.logging import setup_logging, get_logger
from . import __version__


GGUF_DIR = Path(__file__).resolve().parent.parent / "gpt-oss-120b-Derestricted-GGUF"


def _auto_project_gguf_dirs() -> list[Path]:
    """Every ~/Projects/*-GGUF/ directory and ~/Projects/*/*-GGUF/ subdirs.

    Keeps model drops discoverable without the user having to edit this
    file every time they grab a new GGUF. We include *-GGUF/ naming (the
    convention HF-downloaded GGUFs ship with) at depth 1 and 2 under
    ~/Projects — deep enough to find Qwen3.6-35B-A3B-GGUF/ at the root
    without walking the whole tree.
    """
    projects = Path.home() / "Projects"
    if not projects.is_dir():
        return []
    found: list[Path] = []
    for entry in projects.iterdir():
        if entry.is_dir() and entry.name.endswith("-GGUF"):
            found.append(entry)
    return found


LOCAL_GGUF_DIRS: list[Path] = [
    GGUF_DIR,
    Path.home() / "MiniMax-M2-GGUF" / "IQ4_XS",
    # Centralized drop-in dir — user can symlink or copy GGUFs here and
    # they'll show up in the selector automatically.
    Path.home() / "Projects" / "models",
    # Auto-pick up any ~/Projects/*-GGUF/ repo layout
    *_auto_project_gguf_dirs(),
]
def _default_llama_cpp_dir() -> Path:
    """Where `tether setup` clones/builds llama.cpp.

    Order: ``TETHER_LLAMA_CPP_DIR`` if set; a checkout next to a git clone of
    Tether (the historical location, kept so existing builds are found); else
    ``~/.tether/llama.cpp`` — outside any pipx venv, so upgrading the CLI does
    not delete a build that took minutes.
    """
    override = os.environ.get("TETHER_LLAMA_CPP_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    package_dir = Path(__file__).resolve().parent
    beside_checkout = package_dir.parent / "llama.cpp"
    if (package_dir / ".git").exists() or beside_checkout.exists():
        return beside_checkout
    return CONFIG_DIR / "llama.cpp"


LLAMA_CPP_DIR = _default_llama_cpp_dir()
LLAMA_SERVER_BIN = LLAMA_CPP_DIR / "build" / "bin" / "llama-server"
HF_CACHE_DIR = Path.home() / ".cache" / "huggingface" / "hub"

GGUF_MAGIC = b"GGUF"
GGUF_SUPPORTED_VERSIONS = {2, 3}

# GGUF value type format strings (type_id -> (struct_fmt, byte_size))
_GGUF_SCALAR_TYPES = {
    0: ("B", 1),    # UINT8
    1: ("b", 1),    # INT8
    2: ("<H", 2),   # UINT16
    3: ("<h", 2),   # INT16
    4: ("<I", 4),   # UINT32
    5: ("<i", 4),   # INT32
    6: ("<f", 4),   # FLOAT32
    7: ("?", 1),    # BOOL
    10: ("<Q", 8),  # UINT64
    11: ("<q", 8),  # INT64
    12: ("<d", 8),  # FLOAT64
}


def _gguf_read_string(f) -> str:
    length = struct.unpack("<Q", f.read(8))[0]
    return f.read(length).decode("utf-8")


def _gguf_read_value(f, vtype: int):
    if vtype == 8:  # STRING
        return _gguf_read_string(f)
    if vtype == 9:  # ARRAY
        arr_type = struct.unpack("<I", f.read(4))[0]
        arr_len = struct.unpack("<Q", f.read(8))[0]
        return [_gguf_read_value(f, arr_type) for _ in range(arr_len)]
    fmt, size = _GGUF_SCALAR_TYPES[vtype]
    return struct.unpack(fmt, f.read(size))[0]


def read_gguf_metadata(path: Path) -> dict | None:
    """Read metadata key-value pairs from a GGUF file header.

    Returns a dict of metadata, or None if the file is not a valid GGUF.
    Only reads the header -- does not touch tensor data.
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != GGUF_MAGIC:
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version not in GGUF_SUPPORTED_VERSIONS:
                return None
            _tensor_count = struct.unpack("<Q", f.read(8))[0]
            kv_count = struct.unpack("<Q", f.read(8))[0]

            metadata = {}
            for _ in range(kv_count):
                key = _gguf_read_string(f)
                vtype = struct.unpack("<I", f.read(4))[0]
                value = _gguf_read_value(f, vtype)
                metadata[key] = value
            return metadata
    except (OSError, struct.error, UnicodeDecodeError, KeyError):
        return None


def get_gguf_context_length(path: Path) -> int | None:
    """Extract the model's native context length from GGUF metadata."""
    meta = read_gguf_metadata(path)
    if meta is None:
        return None
    # The key is "{architecture}.context_length"
    arch = meta.get("general.architecture", "")
    ctx_key = f"{arch}.context_length"
    return meta.get(ctx_key)


def get_hf_context_length(snapshot_dir: Path) -> int | None:
    """Extract context length from a HuggingFace model's config.json."""
    config_path = snapshot_dir / "config.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
        # Check common locations for max context / sequence length
        for key in ("max_position_embeddings", "max_sequence_length", "n_positions", "seq_length"):
            if key in data:
                return data[key]
        # Some models nest it under text_config (e.g. gemma multimodal)
        text_cfg = data.get("text_config", {})
        for key in ("max_position_embeddings", "max_sequence_length", "n_positions"):
            if key in text_cfg:
                return text_cfg[key]
    except (json.JSONDecodeError, OSError):
        pass
    return None


def validate_gguf(path: Path) -> tuple[bool, str]:
    """Validate that a GGUF file is loadable by llama.cpp.

    Returns (ok, reason).
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != GGUF_MAGIC:
                return False, "not a GGUF file (bad magic bytes)"
            version = struct.unpack("<I", f.read(4))[0]
            if version not in GGUF_SUPPORTED_VERSIONS:
                return False, f"unsupported GGUF version {version} (need v2 or v3)"
    except OSError as e:
        return False, f"cannot read file: {e}"
    return True, ""


@dataclass
class DiscoveredModel:
    name: str           # e.g. "google/gemma-4-31B-it"
    path: Path          # path to the model file or snapshot dir
    format: str         # "gguf" or "safetensors"
    size_bytes: int     # total size of model files
    source: str         # "huggingface" or "local"
    context_length: int | None = None  # native context window from metadata
    architecture: str | None = None    # model architecture (e.g. "llama", "gemma")

    @property
    def size_human(self) -> str:
        if self.size_bytes >= 1 << 30:
            return f"{self.size_bytes / (1 << 30):.1f} GB"
        return f"{self.size_bytes / (1 << 20):.0f} MB"

    @property
    def context_human(self) -> str:
        if self.context_length is None:
            return "?"
        k = self.context_length / 1024
        if k >= 1024:
            return f"{k / 1024:.0f}M"
        return f"{int(k)}k"


# Matches split GGUF naming: "Model-Q4_K_M-00001-of-00004.gguf"
_SPLIT_GGUF_RE = re.compile(r"^(.+)-(\d{5})-of-(\d{5})\.gguf$")


def _find_gguf_in_dir(directory: Path) -> list[Path]:
    """Find usable GGUF files in a directory.

    Skips incomplete downloads (.part suffix in name) and groups split GGUF
    files so only the first shard (00001-of-N) is returned.  The caller
    should use ``_split_gguf_parts`` to get all shards for size calculation.
    """
    seen_splits: set[str] = set()   # base names we already recorded
    ggufs: list[Path] = []

    for f in sorted(directory.glob("*.gguf")):
        # Skip HuggingFace incomplete-download temporaries
        if f.name.endswith(".part") or ".gguf.part" in f.name:
            continue

        m = _SPLIT_GGUF_RE.match(f.name)
        if m:
            base, idx, _total = m.group(1), m.group(2), m.group(3)
            if base in seen_splits:
                continue           # already recorded the first shard
            seen_splits.add(base)
            if idx != "00001":
                continue           # stray shard without 00001 -- skip
            ggufs.append(f)
        else:
            ggufs.append(f)

    return ggufs


def _split_gguf_parts(first_shard: Path) -> list[Path]:
    """Return all shards for a split GGUF, or [path] for a single file."""
    m = _SPLIT_GGUF_RE.match(first_shard.name)
    if not m:
        return [first_shard]
    base, _, total_str = m.group(1), m.group(2), m.group(3)
    total = int(total_str)
    parts = []
    for i in range(1, total + 1):
        part = first_shard.parent / f"{base}-{i:05d}-of-{total_str}.gguf"
        if part.exists():
            parts.append(part)
    return parts if parts else [first_shard]


def _total_size(files: list[Path]) -> int:
    return sum(f.stat().st_size for f in files if f.exists())


def discover_models(extra_dirs: list[Path] | None = None) -> list[DiscoveredModel]:
    """Scan HF cache and local GGUF dirs for available models.

    `extra_dirs` augments the search at runtime (e.g. from a CLI flag).
    Persistent user-configured directories live in `config.gguf_dirs` and
    are pulled in here so callers don't have to plumb the config through.
    """
    models: list[DiscoveredModel] = []
    seen_paths: set[Path] = set()

    # Build the full search list: builtins + persisted user dirs + runtime extras
    user_dirs: list[Path] = []
    try:
        user_cfg = TetherConfig.load()
        for entry in user_cfg.gguf_dirs:
            try:
                user_dirs.append(Path(entry).expanduser())
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    runtime_dirs = [Path(p).expanduser() for p in (extra_dirs or [])]
    search_dirs: list[Path] = []
    for d in (*LOCAL_GGUF_DIRS, *user_dirs, *runtime_dirs):
        if d not in search_dirs:
            search_dirs.append(d)

    # 1. Local GGUF directories
    for gguf_dir in search_dirs:
        if not gguf_dir.exists():
            continue
        ggufs = _find_gguf_in_dir(gguf_dir)
        for gguf in ggufs:
            resolved = gguf.resolve()
            if resolved in seen_paths:
                continue
            ok, reason = validate_gguf(gguf)
            if not ok:
                continue
            meta = read_gguf_metadata(gguf)
            arch = meta.get("general.architecture", "") if meta else ""
            ctx = meta.get(f"{arch}.context_length") if meta and arch else None
            all_parts = _split_gguf_parts(gguf)
            seen_paths.add(resolved)
            models.append(DiscoveredModel(
                name=meta.get("general.name", gguf.stem) if meta else gguf.stem,
                path=gguf,
                format="gguf",
                size_bytes=_total_size(all_parts),
                source="local",
                context_length=ctx,
                architecture=arch or None,
            ))

    # 2. Converted models directory (~/.tether/models/)
    if CONVERTED_DIR.exists():
        ggufs = _find_gguf_in_dir(CONVERTED_DIR)
        for gguf in ggufs:
            ok, reason = validate_gguf(gguf)
            if not ok:
                continue
            meta = read_gguf_metadata(gguf)
            arch = meta.get("general.architecture", "") if meta else ""
            ctx = meta.get(f"{arch}.context_length") if meta and arch else None
            # Use filename (e.g. "gemma-4-31B-it-f16") -- more readable than metadata name
            display_name = gguf.stem.replace("--", "/")
            all_parts = _split_gguf_parts(gguf)
            models.append(DiscoveredModel(
                name=display_name,
                path=gguf,
                format="gguf",
                size_bytes=_total_size(all_parts),
                source="converted",
                context_length=ctx,
                architecture=arch or None,
            ))

    # 3. HuggingFace cache
    if HF_CACHE_DIR.exists():
        for model_dir in sorted(HF_CACHE_DIR.iterdir()):
            if not model_dir.name.startswith("models--"):
                continue
            # Parse org/model from "models--org--name" format
            parts = model_dir.name.split("--", 1)
            if len(parts) < 2:
                continue
            hf_name = parts[1].replace("--", "/")

            # Find the latest snapshot
            snapshots_dir = model_dir / "snapshots"
            if not snapshots_dir.exists():
                continue
            snapshot_dirs = sorted(snapshots_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            if not snapshot_dirs:
                continue
            snapshot = snapshot_dirs[0]

            # Check for GGUF files first
            ggufs = _find_gguf_in_dir(snapshot)
            if ggufs:
                for gguf in ggufs:
                    ok, reason = validate_gguf(gguf)
                    if not ok:
                        continue
                    meta = read_gguf_metadata(gguf)
                    arch = meta.get("general.architecture", "") if meta else ""
                    ctx = meta.get(f"{arch}.context_length") if meta and arch else None
                    all_parts = _split_gguf_parts(gguf)
                    models.append(DiscoveredModel(
                        name=f"{hf_name} ({gguf.name})",
                        path=gguf,
                        format="gguf",
                        size_bytes=_total_size(all_parts),
                        source="huggingface",
                        context_length=ctx,
                        architecture=arch or None,
                    ))
                continue

            # Check for safetensors
            safetensors = sorted(snapshot.glob("*.safetensors"))
            if safetensors:
                ctx = get_hf_context_length(snapshot)
                models.append(DiscoveredModel(
                    name=hf_name,
                    path=snapshot,
                    format="safetensors",
                    size_bytes=_total_size(safetensors),
                    source="huggingface",
                    context_length=ctx,
                ))

    return models


def _get_total_memory_bytes() -> int:
    """Get total system memory from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _get_gpu_memory_bytes() -> int:
    """Get total GPU memory via nvidia-smi (returns bytes, or 0 on failure).

    On unified-memory systems (e.g. DGX Spark / Grace Hopper) nvidia-smi
    reports 'N/A' or 'Not Supported' for memory totals -- returns 0 in
    that case, signalling the caller to treat system RAM as the full pool.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        )
        total = 0
        for line in out.strip().splitlines():
            line = line.strip()
            if line.startswith("[") or "Not Supported" in line or "N/A" in line:
                continue
            total += int(line) * (1 << 20)
        return total
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def _is_unified_memory() -> bool:
    """Detect unified CPU/GPU memory (e.g. DGX Spark, Jetson).

    nvidia-smi reports 'Not Supported' for memory on these systems.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        )
        for line in out.strip().splitlines():
            if "[N/A]" in line or "Not Supported" in line:
                return True
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return False


_KV_CACHE_BPE = {
    "f32": 4.0, "f16": 2.0, "bf16": 2.0,
    "q8_0": 1.0625, "q5_1": 0.6875, "q5_0": 0.65625,
    "q4_1": 0.5625, "q4_0": 0.53125, "iq4_nl": 0.53125,
}


def _estimate_kv_bytes_per_token(meta: dict, arch: str,
                                 cache_type_k: str = "f16",
                                 cache_type_v: str = "f16") -> float:
    """Estimate KV cache bytes per token per slot from GGUF metadata.

    Accounts for sliding window attention (only caches window_size tokens)
    and per-layer GQA variations.  *cache_type_k* / *cache_type_v* let the
    caller pass quantised KV types (e.g. ``"q4_0"``) so the estimate
    reflects the actual memory footprint.
    """
    n_layers = meta.get(f"{arch}.block_count", 32)
    head_count_kv = meta.get(f"{arch}.attention.head_count_kv", 8)
    head_count = meta.get(f"{arch}.attention.head_count", 32)
    embd = meta.get(f"{arch}.embedding_length", 4096)
    swa_window = meta.get(f"{arch}.attention.sliding_window")

    # Determine head dimensions
    # Regular: head_dim = embedding_length / head_count
    head_dim = embd // head_count if head_count else 128

    # Check for explicit key/value head dims in metadata
    head_dim_k = meta.get(f"{arch}.attention.key_length", head_dim)
    head_dim_v = meta.get(f"{arch}.attention.value_length", head_dim)

    bpe_k = _KV_CACHE_BPE.get(cache_type_k, 2.0)
    bpe_v = _KV_CACHE_BPE.get(cache_type_v, 2.0)

    # Per-layer KV head counts (may be a list for mixed GQA, e.g. gemma4)
    if isinstance(head_count_kv, list):
        kv_heads_per_layer = head_count_kv
    else:
        kv_heads_per_layer = [head_count_kv] * n_layers

    # Determine which layers use sliding window vs full context
    # Heuristic: layers with fewer KV heads than the mode are global attention
    # (gemma4 pattern: 16 kv heads = sliding, 4 kv heads = global)
    if swa_window and len(set(kv_heads_per_layer)) > 1:
        from collections import Counter
        mode_kv = Counter(kv_heads_per_layer).most_common(1)[0][0]
        # Sliding window layers use swa head dims (often half of global)
        swa_head_dim_k = meta.get(f"{arch}.attention.key_length_swa", head_dim_k // 2)
        swa_head_dim_v = meta.get(f"{arch}.attention.value_length_swa", head_dim_v // 2)

        # Return a function of context length since SWA layers cap at window_size
        # We'll compute for a reference context and return per-token average
        # Actually, return the effective bytes per context token:
        # SWA layers contribute a fixed cost (window * kv_size), not per-context-token
        # Global layers contribute per-context-token
        global_per_token = 0
        swa_fixed_total = 0
        for kv_h in kv_heads_per_layer:
            if kv_h == mode_kv:
                # Sliding window layer
                swa_fixed_total += kv_h * (swa_head_dim_k * bpe_k + swa_head_dim_v * bpe_v) * swa_window
            else:
                # Global attention layer
                global_per_token += kv_h * (head_dim_k * bpe_k + head_dim_v * bpe_v)

        # Return as a tuple so caller can handle both parts
        return global_per_token, swa_fixed_total
    else:
        # No sliding window.  Sum per-layer contributions (handles hybrid
        # architectures like Nemotron-H where most layers are Mamba/SSM with
        # 0 KV heads and only a few layers have attention).
        per_token = sum(kv_h * (head_dim_k * bpe_k + head_dim_v * bpe_v)
                        for kv_h in kv_heads_per_layer)
        if swa_window:
            # All layers are SWA -- KV is capped at window_size
            return 0, per_token * swa_window
        return per_token, 0


def _optimal_context_size(model: DiscoveredModel, parallel_slots: int,
                          cache_type_k: str = "f16",
                          cache_type_v: str = "f16") -> int:
    """Pick the largest context size that fits in memory.

    Reads GGUF metadata to estimate KV cache cost accurately, including
    sliding window attention.  Uses combined CPU + GPU memory budget
    since KV cache can land on either, then subtracts model weights and
    a conservative OS headroom.
    """
    if not model.context_length:
        return 131072

    sys_mem = _get_total_memory_bytes()
    gpu_mem = _get_gpu_memory_bytes()
    unified = _is_unified_memory()

    if unified:
        # Unified memory (DGX Spark / Grace Hopper / Jetson): system RAM is
        # the entire pool shared between CPU and GPU.  Model weights and KV
        # cache all come from this pool.
        total_mem = sys_mem
        headroom = 4 * (1 << 30)
    elif gpu_mem and sys_mem:
        # Discrete GPU: model weights mostly on VRAM, KV cache on VRAM too.
        total_mem = sys_mem + gpu_mem
        headroom = 10 * (1 << 30)
    else:
        total_mem = sys_mem or gpu_mem
        headroom = 10 * (1 << 30)

    if total_mem == 0:
        return min(model.context_length, 131072)

    available = total_mem - model.size_bytes - headroom
    if available <= 0:
        return 8192

    meta = read_gguf_metadata(model.path)
    if meta:
        arch = meta.get("general.architecture", "")
        result = _estimate_kv_bytes_per_token(meta, arch, cache_type_k, cache_type_v)
        if isinstance(result, tuple):
            per_token, swa_fixed = result
        else:
            per_token, swa_fixed = result, 0

        # Total KV = (per_token * ctx + swa_fixed) * slots
        # Solve for ctx: ctx = (available / slots - swa_fixed) / per_token
        budget_per_slot = available / parallel_slots
        if per_token > 0:
            max_tokens = int((budget_per_slot - swa_fixed) / per_token)
        else:
            # Pure SWA model, context doesn't affect KV much
            max_tokens = model.context_length
    else:
        # No metadata, conservative estimate
        max_tokens = available // (parallel_slots * 1024)

    # Round down to nearest power-of-2 friendly number
    ctx_options = [8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]
    best = 8192
    for ctx in ctx_options:
        if ctx <= max_tokens and ctx <= model.context_length:
            best = ctx

    return best


QUANT_TYPES = ["Q4_K_M", "Q4_K_S", "Q5_K_M", "Q6_K", "Q8_0", "F16"]

CONVERT_SCRIPT = LLAMA_CPP_DIR / "convert_hf_to_gguf.py"
QUANTIZE_BIN = LLAMA_CPP_DIR / "build" / "bin" / "llama-quantize"
CONVERTED_DIR = CONFIG_DIR / "models"


def _is_prequantized_hf(model_path: Path) -> bool:
    """Check if a HuggingFace model directory contains pre-quantized weights (NVFP4, GPTQ, etc.)."""
    quant_config = model_path / "hf_quant_config.json"
    if quant_config.exists():
        try:
            data = json.loads(quant_config.read_text())
            algo = data.get("quantization", {}).get("quant_algo", "")
            if algo in ("NVFP4", "MIXED_PRECISION", "FP8"):
                return True
        except (json.JSONDecodeError, OSError):
            pass
    # Also check config.json for quantization_config
    config_json = model_path / "config.json"
    if config_json.exists():
        try:
            data = json.loads(config_json.read_text())
            if data.get("quantization_config", {}).get("quant_method") in ("modelopt", "gptq", "awq"):
                return True
        except (json.JSONDecodeError, OSError):
            pass
    return False


def _convert_to_gguf(model: DiscoveredModel) -> Path | None:
    """Convert a safetensors model to GGUF. Returns path to final GGUF or None."""
    from .ui.colors import bold, dim, error, info, warning, DIM, RESET, CYAN, GREEN

    print()
    print(info(f"  {model.name} is safetensors — needs conversion to GGUF for llama.cpp"))
    print()

    if not CONVERT_SCRIPT.exists():
        print(error("  convert_hf_to_gguf.py not found in llama.cpp directory."))
        print(dim(f"  Expected: {CONVERT_SCRIPT}"))
        return None

    # Check that the venv has the needed deps (transformers, torch)
    python = sys.executable

    # Pre-quantized models (NVFP4, FP8, etc.) should be converted with
    # --outtype auto to preserve native quantization.  No further quantize step.
    prequantized = _is_prequantized_hf(model.path)

    if prequantized:
        quant_type = None  # no re-quantization
        print(dim("  Pre-quantized model detected — converting with native quantization preserved."))
    else:
        # Pick quantization type
        print(f"  {bold('Select quantization')}")
        print()
        for i, qt in enumerate(QUANT_TYPES):
            note = ""
            if qt == "Q4_K_M":
                note = f"  {DIM}(recommended, good balance){RESET}"
            elif qt == "Q4_K_S":
                note = f"  {DIM}(smaller, slightly less quality){RESET}"
            elif qt == "Q5_K_M":
                note = f"  {DIM}(higher quality, larger){RESET}"
            elif qt == "Q8_0":
                note = f"  {DIM}(near-lossless, 2x size of Q4){RESET}"
            elif qt == "F16":
                note = f"  {DIM}(no quantization, largest){RESET}"
            print(f"  {CYAN}{i + 1}{RESET}  {qt}{note}")
        print()

        try:
            choice = input("  [1]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not choice:
            choice = "1"
        try:
            qt_idx = int(choice) - 1
            if qt_idx < 0 or qt_idx >= len(QUANT_TYPES):
                print(error(f"  Invalid choice: {choice}"))
                return None
        except ValueError:
            print(error(f"  Invalid choice: {choice}"))
            return None

        quant_type = QUANT_TYPES[qt_idx]

    # Output paths
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = model.name.replace("/", "--")

    if prequantized:
        final_path = CONVERTED_DIR / f"{safe_name}-native.gguf"
        f16_path = final_path  # no intermediate needed
    elif quant_type == "F16":
        f16_path = CONVERTED_DIR / f"{safe_name}-f16.gguf"
        final_path = f16_path
    else:
        f16_path = CONVERTED_DIR / f"{safe_name}-f16.gguf"
        final_path = CONVERTED_DIR / f"{safe_name}-{quant_type}.gguf"

    # Skip if already converted
    if final_path.exists():
        print()
        print(f"  {GREEN}already converted{RESET}  {final_path.name}")
        return final_path

    # Step 1: convert safetensors -> GGUF
    outtype = "auto" if prequantized else "f16"
    steps = "1/1" if (prequantized or quant_type == "F16") else "1/2"
    print()
    print(f"  {bold(f'Step {steps}')}  converting to {outtype} GGUF...")
    print(dim(f"  source: {model.path}"))
    print(dim(f"  output: {f16_path}"))
    print()

    convert_cmd = [
        python, str(CONVERT_SCRIPT),
        str(model.path),
        "--outfile", str(f16_path),
        "--outtype", outtype,
    ]

    try:
        result = subprocess.run(convert_cmd, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        print(error("  Conversion timed out (1 hour limit)."))
        return None

    if result.returncode != 0:
        print(error("  Conversion failed:"))
        # Show last few lines of stderr
        out_text = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        for line in out_text.splitlines()[-8:]:
            print(f"  {DIM}{line}{RESET}")
        return None

    print(f"  {GREEN}done{RESET}  {f16_path.name}")

    if prequantized or quant_type == "F16":
        return final_path

    # Step 2: quantize f16 -> target type
    if not QUANTIZE_BIN.exists():
        print(warning("  llama-quantize not found, keeping f16 GGUF."))
        return f16_path

    print()
    print(f"  {bold('Step 2/2')}  quantizing to {quant_type}...")
    print(dim(f"  output: {final_path}"))
    print()

    nthreads = os.cpu_count() or 4
    quant_cmd = [
        str(QUANTIZE_BIN),
        str(f16_path),
        str(final_path),
        quant_type,
        str(nthreads),
    ]

    try:
        result = subprocess.run(quant_cmd, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        print(error("  Quantization timed out (1 hour limit)."))
        return None

    if result.returncode != 0:
        print(error("  Quantization failed:"))
        out_text = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
        for line in out_text.splitlines()[-8:]:
            print(f"  {DIM}{line}{RESET}")
        # Fall back to f16 if quantization fails
        if f16_path.exists():
            print(warning("  Falling back to f16 GGUF."))
            return f16_path
        return None

    print(f"  {GREEN}done{RESET}  {final_path.name}")

    # Clean up f16 intermediate (it's large)
    if f16_path.exists() and final_path.exists() and f16_path != final_path:
        f16_size = f16_path.stat().st_size / (1 << 30)
        print(dim(f"  Removing intermediate f16 ({f16_size:.1f} GB)..."))
        f16_path.unlink()

    return final_path


def select_model(config: TetherConfig, extra_dirs: list[Path] | None = None) -> Path | None:
    """Interactive model selector. Returns path to model file/dir, or None."""
    from .ui.colors import bold, dim, error, DIM, RESET, CYAN, GREEN, YELLOW, MAGENTA

    models = discover_models(extra_dirs=extra_dirs)
    if not models:
        print(error("No models found."))
        print(dim("  Download a GGUF model to ~/.cache/huggingface/hub/ or place one in the project directory."))
        return None

    # Check if config has a saved model that still exists
    if config.model_path:
        saved = Path(config.model_path)
        if saved.exists():
            # Find index of saved model for default selection
            for i, m in enumerate(models):
                if m.path == saved:
                    break

    print(f"  {bold('Select a model')}")
    print()

    for i, m in enumerate(models):
        num = f"  {CYAN}{i + 1}{RESET}"
        fmt_tag = f"{GREEN}gguf{RESET}" if m.format == "gguf" else f"{YELLOW}safetensors{RESET}"
        ctx_tag = f"{DIM}ctx:{RESET}{m.context_human}" if m.context_length else ""
        default_marker = ""
        if config.model_path and Path(config.model_path) == m.path:
            default_marker = f" {MAGENTA}(last used){RESET}"
        src = f"{DIM}{m.source}{RESET}"
        print(f"{num}  {m.name}  {fmt_tag}  {m.size_human}  {ctx_tag}  {src}{default_marker}")

    print()

    # If there's only one model, auto-select it
    if len(models) == 1:
        print(dim("  Auto-selected the only available model."))
        selected = models[0]
    else:
        # Find default
        default_idx = None
        if config.model_path:
            for i, m in enumerate(models):
                if m.path == Path(config.model_path):
                    default_idx = i + 1
                    break

        prompt_str = "  > "
        if default_idx:
            prompt_str = f"  [{default_idx}]> "

        try:
            choice = input(prompt_str).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not choice and default_idx:
            choice = str(default_idx)
        elif not choice:
            choice = "1"

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(models):
                print(error(f"  Invalid choice: {choice}"))
                return None
        except ValueError:
            print(error(f"  Invalid choice: {choice}"))
            return None

        selected = models[idx]

    if selected.format != "gguf":
        result = _convert_to_gguf(selected)
        if result is None:
            return None
        # Re-read metadata from the new GGUF
        meta = read_gguf_metadata(result)
        if meta:
            arch = meta.get("general.architecture", "")
            ctx = meta.get(f"{arch}.context_length")
            if ctx:
                selected.context_length = ctx
        selected.path = result
        selected.format = "gguf"

    # Auto-configure context size from model metadata + available memory.
    # If memory is very tight, try quantised KV cache (q4_0) and fewer
    # parallel slots before settling for a small context window.
    if selected.context_length:
        ctk, ctv = config.cache_type_k, config.cache_type_v
        optimal_ctx = _optimal_context_size(selected, config.parallel_slots, ctk, ctv)

        if optimal_ctx <= 8192 and config.parallel_slots > 1:
            # Memory-constrained: try with fewer slots for more context
            for fewer in (2, 1):
                better_ctx = _optimal_context_size(selected, fewer, ctk, ctv)
                if better_ctx > optimal_ctx:
                    optimal_ctx = better_ctx
                    config.parallel_slots = fewer
                    break

        if optimal_ctx <= 16384 and ctk == "f16":
            # Still tight -- enable quantised KV cache (q4_0) to reclaim
            # memory.  This is the "turbo quant" approach: ~4x KV savings
            # with minimal quality loss on most architectures.
            ctk, ctv = "q4_0", "q4_0"
            slots = config.parallel_slots
            q_ctx = _optimal_context_size(selected, slots, ctk, ctv)
            if q_ctx <= 8192 and slots > 1:
                for fewer in (2, 1):
                    better = _optimal_context_size(selected, fewer, ctk, ctv)
                    if better > q_ctx:
                        q_ctx = better
                        slots = fewer
                        break
            if q_ctx > optimal_ctx:
                optimal_ctx = q_ctx
                config.parallel_slots = slots
                config.cache_type_k = ctk
                config.cache_type_v = ctv

        config.context_size = optimal_ctx

    # Save selection
    config.model_path = str(selected.path)
    config.save()

    print()
    ctx_k = config.context_size / 1024
    ctx_str = f"{int(ctx_k)}k" if ctx_k < 1024 else f"{ctx_k / 1024:.0f}M"
    max_str = selected.context_human if selected.context_length else "?"
    kv_info = ""
    if config.cache_type_k != "f16":
        kv_info = f", KV cache {config.cache_type_k}"
    print(dim(f"  Using: {selected.name} ({selected.size_human}, {ctx_str} context, max {max_str}{kv_info})"))
    return selected.path


def find_gguf_model() -> Path | None:
    if GGUF_DIR.exists():
        for f in sorted(GGUF_DIR.glob("*.gguf")):
            if "part" not in f.name:
                return f
        parts = sorted(GGUF_DIR.glob("*.part*"))
        if parts:
            combined = GGUF_DIR / parts[0].name.split(".part")[0]
            if not combined.exists():
                return None
            return combined
    return None


def find_llama_server() -> Path | None:
    if LLAMA_SERVER_BIN.exists():
        return LLAMA_SERVER_BIN
    which = shutil.which("llama-server")
    if which:
        return Path(which)
    return None


def server_is_running(config: TetherConfig) -> bool:
    try:
        req = urllib.request.Request(f"{config.server_url}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def doctor_report() -> dict:
    """Facts the desktop app and support need, with no secrets."""
    import platform as _platform

    config = TetherConfig.load()
    server_bin = find_llama_server()
    return {
        "version": __version__,
        "python": _platform.python_version(),
        "python_executable": sys.executable,
        "config_dir": str(CONFIG_DIR),
        "llama_cpp_dir": str(LLAMA_CPP_DIR),
        "llama_server": str(server_bin) if server_bin else None,
        "llama_server_ok": server_bin is not None,
        "provider": config.provider or "local",
        "model": config.model_path if (config.provider or "local") == "local" else config.api_model,
        "gguf_models_found": len(discover_models()),
        "platform": _platform.system().lower(),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    from .ui.colors import bold, dim, success, warning

    print(bold("Tether doctor"))
    print(f"  version        {report['version']}")
    print(f"  python         {report['python']}  {dim(report['python_executable'])}")
    print(f"  config         {report['config_dir']}")
    if report["llama_server_ok"]:
        print(f"  llama-server   {success('built')}  {dim(report['llama_server'])}")
    else:
        print(f"  llama-server   {warning('not built')}  {dim('run: tether setup')}")
    print(f"  provider       {report['provider']} ({report['model'] or 'no model selected'})")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from .ui.colors import bold, dim, error, info, success, warning

    print(bold("Tether Setup"))
    print()

    # Step 1: llama.cpp
    if LLAMA_CPP_DIR.exists():
        print(success(f"llama.cpp found at {LLAMA_CPP_DIR}"))
    else:
        print(info(f"Cloning llama.cpp into {LLAMA_CPP_DIR}..."))
        LLAMA_CPP_DIR.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/ggerganov/llama.cpp.git", str(LLAMA_CPP_DIR)],
            cwd=str(LLAMA_CPP_DIR.parent),
        )
        if result.returncode != 0:
            print(error("Failed to clone llama.cpp"))
            return 1
        print(success("Cloned llama.cpp"))

    # Step 2: Build
    server_bin = find_llama_server()
    if server_bin:
        print(success(f"llama-server found at {server_bin}"))
    else:
        print(info("Building llama.cpp..."))
        build_dir = LLAMA_CPP_DIR / "build"
        build_dir.mkdir(exist_ok=True)

        has_cuda = shutil.which("nvcc") is not None
        cmake_args = ["cmake", ".."]
        if has_cuda:
            cmake_args.append("-DGGML_CUDA=ON")
            print(dim("CUDA detected, building with GPU support"))
        else:
            print(dim("No CUDA detected, building CPU-only"))

        result = subprocess.run(cmake_args, cwd=str(build_dir))
        if result.returncode != 0:
            print(error("cmake failed"))
            return 1

        ncpu = os.cpu_count() or 4
        result = subprocess.run(["cmake", "--build", ".", "--config", "Release", "-j", str(ncpu)], cwd=str(build_dir))
        if result.returncode != 0:
            print(error("Build failed"))
            return 1
        print(success("Built llama.cpp"))

    # Step 3: GGUF model
    model = find_gguf_model()
    if model:
        print(success(f"Model found: {model}"))
    else:
        parts = sorted(GGUF_DIR.glob("*.part*")) if GGUF_DIR.exists() else []
        if parts:
            print(info("Concatenating split GGUF parts..."))
            base_name = parts[0].name.split(".part")[0]
            output = GGUF_DIR / base_name
            with open(output, "wb") as out:
                for part in parts:
                    print(dim(f"  + {part.name}"))
                    with open(part, "rb") as inp:
                        while chunk := inp.read(1024 * 1024 * 64):
                            out.write(chunk)
            print(success(f"Model concatenated: {output}"))
        else:
            print(warning(f"No GGUF model found in {GGUF_DIR}"))
            print(dim("Download a GGUF and place it in the gpt-oss-120b-Derestricted-GGUF/ directory"))
            return 1

    # Step 4: Save config
    config = TetherConfig.load()
    model = find_gguf_model()
    if model:
        config.model_path = str(model)
    config.save()
    print(success(f"Config saved to {CONFIG_DIR / 'config.json'}"))
    print()
    print(bold("Setup complete. Run: tether start"))
    return 0


def _build_server_cmd(config: TetherConfig, model: Path) -> list[str]:
    server_bin = find_llama_server()
    cmd = [
        str(server_bin),
        "-m", str(model),
        "--port", str(config.port),
        "-c", str(config.context_size),
        "-ngl", str(config.gpu_layers),
        "--host", "0.0.0.0",
        "--no-mmap",
        "--flash-attn", "on",
        "--cont-batching",
        "-np", str(config.parallel_slots),
    ]
    if config.cache_type_k != "f16":
        cmd += ["-ctk", config.cache_type_k]
    if config.cache_type_v != "f16":
        cmd += ["-ctv", config.cache_type_v]
    return cmd


def cmd_serve(args: argparse.Namespace) -> int:
    """Start llama-server in the foreground."""
    from .ui.colors import bold, dim, error, info

    config = TetherConfig.load()
    server_bin = find_llama_server()

    if not server_bin:
        print(error("llama-server not found. Run: tether setup"))
        return 1

    model = find_gguf_model()
    if not model and config.model_path:
        model = Path(config.model_path)
    if not model or not model.exists():
        print(error("No GGUF model found. Run: tether setup"))
        return 1

    if args.port:
        config.port = args.port
    if args.context_size:
        config.context_size = args.context_size
    if args.gpu_layers is not None:
        config.gpu_layers = args.gpu_layers

    cmd = _build_server_cmd(config, model)

    print(bold("Starting llama-server..."))
    print(dim(f"  Model:      {model.name}"))
    print(dim(f"  Port:       {config.port}"))
    print(dim(f"  Context:    {config.context_size}"))
    print(dim(f"  GPU layers: {config.gpu_layers}"))
    print(dim("  Slots:      4 (parallel requests)"))
    print(dim("  Flags:      --no-mmap --flash-attn --cont-batching"))
    print()
    print(dim(f"  $ {' '.join(cmd)}"))
    print()

    proc = None
    try:
        proc = subprocess.Popen(cmd)
        proc.wait()
    except KeyboardInterrupt:
        print(f"\n{info('Shutting down llama-server...')}")
        if proc is not None:
            proc.terminate()
            proc.wait()

    return 0


def _server_health_status(config: TetherConfig) -> str:
    """Check server health. Returns 'ok', 'loading', or 'down'."""
    try:
        req = urllib.request.Request(f"{config.server_url}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return "ok"
            return "loading"
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return "loading"
        return "down"
    except (urllib.error.URLError, OSError):
        return "down"


def _tail_last_log_line(log_path: Path) -> str:
    """Read the last non-empty line from the server log."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return ""
            # read last 2KB
            read_size = min(size, 2048)
            f.seek(-read_size, 2)
            data = f.read().decode("utf-8", errors="replace")
            lines = [l.strip() for l in data.splitlines() if l.strip()]
            return lines[-1] if lines else ""
    except OSError:
        return ""


def _format_model_size(model: Path) -> str:
    """Human-readable file size."""
    size = model.stat().st_size
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    return f"{size / (1 << 20):.0f} MB"


def _launch_server_background(config: TetherConfig, model_override: Path | None = None) -> subprocess.Popen | None:
    from .ui.colors import dim, error, warning, CYAN, GREEN, YELLOW, RESET, DIM, BOLD

    server_bin = find_llama_server()
    if not server_bin:
        print(error("llama-server not found. Run: tether setup"))
        return None

    model = model_override
    if not model:
        model = find_gguf_model()
    if not model and config.model_path:
        model = Path(config.model_path)
    if not model or not model.exists():
        print(error("No GGUF model found. Run: tether setup"))
        return None

    cmd = _build_server_cmd(config, model)
    log_path = CONFIG_DIR / "server.log"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # truncate old log so we only see fresh output; the child dups the fd,
    # so the parent's handle is closed right after spawning.
    log_file = open(log_path, "w")

    model_size = _format_model_size(model)

    print()
    print(f"  {BOLD}TETHER{RESET} {DIM}— starting inference engine{RESET}")
    print()
    print(f"  {DIM}model{RESET}    {model.name} ({model_size})")
    print(f"  {DIM}context{RESET}  {config.context_size:,} tokens")
    print(f"  {DIM}gpu{RESET}      {'all layers offloaded' if config.gpu_layers == -1 else f'{config.gpu_layers} layers'}")
    print(f"  {DIM}slots{RESET}    {config.parallel_slots}")
    print()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            # Detach from our process group so the server outlives the CLI.
            # start_new_session is fork-safe; preexec_fn is not when the
            # caller (e.g. the desktop bridge) already has threads running.
            start_new_session=True,
        )
    finally:
        log_file.close()

    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    start_time = time.time()

    for i in range(600):
        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        time_str = f"{mins}:{secs:02d}" if mins else f"{secs}s"
        spinner = spinner_chars[i % len(spinner_chars)]

        # check health
        status = _server_health_status(config)

        if status == "ok":
            # clear the status line and print success
            print(f"\r\033[K  {GREEN}● ready{RESET}  {DIM}{time_str}{RESET}  llama-server listening on port {config.port} (pid {proc.pid})")
            print()
            return proc

        if proc.poll() is not None:
            print(f"\r\033[K  {YELLOW}●{RESET} {error('llama-server crashed during startup')}")
            # show last few log lines
            print()
            try:
                with open(log_path) as f:
                    lines = f.readlines()
                for line in lines[-8:]:
                    print(f"  {DIM}{line.rstrip()}{RESET}")
            except OSError:
                pass
            print()
            print(dim(f"  full log: {log_path}"))
            return None

        # get last log line for context
        log_line = _tail_last_log_line(log_path)

        # determine phase
        if status == "loading":
            phase_label = f"{CYAN}● loading model{RESET}"
        else:
            phase_label = f"{YELLOW}● starting{RESET}"

        # clean up log line for display (truncate to terminal width)
        display_log = ""
        if log_line:
            # strip ANSI and common log prefixes
            clean = log_line
            for prefix in ["SRV  ", "INF  ", "WRN  ", "ERR  "]:
                if prefix in clean:
                    clean = clean[clean.index(prefix) + len(prefix):]
            # truncate
            if len(clean) > 72:
                clean = clean[:69] + "..."
            display_log = f"  {DIM}{clean}{RESET}"

        print(f"\r\033[K  {spinner} {phase_label}  {DIM}{time_str}{RESET}{display_log}", end="", flush=True)
        time.sleep(0.5)

    print(f"\r\033[K  {warning('● timed out waiting for server (5 min)')}")
    print(dim(f"  it may still be loading — check: curl {config.server_url}/health"))
    print(dim(f"  log: {log_path}"))
    return proc


def _resume_target(args: argparse.Namespace) -> str:
    """Resolve the conversation to resume from CLI flags.

    ``tether --continue`` → "@latest" (most recent saved session),
    ``tether --continue <id>`` → that session,
    ``tether start --resume <id>`` / ``start --continue`` also accepted.
    Empty string means start fresh."""
    target = getattr(args, "resume_target", None)
    if target is not None:
        return target or "@latest"
    if getattr(args, "resume", ""):
        return args.resume
    if getattr(args, "resume_last", False):
        return "@latest"
    return ""


def cmd_start(args: argparse.Namespace) -> int:
    """Start server (if needed) + REPL in one command."""
    from .ui.repl import TetherREPL
    from .ui.colors import dim, success

    config = TetherConfig.load()
    if args.port:
        config.port = args.port

    server_proc = None
    extra_dirs = [Path(p) for p in getattr(args, "gguf_dirs", []) or []]

    if config.is_codex:
        from .engine.codex_backend import codex_login_status
        status = codex_login_status()
        if not status.ok:
            from .ui.colors import error
            print(error("Provider 'codex' is configured but Codex CLI is not logged in."))
            print(dim("  run: codex login --device-auth"))
            return 1
        print(success(f"  using Codex CLI provider ({config.api_model})"))
        print()
    elif config.is_remote:
        # Remote provider: no local server to start. Just check that *some*
        # key is present (env var or stored) so we fail fast.
        if not config.has_api_key():
            from .ui.colors import error
            print(error(f"Provider '{config.provider}' is configured but no API key is available."))
            if config.api_key_env:
                print(dim(f"  options:  export {config.api_key_env}=<your key>"))
                print(dim("            tether key set"))
            else:
                print(dim("  store one with:  tether key set"))
            return 1
    elif server_is_running(config):
        print(success(f"Server already running at {config.server_url}"))
    else:
        # Model selection
        if getattr(args, "model", None):
            model_path = Path(args.model)
            if not model_path.exists():
                from .ui.colors import error
                print(error(f"Model not found: {model_path}"))
                return 1
        else:
            model_path = select_model(config, extra_dirs=extra_dirs)
            if model_path is None:
                return 1
        server_proc = _launch_server_background(config, model_override=model_path)
        if server_proc is None:
            return 1

    try:
        repl = TetherREPL(config)
        return repl.run(resume=_resume_target(args))
    finally:
        if server_proc and server_proc.poll() is None:
            print(dim("Leaving server running in background."))
            print(dim("  To stop: tether stop"))


def served_model_path(config: TetherConfig) -> str | None:
    """Return the model id the llama-server on ``config.port`` is serving.

    llama-server reports the ``-m`` path (or alias) as the model id on
    ``/v1/models``. ``None`` when nothing is listening or the shape is not
    recognised (e.g. some other OpenAI-compatible server on the port).
    """
    try:
        req = urllib.request.Request(f"{config.server_url}/v1/models")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    models = data.get("data") if isinstance(data, dict) else None
    if not models or not isinstance(models, list):
        return None
    first = models[0] if isinstance(models[0], dict) else {}
    model_id = first.get("id")
    return str(model_id) if model_id else None


def _listening_pids(port: int) -> list[int]:
    """PIDs listening on ``port`` (not clients connected to it)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
    except OSError:
        return []
    pids = []
    for line in result.stdout.split():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def stop_server_on_port(config: TetherConfig, timeout: float = 8.0) -> bool:
    """SIGTERM whatever listens on the server port and wait for it to go away.

    Escalates to SIGKILL after ``timeout``. Returns True when nothing answers
    ``/health`` afterwards. Used by ``tether stop`` and by the desktop when a
    server started elsewhere holds the port with a different model.
    """
    pids = _listening_pids(config.port)
    if not pids and not server_is_running(config):
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_is_running(config) and not _listening_pids(config.port):
            return True
        time.sleep(0.25)
    for pid in _listening_pids(config.port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return not server_is_running(config) and not _listening_pids(config.port)


def cmd_stop(args: argparse.Namespace) -> int:
    from .ui.colors import dim, error, success

    config = TetherConfig.load()
    if not server_is_running(config) and not _listening_pids(config.port):
        print(dim("No server running."))
        return 0

    try:
        if stop_server_on_port(config):
            print(success(f"Stopped server on port {config.port}"))
        else:
            print(error(f"Server on port {config.port} did not stop"))
    except Exception as e:
        print(error(f"Error stopping server: {e}"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .ui.repl import TetherREPL
    from .ui.colors import success, dim

    config = TetherConfig.load()
    if args.port:
        config.port = args.port

    server_proc = None
    extra_dirs = [Path(p) for p in getattr(args, "gguf_dirs", []) or []]

    if config.is_codex:
        from .engine.codex_backend import codex_login_status
        status = codex_login_status()
        if not status.ok:
            from .ui.colors import error
            print(error("Provider 'codex' is configured but Codex CLI is not logged in."))
            print(dim("  run: codex login --device-auth"))
            return 1
        print(success(f"  using Codex CLI provider ({config.api_model})"))
        print()
    elif config.is_remote:
        # Remote provider: no local server to start. Just check that *some*
        # key is present (env var or stored) so we fail fast.
        if not config.has_api_key():
            from .ui.colors import error
            print(error(f"Provider '{config.provider}' is configured but no API key is available."))
            if config.api_key_env:
                print(dim(f"  options:  export {config.api_key_env}=<your key>"))
                print(dim("            tether key set"))
            else:
                print(dim("  store one with:  tether key set"))
            return 1
    elif server_is_running(config):
        print(success(f"  server already running at {config.server_url}"))
        print()
    else:
        # Model selection
        if getattr(args, "model", None):
            model_path = Path(args.model)
            if not model_path.exists():
                from .ui.colors import error
                print(error(f"Model not found: {model_path}"))
                return 1
        else:
            model_path = select_model(config, extra_dirs=extra_dirs)
            if model_path is None:
                return 1
        server_proc = _launch_server_background(config, model_override=model_path)
        if server_proc is None:
            return 1

    try:
        repl = TetherREPL(config)
        return repl.run(resume=_resume_target(args))
    finally:
        if server_proc and server_proc.poll() is None:
            print(dim("  server still running in background — stop with: tether stop"))
            print()


def cmd_remote(args: argparse.Namespace) -> int:
    """Configure or inspect the remote API provider."""
    from .ui.colors import bold, dim, error, success, warning

    config = TetherConfig.load()
    target = (args.provider or "").strip().lower()

    # No arg -> show status
    if not target:
        if config.is_codex:
            from .engine.codex_backend import codex_login_status
            print(f"  {bold('provider')}     codex")
            print(f"  {bold('model')}        {config.api_model}")
            status = codex_login_status()
            state = success(status.output) if status.ok else warning(status.output)
            print(f"  {bold('login')}        {state}")
        elif config.is_remote:
            print(f"  {bold('provider')}     {config.provider}")
            print(f"  {bold('base url')}     {config.api_base_url}")
            print(f"  {bold('model')}        {config.api_model}")
            env_label = f"${config.api_key_env}" if config.api_key_env else "(no env var)"
            print(f"  {bold('api key env')}  {env_label}")
            env_set = any(
                os.environ.get(name, "").strip()
                for name in config.api_key_env_names()
            )
            stored_set = bool(config.stored_api_key())
            if env_set:
                print(f"  {bold('api key')}      {success('env var set')}")
            elif stored_set:
                print(f"  {bold('api key')}      {success('stored in config')}")
            else:
                print(f"  {bold('api key')}      {warning('NOT SET')}")
            effort = config.reasoning_effort or dim("off")
            print(f"  {bold('thinking')}     {effort}")
        else:
            print(dim("  provider: local (using llama-server)"))
            preset_names = ", ".join(sorted(REMOTE_PROVIDERS)) or "(none)"
            print(dim(f"  available presets: {preset_names}"))
            print(dim("  switch with:  tether remote <name>"))
            print(dim("  or generic:   tether remote custom --base-url URL --model NAME --api-key-env VAR"))
        return 0

    if target in ("off", "local", "none"):
        apply_provider_selection(config, "local")
        config.save()
        print(success("  switched back to local llama-server"))
        return 0

    if target == "custom":
        if not (args.base_url and args.model and args.api_key_env):
            print(error("  custom requires --base-url, --model, and --api-key-env"))
            return 1
        config.provider = "custom"
        config.api_base_url = args.base_url.rstrip("/")
        config.api_model = args.model
        config.api_key_env = args.api_key_env
    elif target in REMOTE_PROVIDERS:
        preset = REMOTE_PROVIDERS[target]
        # Stash local context_size on first transition to remote so we can
        # restore it on `tether remote off`.
        if not config.is_remote and not config.local_context_size:
            config.local_context_size = config.context_size
        config.provider = target
        config.api_base_url = preset["api_base_url"]
        config.api_model = args.model or preset["api_model"]
        config.api_key_env = preset["api_key_env"]
        if "context_size" in preset:
            config.context_size = preset["context_size"]
        if "max_budget_tokens" in preset:
            config.max_budget_tokens = preset["max_budget_tokens"]
        # Catalog plus whatever the provider reports live (needs a key).
        if provider_model(target, config.api_model, config) is not None:
            apply_provider_selection(
                config,
                target,
                config.api_model,
                reasoning_effort=args.reasoning_effort,
            )
    else:
        preset_names = ", ".join(sorted(REMOTE_PROVIDERS))
        print(error(f"  unknown provider: {target}"))
        print(dim(f"  known: {preset_names}, custom, off"))
        return 1

    # Reasoning effort precedence: explicit flag > preset default > model-name
    # heuristic > off. Pass --reasoning-effort "" to clear it.
    catalog_model = provider_model(target, config.api_model, config) if target in REMOTE_PROVIDERS else None
    if args.reasoning_effort is not None:
        config.reasoning_effort = args.reasoning_effort
    elif catalog_model is not None:
        # apply_provider_selection already chose the model's documented default.
        pass
    elif target in REMOTE_PROVIDERS and "reasoning_effort" in REMOTE_PROVIDERS[target]:
        config.reasoning_effort = REMOTE_PROVIDERS[target]["reasoning_effort"]
    elif "reasoner" in config.api_model.lower() or "thinking" in config.api_model.lower():
        config.reasoning_effort = "high"
    else:
        config.reasoning_effort = ""

    config.save()
    print(success(f"  provider set to {config.provider} ({config.api_model})"))
    if config.is_codex:
        from .engine.codex_backend import codex_login_status
        status = codex_login_status()
        if status.ok:
            print(dim(f"  {status.output}"))
        else:
            print(warning("  Codex CLI is not logged in"))
            print(dim("  run: codex login --device-auth"))
        return 0
    if config.reasoning_effort:
        print(dim(f"  thinking mode: reasoning_effort={config.reasoning_effort} (no temperature/top_p)"))

    env_key = next(
        (os.environ.get(name, "").strip() for name in config.api_key_env_names() if os.environ.get(name, "").strip()),
        "",
    )
    stored_key = config.stored_api_key()
    if not env_key and not stored_key:
        # Offer to capture the key right now so the user isn't sent back
        # to fight a shell. Only when running interactively.
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                import getpass
                prompt = f"  store API key for {config.provider} now? [y/N]: "
                ans = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
                print()
            if ans in ("y", "yes"):
                try:
                    key = getpass.getpass("  enter API key (input hidden): ").strip()
                except (EOFError, KeyboardInterrupt):
                    key = ""
                    print()
                if key:
                    config.api_keys[config.provider] = key
                    config.save()
                    print(success(f"  key stored in {CONFIG_FILE} (mode 0600)"))
                    return 0
        if config.api_key_env:
            print(warning(f"  warning: ${config.api_key_env} is not set"))
            print(dim(f"  options:  export {config.api_key_env}=<your key>"))
            print(dim("            tether key set"))
        else:
            print(warning("  no API key configured"))
            print(dim("  store one with:  tether key set"))
    elif not env_key and stored_key:
        print(dim("  using stored API key (no env var set)"))
    return 0


def config_for_display(config: TetherConfig) -> dict:
    """Return CLI configuration status with every stored credential redacted."""
    data = dict(config.__dict__)
    if data.get("api_key"):
        data["api_key"] = "***"
    api_keys = data.get("api_keys")
    if isinstance(api_keys, dict):
        data["api_keys"] = {
            str(provider): "***" if value else ""
            for provider, value in api_keys.items()
        }
    return data


def cmd_key(args: argparse.Namespace) -> int:
    """Manage the stored API key (fallback when $api_key_env is unset)."""
    import getpass

    from .ui.colors import bold, dim, error, success, warning

    config = TetherConfig.load()
    action = (args.action or "show").strip().lower()

    if action == "show":
        if config.is_codex:
            print(dim("  provider: codex — no API key needed"))
            print(dim("  auth:     codex login --device-auth"))
            return 0
        if not config.is_remote:
            print(dim("  provider: local — no key needed"))
            return 0
        env_set = any(
            os.environ.get(name, "").strip()
            for name in config.api_key_env_names()
        )
        stored = bool(config.stored_api_key())
        env_label = config.api_key_env or "(no env var configured)"
        env_status = success("set") if env_set else dim("not set")
        stored_status = success("set") if stored else dim("not set")
        print(f"  {bold('provider')}     {config.provider}")
        print(f"  {bold('env var')}      ${env_label}: {env_status}")
        print(f"  {bold('stored key')}   {stored_status}")
        if not env_set and not stored:
            print(warning("  no key available — requests will be unauthenticated"))
            print(dim("  fix with:  tether key set"))
        return 0

    if action == "clear":
        if not config.stored_api_key():
            print(dim("  no stored key to clear"))
            return 0
        config.api_keys.pop(config.provider, None)
        config.api_key = ""
        config.save()
        print(success("  stored key cleared"))
        return 0

    if action == "set":
        if config.is_codex:
            print(error("  codex uses Codex CLI login, not an API key"))
            print(dim("  run: codex login --device-auth"))
            return 1
        if not config.is_remote:
            print(error("  no remote provider configured"))
            print(dim("  switch first:  tether remote <name>"))
            return 1
        # Allow piping the key in (`echo $KEY | tether key set`) for
        # scripted setups; otherwise prompt without echo.
        if not sys.stdin.isatty():
            key = sys.stdin.read().strip()
        else:
            label = config.api_key_env or config.provider
            try:
                key = getpass.getpass(f"  enter API key for {label} (input hidden): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
        if not key:
            print(error("  empty key — aborting"))
            return 1
        config.api_keys[config.provider] = key
        config.save()
        print(success(f"  key stored in {CONFIG_FILE} (mode 0600)"))
        if any(os.environ.get(name, "").strip() for name in config.api_key_env_names()):
            print(dim(f"  note: ${config.api_key_env} is also set — env var takes precedence"))
        return 0

    print(error(f"  unknown action: {action}"))
    print(dim("  expected: set, show, clear"))
    return 1


def cmd_models(args: argparse.Namespace) -> int:
    """Manage user-supplied GGUF model directories."""
    from .ui.colors import bold, dim, error, success, warning

    config = TetherConfig.load()
    action = (args.action or "list").strip().lower()

    if action in ("list", "list-dirs"):
        builtin = [str(d) for d in LOCAL_GGUF_DIRS]
        user = list(config.gguf_dirs)
        print(f"  {bold('built-in search dirs')}")
        for d in builtin:
            exists = "" if Path(d).expanduser().is_dir() else dim(" (missing)")
            print(f"    {d}{exists}")
        print(f"  {bold('user-added dirs')}")
        if not user:
            print(dim("    (none — add one with: tether models add-dir <path>)"))
        else:
            for d in user:
                exists = "" if Path(d).expanduser().is_dir() else dim(" (missing)")
                print(f"    {d}{exists}")
        return 0

    if action in ("add", "add-dir"):
        if not args.path:
            print(error("  add-dir requires a path"))
            return 1
        path = Path(args.path).expanduser().resolve()
        if not path.is_dir():
            print(error(f"  not a directory: {path}"))
            return 1
        existing = {Path(p).expanduser().resolve() for p in config.gguf_dirs}
        if path in existing:
            print(dim(f"  already added: {path}"))
            return 0
        config.gguf_dirs.append(str(path))
        config.save()
        print(success(f"  added: {path}"))
        # Quick scan to give immediate feedback
        ggufs = _find_gguf_in_dir(path)
        if ggufs:
            print(dim(f"  found {len(ggufs)} GGUF file(s) under this directory"))
        else:
            print(warning("  no .gguf files found yet — drop one in and re-run model selection"))
        return 0

    if action in ("remove", "remove-dir", "rm"):
        if not args.path:
            print(error("  remove-dir requires a path"))
            return 1
        target = Path(args.path).expanduser().resolve()
        kept: list[str] = []
        removed = False
        for entry in config.gguf_dirs:
            if Path(entry).expanduser().resolve() == target:
                removed = True
                continue
            kept.append(entry)
        if not removed:
            print(error(f"  not in user list: {target}"))
            return 1
        config.gguf_dirs = kept
        config.save()
        print(success(f"  removed: {target}"))
        return 0

    print(error(f"  unknown action: {action}"))
    print(dim("  expected: list, add-dir <path>, remove-dir <path>"))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tether",
        description="Tether — local-first AI coding agent",
    )
    parser.add_argument(
        "--continue", nargs="?", const="", default=None, dest="resume_target",
        metavar="SESSION_ID",
        help="Start the REPL resuming a saved conversation "
             "(no id = most recent; list them with /resume inside the REPL)",
    )
    parser.add_argument("--version", action="version", version=f"tether {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start server (if needed) + REPL")
    start_parser.add_argument("--port", type=int, help="Server port")
    start_parser.add_argument("--model", type=str, help="Path to GGUF model (skip interactive selector)")
    start_parser.add_argument(
        "--continue", action="store_true", dest="resume_last",
        help="Resume the most recent saved conversation",
    )
    start_parser.add_argument(
        "--resume", type=str, default="", metavar="SESSION_ID",
        help="Resume a specific saved conversation (see /resume inside the REPL for the list)",
    )
    start_parser.add_argument(
        "--gguf-dir",
        action="append",
        default=[],
        dest="gguf_dirs",
        help="Extra GGUF directory to scan for this run (can pass multiple times)",
    )

    run_parser = subparsers.add_parser("run", help="Start server (if needed) + REPL")
    run_parser.add_argument("--port", type=int, help="Server port")
    run_parser.add_argument("--model", type=str, help="Path to GGUF model (skip interactive selector)")
    run_parser.add_argument(
        "--continue", action="store_true", dest="resume_last",
        help="Resume the most recent saved conversation",
    )
    run_parser.add_argument(
        "--resume", type=str, default="", metavar="SESSION_ID",
        help="Resume a specific saved conversation (see /resume inside the REPL for the list)",
    )
    run_parser.add_argument(
        "--gguf-dir",
        action="append",
        default=[],
        dest="gguf_dirs",
        help="Extra GGUF directory to scan for this run (can pass multiple times)",
    )

    serve_parser = subparsers.add_parser("serve", help="Start llama-server in foreground")
    serve_parser.add_argument("--port", type=int, help="Port (default: 8080)")
    serve_parser.add_argument("--context-size", type=int, help="Context size")
    serve_parser.add_argument("--gpu-layers", type=int, help="GPU layers (-1 = all)")

    subparsers.add_parser("stop", help="Stop background server")
    subparsers.add_parser("setup", help="Clone llama.cpp, build, and configure")
    subparsers.add_parser("config", help="Show current configuration")
    doctor_parser = subparsers.add_parser(
        "doctor", help="Report what is installed (Python, llama-server, config); --json for tools"
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable output")

    remote_parser = subparsers.add_parser(
        "remote",
        help="Configure a remote API provider (e.g. deepseek)",
    )
    remote_parser.add_argument(
        "provider",
        nargs="?",
        help="Preset name (deepseek), 'custom', 'off' to revert to local, or omit to show status",
    )
    remote_parser.add_argument("--model", help="Override the model name sent to the API")
    remote_parser.add_argument("--base-url", help="Custom: API base URL (e.g. https://api.example.com)")
    remote_parser.add_argument("--api-key-env", help="Custom: env var name holding the API key")
    remote_parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        default=None,
        help="Enable thinking mode: 'high' or 'max' (or '' to disable). "
             "Drops temperature/top_p; auto-set for *reasoner* models.",
    )

    key_parser = subparsers.add_parser(
        "key",
        help="Manage stored API key (fallback when $api_key_env is unset)",
    )
    key_parser.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["set", "show", "clear"],
        help="set: prompt and store key; show: report status; clear: remove stored key",
    )

    models_parser = subparsers.add_parser(
        "models",
        help="Manage GGUF model search directories",
    )
    models_parser.add_argument(
        "action",
        nargs="?",
        default="list",
        help="list | add-dir <path> | remove-dir <path>",
    )
    models_parser.add_argument("path", nargs="?", help="Directory path for add-dir / remove-dir")

    # Internal structured transport for the native macOS app. It is a separate
    # command instead of a flag on `start` so the terminal REPL remains stable.
    app_bridge_parser = subparsers.add_parser(
        "app-bridge",
        help=argparse.SUPPRESS,
    )
    app_bridge_parser.add_argument("--project", required=True, help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Set up logging early
    setup_logging()
    logger = get_logger(__name__)
    
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        return cmd_start(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "serve":
        return cmd_serve(args)
    elif args.command == "stop":
        return cmd_stop(args)
    elif args.command == "setup":
        return cmd_setup(args)
    elif args.command == "doctor":
        return cmd_doctor(args)
    elif args.command == "remote":
        return cmd_remote(args)
    elif args.command == "key":
        return cmd_key(args)
    elif args.command == "models":
        return cmd_models(args)
    elif args.command == "config":
        config = TetherConfig.load()
        import json
        print(json.dumps(config_for_display(config), indent=2))
        return 0
    elif args.command == "app-bridge":
        from .app_bridge import run_app_bridge
        return run_app_bridge(args)
    else:
        args.port = None
        args.model = None
        return cmd_start(args)
