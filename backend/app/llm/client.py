"""Thin client for any OpenAI-compatible chat endpoint (GroqCloud by default).

Kept dependency-light (httpx only). Later phases may wrap this in a LangChain
ChatOpenAI pointed at the same base URL; the config stays the same.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


@dataclass
class StreamStats:
    text: str
    ttft_s: float
    total_s: float
    completion_tokens: int
    prompt_tokens: int

    @property
    def decode_tok_per_s(self) -> float:
        decode_time = self.total_s - self.ttft_s
        return self.completion_tokens / decode_time if decode_time > 0 else 0.0


def strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> OpenAI/Groq strict form: every object closed, every property required."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        headers = {"Content-Type": "application/json"}
        if self.s.llm_api_key:
            headers["Authorization"] = f"Bearer {self.s.llm_api_key}"
        self._http = httpx.Client(
            base_url=self.s.llm_base_url.rstrip("/"), headers=headers, timeout=self.s.llm_timeout_s
        )
        self.json_schema_supported = True  # flipped if the model rejects json_schema
        self.usage = {"calls": 0, "cached_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    # ---------- transport ----------
    def _request(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        for attempt in range(self.s.llm_max_retries + 1):
            r = self._http.request(method, path, json=body)
            if r.status_code not in RETRY_STATUS or attempt == self.s.llm_max_retries:
                break
            if r.status_code == 429 and "Request too large" in _error_message(r):
                break  # max_tokens / prompt exceed the per-minute cap: retrying can't help
            time.sleep(_retry_delay(r, attempt))
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code} from {path}: {_error_message(r)}", r.status_code)
        return r

    def _chat_body(self, messages: list[dict], **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.s.chat_model,
            "messages": messages,
            "temperature": self.s.llm_temperature,
            "max_tokens": self.s.llm_max_tokens,
        }
        body.update(self.s.llm_extra_body)
        body.update(extra)
        return body

    # ---------- API ----------
    def list_models(self) -> list[dict]:
        return self._request("GET", "/models").json().get("data", [])

    def chat(self, messages: list[dict], **extra: Any) -> str:
        if self.s.llm_disabled:
            raise LLMError("LLM disabled (llm_disabled=true)")
        body = self._chat_body(messages, **extra)
        cache = self._cache_path(body)
        if cache and cache.exists():
            self.usage["cached_calls"] += 1
            return json.loads(cache.read_text())["content"]
        if self.s.llm_cache_only:
            raise LLMError("cache miss (cache-only mode)")
        data = self._request("POST", "/chat/completions", body).json()
        content = data["choices"][0]["message"].get("content") or ""
        u = data.get("usage") or {}
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += u.get("completion_tokens", 0)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"content": content, "usage": u}))
        return content

    def _cache_path(self, body: dict):
        if not self.s.llm_cache_dir:
            return None
        h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return self.s.llm_cache_dir / h[:2] / f"{h}.json"

    def chat_json(self, messages: list[dict], schema: type[T]) -> T:
        """Schema-constrained output. Uses strict json_schema; if the model doesn't support it,
        falls back to json_object mode with the schema in the system prompt."""
        if self.json_schema_supported:
            rf = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": strict_json_schema(schema), "strict": True}}
            try:
                return schema.model_validate_json(_strip_fences(self.chat(messages, response_format=rf)))
            except LLMError as e:
                if e.status != 400 or "response_format" not in str(e) and "json_schema" not in str(e):
                    raise
                self.json_schema_supported = False
        sys_msg = {"role": "system", "content": "Respond with a single JSON object matching this "
                   f"JSON Schema, and nothing else:\n{json.dumps(schema.model_json_schema())}"}
        raw = self.chat([sys_msg, *messages], response_format={"type": "json_object"})
        return schema.model_validate_json(_strip_fences(raw))

    def chat_stream_stats(self, messages: list[dict], max_tokens: int = 256) -> StreamStats:
        """Stream a completion and measure time-to-first-token and decode speed."""
        body = self._chat_body(messages, stream=True, max_tokens=max_tokens,
                               stream_options={"include_usage": True})
        t0 = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        usage: dict = {}
        with self._http.stream("POST", "/chat/completions", json=body) as r:
            if r.status_code >= 400:
                r.read()
                raise LLMError(f"{r.status_code} from /chat/completions: {_error_message(r)}",
                               r.status_code)
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                # Groq puts usage under x_groq.usage in the final chunk.
                usage = chunk.get("usage") or chunk.get("x_groq", {}).get("usage") or usage
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta", {})
                    piece = delta.get("content") or delta.get("reasoning")
                    if piece:
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        if delta.get("content"):
                            parts.append(delta["content"])
        total = time.perf_counter() - t0
        text = "".join(parts)
        return StreamStats(
            text=text,
            ttft_s=ttft if ttft is not None else total,
            total_s=total,
            completion_tokens=usage.get("completion_tokens") or max(1, len(text) // 4),
            prompt_tokens=usage.get("prompt_tokens", 0),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not (self.s.embed_model and self.s.embed_base_url):
            raise LLMError("EMBED_BASE_URL / EMBED_MODEL not configured (Phase 3)")
        headers = {"Authorization": f"Bearer {self.s.embed_api_key}"} if self.s.embed_api_key else {}
        r = httpx.post(f"{self.s.embed_base_url.rstrip('/')}/embeddings", headers=headers,
                       json={"model": self.s.embed_model, "input": texts}, timeout=self.s.llm_timeout_s)
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code} from /embeddings: {_error_message(r)}", r.status_code)
        return [d["embedding"] for d in r.json()["data"]]


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    try:
        return min(float(r.headers.get("retry-after", "")), 30.0)
    except ValueError:
        return min(2.0 * 2**attempt, 30.0)


def _error_message(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return r.text[:300]


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    return s.strip()
