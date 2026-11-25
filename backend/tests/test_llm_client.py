"""Offline tests for the OpenAI-compatible client (no network: httpx.MockTransport)."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import client as client_mod
from app.llm.client import LLMClient, LLMError, strict_json_schema


class Row(BaseModel):
    date: str
    debit: float | None
    balance: float


class Rows(BaseModel):
    rows: list[Row]


def make_client(handler, **overrides) -> LLMClient:
    s = Settings(_env_file=None, llm_api_key="test-key", **overrides)
    c = LLMClient(s)
    c._http = httpx.Client(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler),
                           headers=c._http.headers)
    return c


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


ROWS_JSON = json.dumps({"rows": [{"date": "2025-01-01", "debit": None, "balance": 10.5}]})


def test_strict_schema_closes_all_objects():
    sch = strict_json_schema(Rows)
    assert sch["additionalProperties"] is False and sch["required"] == ["rows"]
    row = sch["$defs"]["Row"]
    assert row["additionalProperties"] is False
    assert row["required"] == ["date", "debit", "balance"]


def test_body_merges_extra_and_auth():
    seen = {}

    def h(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return completion("hi")

    c = make_client(h, llm_extra_body={"reasoning_effort": "low"}, chat_model="m1")
    assert c.chat([{"role": "user", "content": "x"}], max_tokens=5) == "hi"
    assert seen["auth"] == "Bearer test-key"
    b = seen["body"]
    assert b["model"] == "m1" and b["reasoning_effort"] == "low" and b["max_tokens"] == 5
    assert b["temperature"] == 0


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"},
                                  json={"error": {"message": "rate limited"}})
        return completion("ok")

    assert make_client(h).chat([{"role": "user", "content": "x"}]) == "ok"
    assert calls["n"] == 3


def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    h = lambda req: httpx.Response(429, json={"error": {"message": "rate limited"}})
    with pytest.raises(LLMError) as ei:
        make_client(h, llm_max_retries=2).chat([{"role": "user", "content": "x"}])
    assert ei.value.status == 429


def test_chat_json_strict_schema():
    def h(req):
        rf = json.loads(req.content)["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
        return completion(ROWS_JSON)

    out = make_client(h).chat_json([{"role": "user", "content": "x"}], Rows)
    assert out.rows[0].balance == 10.5


def test_chat_json_falls_back_to_json_object():
    def h(req):
        body = json.loads(req.content)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {
                "message": "response_format json_schema is not supported for this model"}})
        assert body["messages"][0]["role"] == "system"
        return completion("```json\n" + ROWS_JSON + "\n```")

    c = make_client(h)
    assert c.chat_json([{"role": "user", "content": "x"}], Rows).rows[0].date == "2025-01-01"
    assert c.json_schema_supported is False
