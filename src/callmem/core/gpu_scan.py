"""GPU detection and model recommendation for setup wizard.

Detects GPU VRAM, system RAM, and Ollama model sizes to recommend
the best model and context window configuration.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Quality bonuses for models known to produce good structured JSON extraction
_QUALITY_BONUS: dict[str, int] = {
    "qwen3:14b": 100,
    "qwen3:14b-fast": 100,
    "qwen2.5:14b": 95,
    "qwen3:30b": 110,
    "qwen3.5:35b": 110,
    "qwen3.5:9b": 70,
    "qwen3:8b": 65,
    "qwen2.5:7b": 60,
    "llama3:8b": 55,
    "mistral:instruct": 55,
}


def _estimate_param_billions(name: str) -> float:
    """Estimate parameter count in billions from model name."""
    name_lower = name.lower()
    # Handle e4b/e2b patterns (Google efficiency models)
    if "e4b" in name_lower:
        return 4.0
    if "e2b" in name_lower:
        return 2.0
    # Look for NNb pattern
    match = re.search(r"(\d+\.?\d*)b", name_lower)
    if match:
        return float(match.group(1))
    # Common defaults
    if "mistral" in name_lower and "instruct" in name_lower:
        return 7.0
    return 0.0


def _quality_score(model: ModelInfo) -> int:
    """Score model quality for extraction tasks. Higher = better."""
    if model.name in _QUALITY_BONUS:
        return _QUALITY_BONUS[model.name]

    # Fall back to parameter count estimate
    params = _estimate_param_billions(model.name)
    if params >= 14:
        return 90
    if params >= 8:
        return 60
    if params >= 4:
        return 40
    if params >= 2:
        return 20
    return 10


@dataclass
class GPUInfo:
    name: str = ""
    total_vram_mb: int = 0
    free_vram_mb: int = 0

    @property
    def available(self) -> bool:
        return bool(self.name) and self.total_vram_mb > 0


@dataclass
class SystemInfo:
    gpu: GPUInfo = field(default_factory=GPUInfo)
    ram_mb: int = 0


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0
    size_gb: float = 0.0

    def __post_init__(self) -> None:
        if self.size_bytes and not self.size_gb:
            self.size_gb = self.size_bytes / (1024**3)


@dataclass
class ModelRecommendation:
    model: ModelInfo
    fit_status: str  # "easy", "ok", "tight", "oom"
    free_after_mb: int = 0
    recommended_ctx: int | None = None
    note: str = ""


def detect_gpu() -> GPUInfo:
    """Detect NVIDIA GPU info via nvidia-smi. Returns empty GPUInfo if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return GPUInfo()

        lines = result.stdout.strip().splitlines()
        if not lines:
            return GPUInfo()

        first = lines[0].strip()
        parts = [p.strip() for p in first.split(",")]
        if len(parts) < 3:
            return GPUInfo()

        return GPUInfo(
            name=parts[0],
            total_vram_mb=int(float(parts[1])),
            free_vram_mb=int(float(parts[2])),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return GPUInfo()


def detect_ram() -> int:
    """Detect system RAM in MB from /proc/meminfo. Returns 0 on failure."""
    try:
        content = Path("/proc/meminfo").read_text()
        match = re.search(r"MemTotal:\s+(\d+)\s+kB", content)
        if match:
            return int(match.group(1)) // 1024
    except (OSError, ValueError):
        pass
    return 0


def detect_system() -> SystemInfo:
    """Detect full system info (GPU + RAM)."""
    return SystemInfo(
        gpu=detect_gpu(),
        ram_mb=detect_ram(),
    )


def fetch_ollama_models(endpoint: str) -> list[ModelInfo]:
    """Fetch available models and their sizes from Ollama API."""
    try:
        resp = httpx.get(f"{endpoint.rstrip('/')}/api/tags", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models: list[ModelInfo] = []
        for m in data.get("models", []):
            models.append(ModelInfo(
                name=m.get("name", "unknown"),
                size_bytes=m.get("size", 0),
            ))
        return models
    except (httpx.HTTPError, OSError):
        return []


def _estimate_kv_cache_mb(model_size_bytes: int, num_ctx: int) -> int:
    """Estimate KV cache VRAM for a model at a given context length.

    Rule of thumb: for Q4 models, KV cache at 8k context ≈ 25% of model size.
    Scales linearly with context length.
    """
    size_gb = model_size_bytes / (1024**3)
    kv_8k_gb = size_gb * 0.25
    kv_target_gb = kv_8k_gb * (num_ctx / 8192)
    return int(kv_target_gb * 1024)


def _largest_power_of_2_fits(
    model_vram_mb: int, gpu_total_mb: int, target_pct: float = 0.9,
) -> int | None:
    """Find largest power-of-2 num_ctx that keeps total VRAM under target_pct of GPU."""
    for ctx in [32768, 16384, 8192, 4096, 2048, 1024]:
        kv_mb = _estimate_kv_cache_mb(int(model_vram_mb * 1024**2 / 1024**2 * 1024**2), ctx)
        if model_vram_mb + kv_mb < gpu_total_mb * target_pct:
            return ctx
    return 1024


def recommend_models(
    models: list[ModelInfo],
    gpu: GPUInfo,
    default_ctx: int = 32768,
) -> list[ModelRecommendation]:
    """Generate recommendations for each model based on GPU VRAM."""
    if not gpu.available:
        return [
            ModelRecommendation(model=m, fit_status="ok", note="No GPU info — cannot estimate fit")
            for m in models
        ]

    results: list[ModelRecommendation] = []
    model_vram_mb = 0
    for m in models:
        model_vram_mb = int(m.size_gb * 1024)
        default_kv = _estimate_kv_cache_mb(m.size_bytes, default_ctx)
        total_default = model_vram_mb + default_kv
        free_after = gpu.total_vram_mb - model_vram_mb

        if total_default < gpu.total_vram_mb * 0.75:
            results.append(ModelRecommendation(
                model=m,
                fit_status="easy",
                free_after_mb=free_after - default_kv,
                note="fits easily",
            ))
        elif total_default < gpu.total_vram_mb * 0.9:
            results.append(ModelRecommendation(
                model=m,
                fit_status="ok",
                free_after_mb=free_after - default_kv,
                note="fits well",
            ))
        elif total_default < gpu.total_vram_mb:
            safe_ctx = _largest_power_of_2_fits(model_vram_mb, gpu.total_vram_mb, 0.9)
            results.append(ModelRecommendation(
                model=m,
                fit_status="tight",
                free_after_mb=free_after,
                recommended_ctx=safe_ctx,
                note=f"tight — recommend num_ctx \u2264 {safe_ctx or 4096}",
            ))
        else:
            safe_ctx = _largest_power_of_2_fits(model_vram_mb, gpu.total_vram_mb, 0.9)
            results.append(ModelRecommendation(
                model=m,
                fit_status="oom",
                free_after_mb=free_after,
                recommended_ctx=safe_ctx,
                note="likely OOM at default context",
            ))

    return results


def pick_best(recommendations: list[ModelRecommendation]) -> ModelRecommendation | None:
    """Pick the best model recommendation — highest quality that fits comfortably."""
    candidates = [r for r in recommendations if r.fit_status in ("easy", "ok")]
    if not candidates:
        tight = [r for r in recommendations if r.fit_status == "tight"]
        if tight:
            tight.sort(key=lambda r: _quality_score(r.model), reverse=True)
            return tight[0]
        return None

    # Sort by quality score (primary), then by VRAM headroom (secondary)
    candidates.sort(
        key=lambda r: (_quality_score(r.model), r.free_after_mb),
        reverse=True,
    )
    return candidates[0]


def format_recommendation_table(
    gpu: GPUInfo,
    ram_mb: int,
    recommendations: list[ModelRecommendation],
) -> str:
    """Format recommendations as a human-readable table for the setup wizard."""
    lines: list[str] = []

    if gpu.available:
        lines.append(
            f"  GPU: {gpu.name} "
            f"({gpu.total_vram_mb} MB total, {gpu.free_vram_mb} MB free)"
        )
    if ram_mb:
        lines.append(f"  RAM: {ram_mb // 1024} GB")

    lines.append("")
    lines.append("  Available models:")

    indicators = {
        "easy": "\u2705",
        "ok": "\u2705",
        "tight": "\u26a0\ufe0f ",
        "oom": "\u274c",
    }

    for r in recommendations:
        indicator = indicators.get(r.fit_status, "?")
        size_str = f"{r.model.size_gb:.1f} GB" if r.model.size_gb else "size unknown"
        free_str = f"({r.free_after_mb} MB free for context)" if gpu.available else ""
        lines.append(f"    {r.model.name:<25s} {size_str:<10s} {indicator} {r.note} {free_str}")

    return "\n".join(lines)
