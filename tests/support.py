"""Shared fakes for the unattended-pipeline tests (no network, no real SMTP, no real clock)."""
import json
import os
import urllib.error
from io import BytesIO

from pipeline import store
from pipeline.common import parse_dt, week_key, write_json_atomic
from pipeline.llm import LLMError, LLMFatal


class FakeResp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body="{}"):
    return urllib.error.HTTPError("http://x", code, "err", {}, BytesIO(body.encode()))


class ScriptedUrlopen:
    """Returns/raises the scripted outcomes in order; records every request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return FakeResp(json.dumps(out).encode("utf-8"))


def completion(content, cost=0.01, finish="stop", model="m"):
    return {"model": model, "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": cost}}


def mkpost(pid, text, created="2026-11-09T01:00:00Z", en=None, sent=None, relevant=True, handle="user1"):
    p = {"id": pid, "created_at": created, "account_handle": handle,
         "content_original": text if relevant else "", "content_en": en, "sentiment": sent,
         "topics": ["macro"], "relevant": relevant}
    if not relevant:
        p["exclude_reason"] = "test"
    return p


def write_posts(weeks_dir, posts):
    os.makedirs(weeks_dir, exist_ok=True)
    by_week = {}
    for p in posts:
        by_week.setdefault(week_key(parse_dt(p["created_at"])), []).append(p)
    for week, plist in by_week.items():
        data = store.load_week(week, weeks_dir)
        for p in plist:
            data["posts"][p["id"]] = p
        data["posts"] = dict(sorted(data["posts"].items()))
        write_json_atomic(os.path.join(weeks_dir, f"{week}.json"), data)


def read_posts(weeks_dir):
    return {p["id"]: p for p in store.iter_posts(weeks_dir)}


class FakeLLM:
    """Stands in for llm.OpenRouter in translate/auto tests. `answer(post)` -> content_en."""

    def __init__(self, answer=None, fatal=None, transient=None, not_about=(), sentiment="Neutral", model="fake-model"):
        self.answer = answer or (lambda post: "Withdrawals are fast.")
        self.fatal, self.transient = fatal, transient
        self.not_about, self.sentiment, self.model = set(not_about), sentiment, model
        self.calls = []

    def resolve_model(self):
        return self.model, ""

    def spent_month(self):
        return 0.5

    def chat(self, messages, model, purpose="translate", json_mode=True):
        self.calls.append(messages)
        if self.fatal:
            raise LLMFatal(self.fatal)
        if self.transient:
            raise LLMError(self.transient)
        posts = json.loads(messages[1]["content"])["posts"]
        results = []
        for p in posts:
            results.append({"id": p["id"], "content_en": self.answer(p),
                            "sentiments": [{"brand": b, "sentiment": self.sentiment,
                                            "not_about_brand": p["id"] in self.not_about,
                                            "reason": "different meaning" if p["id"] in self.not_about else ""}
                                           for b in p["brands"]]})
        return {"text": json.dumps({"results": results}), "model": model, "cost_usd": 0.01 * len(posts),
                "finish_reason": "stop"}
