"""Tests for GPU scan, model recommendation, and num_ctx passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from llm_mem.core.gpu_scan import (
    GPUInfo,
    ModelInfo,
    ModelRecommendation,
    detect_gpu,
    detect_ram,
    fetch_ollama_models,
    format_recommendation_table,
    pick_best,
    recommend_models,
)
from llm_mem.core.ollama import OllamaClient


class TestGPUDetection:
    def test_parses_nvidia_smi_output(self) -> None:
        with patch("llm_mem.core.gpu_scan.subprocess.run") as mock_run:
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
        with patch("llm_mem.core.gpu_scan.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            gpu = detect_gpu()
            assert not gpu.available

    def test_returns_empty_on_bad_output(self) -> None:
        with patch("llm_mem.core.gpu_scan.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            gpu = detect_gpu()
            assert not gpu.available


class TestRAMDetection:
    def test_parses_proc_meminfo(self, tmp_path: str) -> None:
        with patch("llm_mem.core.gpu_scan.Path") as mock_path:
            mock_path.return_value.read_text.return_value = (
                "MemTotal:       65789012 kB\nMemFree:        32100000 kB\n"
            )
            ram = detect_ram()
            assert ram == 64247  # 65789012 / 1024

    def test_returns_zero_on_failure(self) -> None:
        with patch("llm_mem.core.gpu_scan.Path") as mock_path:
            mock_path.return_value.read_text.side_effect = OSError
            ram = detect_ram()
            assert ram == 0


class TestFetchOllamaModels:
    def test_parses_model_list(self) -> None:
        with patch("llm_mem.core.gpu_scan.httpx.get") as mock_get:
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

        with patch("llm_mem.core.gpu_scan.httpx.get") as mock_get:
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

    def test_pick_best_prefers_largest_fitting(self) -> None:
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="gemma4:e4b", size_bytes=5_100_000_000),
                fit_status="easy",
            ),
            ModelRecommendation(
                model=ModelInfo(name="qwen3:14b", size_bytes=9_800_000_000),
                fit_status="ok",
            ),
        ]
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

    def test_pick_best_returns_none_if_all_oom(self) -> None:
        recs = [
            ModelRecommendation(
                model=ModelInfo(name="huge", size_bytes=50_000_000_000),
                fit_status="oom",
            ),
        ]
        best = pick_best(recs)
        assert best is None


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
        with patch("llm_mem.core.ollama.httpx.post") as mock_post:
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
        with patch("llm_mem.core.ollama.httpx.post") as mock_post:
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
        from llm_mem.models.config import Config

        config = Config(ollama={"num_ctx": 8192})
        assert config.ollama.num_ctx == 8192

    def test_num_ctx_default_none(self) -> None:
        from llm_mem.models.config import Config

        config = Config()
        assert config.ollama.num_ctx is None

    def test_engine_creates_client_with_num_ctx(self) -> None:

        from llm_mem.core.database import Database
        from llm_mem.core.engine import MemoryEngine
        from llm_mem.models.config import Config

        db = Database(":memory:")
        db.initialize()
        config = Config(ollama={"num_ctx": 4096})
        engine = MemoryEngine(db, config)

        assert engine.llm_client is not None
        assert engine.llm_client.num_ctx == 4096
