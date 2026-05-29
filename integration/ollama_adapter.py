"""
OllamaPacketAdapter — PacketAdapterBase subclass for local Ollama inference.

Execution contract:
  - adapter_target = "ollama/{model_name}"
  - model_version  = model digest from /api/show (captured once at construction)
  - temperature    = 0 (stability mode — not a mathematical determinism guarantee)
  - output_is_deterministic = False
  - stream = False always
  - No retry logic
  - No persistent HTTP session
  - No DB access
  - No candidate parsing (handled upstream in run_packet_task)
  - run() raises on any failure; run_packet_task() catches

request_payload_hash in execution_config:
  sha256 of the request body serialized with sort_keys=True as sent to Ollama.
  The exact bytes sent are sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True)).
  Audit-reconstructible: given adapter params and prompt, the payload is fully deterministic.

Replay doctrine:
  model_version is captured at construction and never refreshed during run().
  If the model digest changes (model replaced), the next adapter construction
  picks up the new digest. Existing stored results carry the digest at time of execution.
"""
import hashlib
import json
from typing import Optional

import requests

from .adapters import PacketAdapterBase


class OllamaPacketAdapter(PacketAdapterBase):
    """
    PacketAdapterBase subclass for local Ollama inference.

    output_is_deterministic = False:
      temperature=0 is stability mode. Ollama does not guarantee bitwise-identical
      output across versions, hardware, or load conditions.
    """
    output_is_deterministic = False

    def __init__(
        self,
        model_name: str,
        base_url: str = "http://localhost:11434",
        temperature: float = 0,
        timeout: int = 120,
    ) -> None:
        """
        Construct and verify the adapter. Fetches model digest from /api/show.

        model_name: Ollama model name (e.g. "mistral", "llama3.2:3b")
        base_url:   Ollama base URL (default: http://localhost:11434)
        temperature: Inference temperature (default: 0, stability mode)
        timeout:    Read timeout in seconds (connection timeout fixed at 5s)
        """
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._timeout = timeout
        self._model_version = self._fetch_model_version()

    def _fetch_model_version(self) -> str:
        """
        Retrieve model digest from Ollama /api/show.

        Called once at construction. Never called during run() or replay.
        On failure, records "unknown:{ExceptionType}" as version string.
        """
        try:
            resp = requests.post(
                f"{self._base_url}/api/show",
                json={"name": self._model_name},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            digest = (
                data.get("digest")
                or data.get("details", {}).get("digest")
                or "unknown"
            )
            return str(digest)
        except Exception as exc:
            return f"unknown:{type(exc).__name__}"

    @property
    def adapter_target(self) -> str:
        return f"ollama/{self._model_name}"

    @property
    def model_version(self) -> str:
        return self._model_version

    def _build_payload(self, rendered_prompt: str) -> dict:
        """
        Single source of truth for the Ollama /api/generate request body.

        Keys are canonical — both run() and build_execution_config() use this
        method to guarantee the payload hash covers exactly what is sent.
        """
        return {
            "model": self._model_name,
            "options": {"seed": None, "temperature": self._temperature},
            "prompt": rendered_prompt,
            "stream": False,
        }

    def _hash_payload(self, payload: dict) -> str:
        """sha256 of the JSON-serialized payload (sort_keys=True, UTF-8)."""
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def run(self, rendered_prompt: str) -> str:
        """
        POST to Ollama /api/generate and return the response text.

        Uses pre-serialized JSON with sorted keys so the bytes sent exactly
        match what request_payload_hash covers.

        Raises on HTTP error, timeout, or malformed response.
        Caller (run_packet_task) catches all exceptions.
        """
        payload = self._build_payload(rendered_prompt)
        body = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        resp = requests.post(
            f"{self._base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=(5, self._timeout),
        )
        resp.raise_for_status()
        data = resp.json()
        if "response" not in data:
            raise ValueError(
                f"Ollama response missing 'response' field: {list(data.keys())}"
            )
        return data["response"]

    def build_execution_config(self, rendered_prompt: str) -> Optional[dict]:
        """
        Build execution_config for ModelTaskResultProvenance.

        Provenance only — never participates in result_id on any path.
        request_payload_hash covers the exact bytes sent to Ollama.
        """
        payload = self._build_payload(rendered_prompt)
        return {
            "temperature": self._temperature,
            "seed": None,
            "timeout_seconds": self._timeout,
            "request_payload_hash": self._hash_payload(payload),
        }
