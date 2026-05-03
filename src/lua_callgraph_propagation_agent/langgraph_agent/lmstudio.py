"""LM Studio OpenAI-compatible JSON model adapter."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"model returned non-object JSON: {type(data)!r}")
    return data


class LmStudioJsonModel:
    """Minimal OpenAI-compatible JSON caller for LM Studio.

    The adapter intentionally uses stdlib networking only so it can run without
    adding another inference client dependency. LM Studio typically exposes an
    OpenAI-compatible `/v1/chat/completions` endpoint on localhost.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:1234/v1",
        model: str,
        api_key: str = "lm-studio",
        temperature: float = 0.0,
        timeout_seconds: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds

    def invoke_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        system_prompt = (
            "You are a strict structured-analysis assistant. "
            "Return exactly one JSON object matching the requested schema. "
            "Do not include markdown fences or explanatory prose."
        )
        user_prompt = (
            "Return one JSON object that matches this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n\n"
            "Context:\n"
            f"{prompt}"
        )
        base_payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        payload_variants = [
            {
                **base_payload,
                "response_format": {"type": "json_object"},
            },
            base_payload,
        ]
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
        print(
            "[lmstudio] verify request "
            f"model={self.model} prompt_chars={len(prompt)} schema_chars={len(json.dumps(schema, ensure_ascii=False))}"
        )
        for idx, payload in enumerate(payload_variants):
            try:
                started_at = time.perf_counter()
                print(
                    "[lmstudio] -> chat/completions "
                    f"variant={idx} has_response_format={'response_format' in payload}"
                )
                body = self._post_chat_completion(payload)
                elapsed = time.perf_counter() - started_at
                print(f"[lmstudio] <- chat/completions ok variant={idx} elapsed={elapsed:.2f}s")
                break
            except urllib.error.HTTPError as exc:
                last_error = self._http_error_from_response(exc, payload, idx)
                if exc.code == 400 and idx + 1 < len(payload_variants):
                    print(
                        "[lmstudio] retrying without response_format "
                        f"after HTTP {exc.code} on variant={idx}"
                    )
                    continue
                raise last_error
        if body is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("LM Studio request failed without response body")
        content = (
            (((body.get("choices") or [{}])[0].get("message") or {}).get("content"))
            or ""
        )
        if not content:
            raise ValueError(f"LM Studio returned empty content: {body}")
        return _extract_json_object(content)

    def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _http_error_from_response(
        self,
        exc: urllib.error.HTTPError,
        payload: dict[str, Any],
        variant_index: int,
    ) -> RuntimeError:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unable to read error body>"
        payload_summary = {
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "has_response_format": "response_format" in payload,
            "message_count": len(payload.get("messages") or []),
        }
        return RuntimeError(
            "LM Studio chat/completions request failed "
            f"(variant={variant_index}, status={exc.code} {exc.reason}). "
            f"payload={payload_summary} response={body}"
        )
