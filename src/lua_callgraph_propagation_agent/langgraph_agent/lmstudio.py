"""LM Studio OpenAI-compatible JSON model adapter."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_json_slice(text: str) -> str:
    text = _strip_markdown_fences(text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def _escape_invalid_json_backslashes(text: str) -> str:
    valid_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_string = not in_string
            i += 1
            continue
        if ch == "\\" and in_string:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt and nxt not in valid_escapes:
                out.append("\\")
                out.append("\\")
                i += 1
                continue
            out.append(ch)
            escape = True
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = _extract_json_slice(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if "Invalid \\escape" not in str(exc):
            raise
        repaired = _escape_invalid_json_backslashes(text)
        data = json.loads(repaired)
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
        print(
            "[lmstudio] verify request "
            f"model={self.model} prompt_chars={len(prompt)} schema_chars={len(json.dumps(schema, ensure_ascii=False))}"
        )
        body = self._invoke_payload_variants(payload_variants)
        content = self._extract_response_text(body)
        if not content:
            raise ValueError(f"LM Studio returned empty content: {body}")
        try:
            return _extract_json_object(content)
        except Exception as exc:
            print(f"[lmstudio] primary JSON parse failed: {exc}", flush=True)
            return self._retry_reformat_json(content, schema)

    def _extract_response_text(self, body: dict[str, Any]) -> str:
        message = (((body.get("choices") or [{}])[0].get("message") or {}))
        return (
            message.get("content")
            or message.get("reasoning_content")
            or ""
        )

    def _invoke_payload_variants(self, payload_variants: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
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
        return body

    def _retry_reformat_json(self, invalid_content: str, schema: dict[str, Any]) -> dict[str, Any]:
        schema_text = json.dumps(schema, ensure_ascii=False)
        system_prompt = (
            "You are a strict JSON repair assistant. "
            "Return exactly one valid JSON object matching the provided schema. "
            "Do not include markdown, prose, or reasoning."
        )
        user_prompt = (
            "Your previous answer was not valid JSON.\n"
            "Reformat it into exactly one valid JSON object matching this schema.\n"
            "Do not change the decision unless a field is truly missing.\n\n"
            f"Schema:\n{schema_text}\n\n"
            "Previous answer:\n"
            f"{invalid_content}"
        )
        payload_variants = [
            {
                "model": self.model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            {
                "model": self.model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        ]
        print("[lmstudio] retrying with JSON reformat request", flush=True)
        body = self._invoke_payload_variants(payload_variants)
        content = self._extract_response_text(body)
        if not content:
            raise ValueError(f"LM Studio returned empty content on JSON reformat retry: {body}")
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
