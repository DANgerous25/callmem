"""Ollama HTTP client for memory-maintenance LLM operations.

Handles sensitive data scanning, summarization, entity extraction, etc.
Uses httpx for HTTP communication with the local Ollama instance.
"""

from __future__ import annotations

import json
import logging

import httpx
from ulid import ULID

from llm_mem.core.json_utils import parse_json
from llm_mem.core.prompts import SENSITIVE_SCAN_PROMPT
from llm_mem.core.redaction import Detection

logger = logging.getLogger(__name__)


class OllamaClient:
    """HTTP client for a local Ollama instance."""

    def __init__(
        self,
        endpoint: str = "http://localhost:11434",
        model: str = "qwen3:8b",
        timeout: int = 120,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if the Ollama instance is reachable."""
        try:
            resp = httpx.get(f"{self.endpoint}/api/tags", timeout=5.0)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    def _generate(self, prompt: str) -> str | None:
        """Send a generate request to Ollama and return the response text."""
        try:
            resp = httpx.post(
                f"{self.endpoint}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "").strip()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.warning("Ollama generate failed: %s", exc)
            return None

    def extract(self, prompt: str) -> str | None:
        """Send an extraction prompt and return the raw response.

        Public wrapper around _generate for use by extraction and other workers.
        """
        return self._generate(prompt)

    def scan_sensitive(self, content: str) -> list[Detection]:
        """Use the local LLM to detect sensitive data that patterns might miss.

        Returns a list of Detection objects with detector="llm".
        """
        prompt = SENSITIVE_SCAN_PROMPT.format(text=content)
        response = self._generate(prompt)
        if response is None:
            return []

        findings = self._parse_findings(content, response)
        return findings

    def _parse_findings(
        self, content: str, response: str
    ) -> list[Detection]:
        """Parse the LLM response into Detection objects.

        The LLM should return a JSON array of objects with keys:
        value, category, confidence.
        """
        try:
            raw = parse_json(response)
        except json.JSONDecodeError:
            logger.warning("LLM scan returned invalid JSON: %s", response[:200])
            return []

        if not isinstance(raw, list):
            return []

        detections: list[Detection] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = item.get("value", "")
            category = item.get("category", "secret")
            confidence = float(item.get("confidence", 0.0))

            if not value or confidence <= 0:
                continue

            start = content.find(value)
            if start == -1:
                continue

            detections.append(Detection(
                vault_id=str(ULID()),
                category=category,
                detector="llm",
                pattern_name=None,
                original_value=value,
                start=start,
                end=start + len(value),
                confidence=confidence,
            ))

        return detections
