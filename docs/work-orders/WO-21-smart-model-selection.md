# WO-21 — Smart Model Selection & Context Window Config

## Goal

Make the setup wizard hardware-aware so it can recommend the best Ollama model for the user's GPU, and add a configurable context window cap (`num_ctx`) to prevent OOM errors when running larger models.

## Background

llm-mem's extraction, summarization, and sensitive data scanning use a local Ollama model. On a 24GB GPU (e.g. RTX 4090), the model weights + KV cache for context compete for VRAM. A 14B model at Q4 uses ~10GB, leaving ~14GB for context — plenty. A 30B model at Q4 uses ~20GB, leaving only ~4GB — enough for ~4k context but OOM at Ollama's default 32k.

Currently:
- No `num_ctx` is passed to Ollama, so it uses its auto-detected default (32k on 24GB cards)
- The setup wizard lists available Ollama models but gives no guidance on which will fit
- Users hit OOM with larger models and don't know why

## Deliverables

### 1. System scan during setup

When the user selects `ollama` as backend, the setup wizard should:

a) **Detect GPU info** — run `nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits` (or fall back gracefully if no NVIDIA GPU). Parse total VRAM and free VRAM.

b) **Detect system RAM** — read from `/proc/meminfo` (MemTotal).

c) **List Ollama models with sizes** — call `GET /api/tags` from the Ollama endpoint. Each model in the response has a `size` field (bytes on disk, roughly correlates to VRAM at the model's quantization level). Use this as the VRAM estimate for model weights.

d) **Recommend models** — for each available model, estimate whether it fits:
   - Model VRAM ≈ `size` from Ollama API (this is the quantized weight size, close to runtime VRAM)
   - KV cache VRAM ≈ varies by context length; rough formula: `num_params_billions * 0.5MB * (num_ctx / 1024)` for Q4 models (approximate, good enough for recommendations)
   - If `model_vram + kv_cache_vram < gpu_total * 0.9` → fits comfortably
   - If `model_vram + kv_cache_vram < gpu_total` → fits but tight
   - Otherwise → won't fit, suggest reducing context or picking smaller model

e) **Display recommendations** — show a table during setup:
   ```
   GPU: NVIDIA RTX 4090 (24576 MB total, 22100 MB free)
   RAM: 64 GB

   Available models:
     gemma4:e4b      5.1 GB   ✅ fits easily (19 GB free for context)
     qwen3:14b       9.8 GB   ✅ fits well  (14 GB free for context)
     qwen3:30b      19.2 GB   ⚠️  tight — recommend num_ctx ≤ 8192
     gemma4:26b     17.8 GB   ⚠️  tight — recommend num_ctx ≤ 8192
     qwen3:30b      20.1 GB   ❌ likely OOM at default context

   Recommended: qwen3:14b (best quality/VRAM balance)
   ```

### 2. Context window config (`num_ctx`)

a) **Add `num_ctx` to `OllamaConfig`** in `src/llm_mem/models/config.py`:
   ```python
   class OllamaConfig(BaseModel):
       model: str = "qwen3:8b"
       endpoint: str = "http://localhost:11434"
       timeout: int = 120
       num_ctx: int | None = None  # None = let Ollama auto-detect
   ```

b) **Pass `num_ctx` in Ollama requests** — update `_generate()` in `src/llm_mem/core/ollama.py`:
   ```python
   def __init__(self, endpoint, model, timeout, num_ctx=None):
       ...
       self.num_ctx = num_ctx

   def _generate(self, prompt):
       body = {"model": self.model, "prompt": prompt, "stream": False}
       if self.num_ctx is not None:
           body["options"] = {"num_ctx": self.num_ctx}
       ...
   ```

c) **Wire config through** — wherever `OllamaClient` is instantiated, pass `num_ctx` from config.

d) **Add to config.toml template**:
   ```toml
   [ollama]
   model = "qwen3:14b"
   endpoint = "http://localhost:11434"
   timeout = 120
   # num_ctx = 8192  # Uncomment to cap context window (reduces VRAM usage)
   ```

### 3. Setup wizard integration

a) **Show `num_ctx` prompt only when relevant** — after the user picks a model, if the estimated model VRAM + default context cache exceeds 85% of GPU VRAM, prompt:
   ```
   ⚠️  qwen3:30b uses ~19 GB — leaving ~5 GB for context.
   llm-mem extraction batches are small, so a reduced context window works fine.

   Context window (num_ctx) [recommended: 8192]:
   ```

   If the model fits comfortably, don't ask — leave `num_ctx` unset (Ollama auto-detect).

b) **Auto-suggest a safe `num_ctx`** — calculate a value that keeps total VRAM under 90% of GPU total. The default suggestion should be the largest power-of-2 context that fits.

### 4. Graceful fallbacks

- If `nvidia-smi` is not available (no NVIDIA GPU, or CPU-only Ollama), skip GPU detection and show models without VRAM estimates. Don't block setup.
- If Ollama API doesn't return model sizes (older versions), skip size display.
- If `num_ctx` is set in config but the backend is `openai_compat` or `none`, ignore it silently.

## Constraints

- Python 3.10 compatible (no `datetime.UTC`, use `from llm_mem.compat import UTC`; `typing_extensions` for `Self`)
- Setup must remain safe to re-run — if `num_ctx` is already set in config, show it as default
- No AI attribution in code or comments
- Keep the setup UX clean — the GPU scan and recommendations should feel helpful, not overwhelming. Users who don't understand VRAM should be able to just accept the recommended model and move on.

## Acceptance criteria

- [ ] `llm-mem setup` detects GPU VRAM and system RAM when Ollama backend is selected
- [ ] Setup lists available Ollama models with VRAM estimates and fit indicators
- [ ] Setup recommends a model based on available VRAM
- [ ] `num_ctx` prompt appears only when the chosen model is tight on VRAM
- [ ] `num_ctx` is written to config.toml and passed through to Ollama API calls
- [ ] Existing setups without `num_ctx` continue to work (Ollama auto-detect)
- [ ] Setup degrades gracefully when nvidia-smi is unavailable
- [ ] All existing tests pass
- [ ] Works on Python 3.10

## Suggested tests

- Unit test for GPU detection parsing (mock nvidia-smi output)
- Unit test for model recommendation logic (given VRAM + model sizes → expected recommendations)
- Unit test for `OllamaClient._generate()` passing `num_ctx` in options
- Integration test for config round-trip (write config with num_ctx, reload, verify)
