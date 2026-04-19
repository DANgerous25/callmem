"""Tests for the Ollama client — LLM-based sensitive data scanning."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from callmem.core.ollama import OllamaClient
from callmem.core.redaction import Detection

if TYPE_CHECKING:
    from callmem.core.database import Database
    pass


class TestOllamaAvailability:
    def test_available_on_200(self) -> None:
        client = OllamaClient()
        with patch("callmem.core.ollama.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            assert client.is_available() is True

    def test_unavailable_on_connection_error(self) -> None:
        client = OllamaClient()
        with patch("callmem.core.ollama.httpx.get") as mock_get:
            import httpx

            mock_get.side_effect = httpx.ConnectError("refused")
            assert client.is_available() is False

    def test_unavailable_on_timeout(self) -> None:
        client = OllamaClient()
        with patch("callmem.core.ollama.httpx.get") as mock_get:
            import httpx

            mock_get.side_effect = httpx.TimeoutException("timeout")
            assert client.is_available() is False


class TestScanSensitive:
    def test_parses_valid_json_response(self) -> None:
        client = OllamaClient()
        content = "the password is hunter2 for the db"
        response = '[{"value": "hunter2", "category": "secret", "confidence": 0.9}]'

        with patch.object(client, "_generate", return_value=response):
            detections = client.scan_sensitive(content)

        assert len(detections) == 1
        assert detections[0].original_value == "hunter2"
        assert detections[0].detector == "llm"
        assert detections[0].category == "secret"
        assert detections[0].confidence == 0.9

    def test_empty_array_returns_no_detections(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value="[]"):
            detections = client.scan_sensitive("nothing here")
        assert detections == []

    def test_invalid_json_returns_empty(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value="not json at all"):
            detections = client.scan_sensitive("some text")
        assert detections == []

    def test_generate_failure_returns_empty(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value=None):
            detections = client.scan_sensitive("the password is secret123")
        assert detections == []

    def test_value_not_found_in_content_skipped(self) -> None:
        client = OllamaClient()
        content = "no secrets here"
        response = '[{"value": "nonexistent_value", "category": "secret", "confidence": 0.9}]'

        with patch.object(client, "_generate", return_value=response):
            detections = client.scan_sensitive(content)
        assert detections == []

    def test_multiple_findings(self) -> None:
        client = OllamaClient()
        content = "password is abc123 and the secret key is xyz789"
        response = (
            '[{"value": "abc123", "category": "secret", "confidence": 0.8},'
            '{"value": "xyz789", "category": "secret", "confidence": 0.7}]'
        )

        with patch.object(client, "_generate", return_value=response):
            detections = client.scan_sensitive(content)

        assert len(detections) == 2
        assert all(d.detector == "llm" for d in detections)

    def test_low_confidence_included_in_raw_detections(self) -> None:
        client = OllamaClient()
        content = "the password is hunter2"
        response = '[{"value": "hunter2", "category": "secret", "confidence": 0.3}]'

        with patch.object(client, "_generate", return_value=response):
            detections = client.scan_sensitive(content)

        assert len(detections) == 1
        assert detections[0].confidence == 0.3


class TestConfidenceThreshold:
    def test_engine_filters_low_confidence_llm_detections(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        config = Config()
        config.sensitive_data.llm_scan_confidence = 0.8
        engine = MemoryEngine(memory_db, config)

        low_conf_detection = Detection(
            vault_id="low1",
            category="secret",
            detector="llm",
            pattern_name=None,
            original_value="hunter2",
            start=4,
            end=11,
            confidence=0.3,
        )
        high_conf_detection = Detection(
            vault_id="high1",
            category="secret",
            detector="llm",
            pattern_name=None,
            original_value="secret123",
            start=0,
            end=9,
            confidence=0.95,
        )

        with patch.object(
            engine.ollama, "scan_sensitive", return_value=[low_conf_detection, high_conf_detection]
        ), patch.object(engine.ollama, "is_available", return_value=True):
            engine.start_session()
            event = engine.ingest_one("note", "secret123 is the password hunter2")

        assert event is not None
        assert "[REDACTED:secret:high1]" in event.content
        assert "[REDACTED:secret:low1]" not in event.content

    def test_engine_scan_status_full_when_ollama_available(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        engine = MemoryEngine(memory_db, Config())

        with patch.object(
            engine.ollama, "scan_sensitive", return_value=[]
        ), patch.object(engine.ollama, "is_available", return_value=True):
            engine.start_session()
            event = engine.ingest_one("note", "normal content here")

        assert event is not None
        assert event.metadata["scan_status"] == "full"

    def test_engine_scan_status_pattern_only_when_ollama_down(
        self, memory_db: Database
    ) -> None:
        from callmem.core.engine import MemoryEngine
        from callmem.models.config import Config

        engine = MemoryEngine(memory_db, Config())

        with patch.object(
            engine.ollama, "is_available", return_value=False
        ):
            engine.start_session()
            event = engine.ingest_one("note", "key = AKIAIOSFODNN7EXAMPLE")

        assert event is not None
        assert event.metadata["scan_status"] == "pattern_only"


class TestExtractMethod:
    def test_extract_returns_response(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value="extraction result"):
            result = client.extract("test prompt")
        assert result == "extraction result"

    def test_extract_returns_none_on_failure(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value=None):
            result = client.extract("test prompt")
        assert result is None

    def test_extract_passes_prompt_through(self) -> None:
        client = OllamaClient()
        with patch.object(client, "_generate", return_value="ok") as mock:
            client.extract("my special prompt")
        mock.assert_called_once_with("my special prompt")


class TestGenerateHttpErrors:
    def test_generate_handles_http_500(self) -> None:
        client = OllamaClient()
        with patch("callmem.core.ollama.httpx.post") as mock_post:
            import httpx
            mock_post.return_value.raise_for_status.side_effect = (
                httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500))
            )
            result = client._generate("test")
        assert result is None


class TestParseFindingsEdgeCases:
    def test_non_list_json_returns_empty(self) -> None:
        client = OllamaClient()
        result = client._parse_findings("content", '{"key": "value"}')
        assert result == []

    def test_item_missing_value_skipped(self) -> None:
        client = OllamaClient()
        result = client._parse_findings(
            "some content",
            '[{"category": "secret", "confidence": 0.9}]',
        )
        assert result == []

    def test_item_zero_confidence_skipped(self) -> None:
        client = OllamaClient()
        result = client._parse_findings(
            "password hunter2",
            '[{"value": "hunter2", "category": "secret", "confidence": 0}]',
        )
        assert result == []

    def test_non_dict_items_skipped(self) -> None:
        client = OllamaClient()
        result = client._parse_findings(
            "password hunter2",
            '["string_item", 42, true]',
        )
        assert result == []


# Import for type hint in test methods
