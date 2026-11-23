"""OpenAI-compatible API client for memory-maintenance LLM operations.

Works with any provider that implements the /v1/chat/completions endpoint:
Z.ai/GLM, OpenAI, Groq, Together, Mistral, local vLLM, etc.

Provides the same interface as OllamaClient so the engine can swap
backends without changing any calling code.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from ulid import ULID

from callmem.core.json_utils import parse_json
from callmem.core.prompts import SCAN_MAX_TOKENS, SENSITIVE_SCAN_PROMPT
from callmem.core.redaction import Detection, IncompleteScanError

logger = logging.getLogger(__name__)


class OpenAICompatClient:
    """Client for any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(
        self,
        endpoint: str = "https://openrouter.ai/api/v1",
        model: str = "z-ai/glm-4-flash",
        api_key: str | None = None,
        timeout: int = 120,
        env_key_fallback: bool = True,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.env_key_fallback = env_key_fallback
        if not env_key_fallback:
            self.api_key = api_key or ""
        else:
            self.api_key = (
                api_key
                or os.environ.get("OPENROUTER_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("CALLMEM_API_KEY")
                or os.environ.get("LLM_MEM_API_KEY", "")
            )
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if the API endpoint is reachable and the key works."""
        if not self.api_key and self.env_key_fallback:
            return False
        try:
            resp = httpx.post(
                f"{self.endpoint}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                },
                headers=self._headers(),
                timeout=10.0,
            )
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _generate(
        self, prompt: str, system: str | None = None, max_tokens: int | None = None,
    ) -> str | None:
        """Send a chat completion request and return the response text."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = httpx.post(
                f"{self.endpoint}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    **({"max_tokens": max_tokens} if max_tokens else {}),
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            return choices[0].get("message", {}).get("content", "").strip()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.warning("OpenAI-compat API call failed: %s", exc)
            return None

    def extract(self, prompt: str) -> str | None:
        """Send an extraction prompt and return the raw response."""
        return self._generate(prompt)

    def scan_sensitive(self, content: str) -> list[Detection]:
        """Use the API model to detect sensitive data.

        Returns a list of Detection objects with detector="llm".
        """
        prompt = SENSITIVE_SCAN_PROMPT.format(text=content)
        response = self._generate(prompt)
        if response is None:
            return []

        return self._parse_findings(content, response)

    def scan_sensitive_strict(self, content: str) -> list[Detection] | None:
        """Like ``scan_sensitive`` but returns None when the scan didn't happen.

        No response means the text was not checked; callers that must not
        pass unchecked text on need to tell that apart from "checked, nothing
        found". A reply that isn't a whole JSON array (usually cut off at the
        token cap) raises IncompleteScanError; see ``scan_in_pieces``.
        """
        response = self._generate(
            SENSITIVE_SCAN_PROMPT.format(text=content), max_tokens=SCAN_MAX_TOKENS,
        )
        if response is None:
            return None
        try:
            raw = parse_json(response)
        except json.JSONDecodeError:
            raw = None
        if not isinstance(raw, list):
            logger.warning("Sensitive scan reply incomplete (%d chars)", len(response))
            raise IncompleteScanError(f"scan reply of {len(response)} chars")
        return self._parse_findings(content, response)

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
            logger.warning("API scan returned invalid JSON: %s", response[:200])
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
