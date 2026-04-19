"""Tests for GPU scan, model recommendation, and num_ctx passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from callmem.core.gpu_scan import (
    GPUInfo,
    ModelInfo,
    ModelRecommendation,
    _estimate_param_billions,
    _quality_score,
    detect_gpu,
    detect_ram,
    fetch_ollama_models,
    format_recommendation_table,
    pick_best,
    recommend_models,
)
from callmem.core.ollama import OllamaClient


class TestGPUDetection:
    def test_parses_nvidia_smi_output(self) -> None:
        with patch("callmem.core.gpu_scan.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="NVIDIA RTX 4090, 24576, 22100\n",
            )
            gpu = detect_gpu()
            assert gpu.name == "NVIDIA RTX 4090"
            assert gpu.total_vram_mb == 24576
            assert gpu.free_vram_mb == 22100
            assert gpu.available

    def test_returns_empty_when_nvidia_smi_missing(self) -> None:
        with patch("callmem.core.gpu_scan.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            gpu = detect_gpu()
            assert not gpu.available

    def test_returns_empty_on_bad_output(self) -> None:
        with patch("callmem.core.gpu_scan.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            gpu = detect_gpu()
            assert not gpu.available


class TestRAMDetection:
    def test_parses_proc_meminfo(self, tmp_path: str) -> None:
        with patch("callmem.core.gpu_scan.Path") as mock_path:
            mock_path.return_value.read_text.return_value = (
                "MemTotal:       65789012 kB\nMemFree:        32100000 kB\n"
            )
            ram = detect_ram()
            assert ram == 64247  # 65789012 / 1024

    def test_returns_zero_on_failure(self) -> None:
        with patch("callmem.core.gpu_scan.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = OSError
            ram = detect_ram()
            assert ram == 0


class TestFetchOllamaModels:
    def test_parses_model_list(self) -> None:
        with patch("callmem.core.gpu_scan.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "models": [
                    {"name": "qwen3:14b", "size": 9900000000},
                    {"name": "gemma4:e4b", "size": 5100000000},
                ]
            }
            mock_get.return_value.raise_for_status = MagicMock()
            models = fetch_ollama_models("http://localhost:11434")
            assert len(models) == 2
            assert models[0].name == "qwen3:14b"
            assert models[0].size_bytes == 9900000000
            assert models[0].size_gb > 0

    def test_returns_empty_on_failure(self) -> None:
        import httpx

        with patch("callmem.core.gpu_scan.httpx.get") as mock_get:
            mock_get.side_effect = httpx.ConnectError("refused")
            models = fetch_ollama_models("http://localhost:11434")
            assert models == []


class TestModelRecommendation:
    def test_easy_fit(self) -> None:
        gpu = GPUInfo(name="RTX 4090", total_vram_mb=24576, free_vram_mb=22100)
        models = [ModelInfo(name="gemma4:e4b", size_bytes=5_100_000_000)]
        recs = recommend_models(models, gpu)
        assert len(recs) == 1
        assert recs[0].fit_status == "easy"

    def test_ok_fit(self) -> None:
        gpu = GPUInfo(name="RTX 4090", total_vram_mb=24576, free_vram_mb=22100)
        models = [ModelInfo(name="qwen3:14b", size_bytes=9_800_000_000)]
        recs = recommend_models(models, gpu)
        assert len(recs) == 1
        assert recs[0].fit_status in ("easy", "ok")

    def test_tight_fit(self) -> None:
        gpu = GPUInfo(name="RTX 4090", total_vram_mb=24576, free_vram_mb=22100)
        models = [ModelInfo(name="qwen3:30b", size_bytes=19_200_000_000)]
        recs = recommend_models(models, gpu)
        assert len(recs) == 1
        assert recs[0].fit_status in ("tight", "oom")
        assert recs[0].recommended_ctx is not None

    def test_oom_fit(self) -> None:
        gpu = GPUInfo(name="RTX 3060", total_vram_mb=12288, free_vram_mb=10000)
        models = [ModelInfo(name="qwen3:30b", size_bytes=20_100_000_000)]
        recs = recommend_models(models, gpu)
        assert len(recs) == 1
        assert recs[0].fit_status == "oom"

    def test_no_gpu_returns_ok(self) -> None:
        gpu = GPUInfo()
        models = [ModelInfo(name="qwen3:14b", size_bytes=9_800_000_000)]
        recs = recommend_models(models, gpu)
        assert len(recs) == 1
        assert recs[0].fit_status == "ok"

    def test_pick_best_prefers_quality_over_size(self) -> None:
        """qwen3:14b should beat gemma4:e4b even though e4b is larger in bytes."""
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="gemma4:e4b", size_bytes=9_500_000_000),
                fit_status="easy",
                free_after_mb=15000,
            ),
            ModelRecommendation(
                model=ModelInfo(name="qwen3:14b", size_bytes=9_200_000_000),
                fit_status="ok",
                free_after_mb=14000,
            ),
        ]
        best = pick_best(recs)
        assert best is not None
        assert best.model.name == "qwen3:14b"

    def test_pick_best_prefers_quality_on_24gb_gpu(self) -> None:
        """Simulate a 24GB GPU with both models fitting — quality wins."""
        gpu = GPUInfo(name="RTX 4090", total_vram_mb=24576, free_vram_mb=22100)
        models = [
            ModelInfo(name="gemma4:e4b", size_bytes=9_500_000_000),
            ModelInfo(name="qwen3:14b", size_bytes=9_200_000_000),
        ]
        recs = recommend_models(models, gpu)
        best = pick_best(recs)
        assert best is not None
        assert best.model.name == "qwen3:14b"

    def test_pick_best_falls_back_to_tight(self) -> None:
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="qwen3:30b", size_bytes=19_200_000_000),
                fit_status="tight",
            ),
            ModelRecommendation(
                model=ModelInfo(name="gemma4:56b", size_bytes=35_000_000_000),
                fit_status="oom",
            ),
        ]
        best = pick_best(recs)
        assert best is not None
        assert best.model.name == "qwen3:30b"

    def test_pick_best_tight_prefers_quality(self) -> None:
        """Among tight-fit models, quality scoring should still apply."""
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="gemma4:e4b", size_bytes=9_500_000_000),
                fit_status="tight",
            ),
            ModelRecommendation(
                model=ModelInfo(name="qwen3:14b", size_bytes=9_200_000_000),
                fit_status="tight",
            ),
        ]
        best = pick_best(recs)
        assert best is not None
        assert best.model.name == "qwen3:14b"

    def test_pick_best_returns_none_if_all_oom(self) -> None:
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="huge", size_bytes=50_000_000_000),
                fit_status="oom",
            ),
        ]
        best = pick_best(recs)
        assert best is None


class TestQualityScoring:
    def test_estimate_param_billions_standard(self) -> None:
        assert _estimate_param_billions("qwen3:14b") == 14.0
        assert _estimate_param_billions("qwen3:30b") == 30.0
        assert _estimate_param_billions("llama3:8b") == 8.0
        assert _estimate_param_billions("deepseek-r1:1.5b") == 1.5

    def test_estimate_param_billions_efficiency_models(self) -> None:
        assert _estimate_param_billions("gemma4:e4b") == 4.0
        assert _estimate_param_billions("gemma4:e2b") == 2.0

    def test_estimate_param_billions_mistral_default(self) -> None:
        assert _estimate_param_billions("mistral:instruct") == 7.0

    def test_estimate_param_billions_unknown(self) -> None:
        assert _estimate_param_billions("unknown-model") == 0.0

    def test_quality_score_known_models(self) -> None:
        assert _quality_score(ModelInfo(name="qwen3:14b")) == 100
        assert _quality_score(ModelInfo(name="qwen3:30b")) == 110
        assert _quality_score(ModelInfo(name="qwen3.5:9b")) == 70

    def test_quality_score_by_param_count(self) -> None:
        score_14b = _quality_score(ModelInfo(name="some-model:14b"))
        score_8b = _quality_score(ModelInfo(name="some-model:8b"))
        score_4b = _quality_score(ModelInfo(name="gemma4:e4b"))
        assert score_14b > score_8b > score_4b

    def test_quality_score_14b_beats_e4b(self) -> None:
        """Core regression: 14B model must score higher than 4B efficiency model."""
        score_qwen14 = _quality_score(ModelInfo(name="qwen3:14b"))
        score_gemma4 = _quality_score(ModelInfo(name="gemma4:e4b"))
        assert score_qwen14 > score_gemma4


class TestFormatTable:
    def test_formats_correctly(self) -> None:
        gpu = GPUInfo(name="RTX 4090", total_vram_mb=24576, free_vram_mb=22100)
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="qwen3:14b", size_bytes=9_800_000_000),
                fit_status="ok",
                note="fits well",
            ),
        ]
        table = format_recommendation_table(gpu, 65536, recs)
        assert "RTX 4090" in table
        assert "qwen3:14b" in table
        assert "64 GB" in table


class TestNumCtxPassthrough:
    def test_generate_includes_num_ctx(self) -> None:
        client = OllamaClient(
            endpoint="http://localhost:11434",
            model="test",
            timeout=30,
            num_ctx=8192,
        )
        with patch("callmem.core.ollama.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"response": "ok"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            client._generate("test prompt")

            call_args = mock_post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body is not None
            assert body["options"]["num_ctx"] == 8192

    def test_generate_omits_num_ctx_when_none(self) -> None:
        client = OllamaClient(
            endpoint="http://localhost:11434",
            model="test",
            timeout=30,
            num_ctx=None,
        )
        with patch("callmem.core.ollama.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"response": "ok"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            client._generate("test prompt")

            call_args = mock_post.call_args
            body = call_args.kwargs.get("json") or call_args[1].get("json")
            assert body is not None
            assert "options" not in body


class TestConfigRoundTrip:
    def test_num_ctx_in_config(self) -> None:
        from callmem.models.config import Config

        config = Config(ollama={"num_ctx": 8192})
        assert config.ollama.num_ctx == 8192

    def test_num_ctx_default_none(self) -> None:
        from callmem.models.config import Config

        config = Config()
        assert config.ollama.num_ctx is None

    def test_engine_creates_client_with_num_ctx(self) -> None:

        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        db = Database(":memory:")
        db.initialize()
        config = Config(ollama={"num_ctx": 4096})
        engine = MemoryEngine(db, config)

        assert engine.llm_client is not None
        assert engine.llm_client.num_ctx == 4096
