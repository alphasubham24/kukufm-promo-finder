"""Model-agnostic LLM client for any OpenAI-compatible Chat Completions API.

Configure with env vars (put them in .env, which is loaded automatically):

    LLM_BASE_URL    e.g. https://api.groq.com/openai/v1
                         https://bedrock-runtime.<region>.amazonaws.com/openai/v1
                         https://generativelanguage.googleapis.com/v1beta/openai
    LLM_API_KEY     the provider's key (sent as a Bearer token)
    LLM_MODEL       the provider's model id
    LLM_TPM         optional client-side tokens/minute throttle (0 = off), e.g. 8000 for Groq free tier
    LLM_CONCURRENCY optional parallel calls (default 4; use 1 on tight free tiers)
    LLM_EXTRA_JSON  optional extra request fields, e.g. {"reasoning_effort":"low"}

Every response is cached on disk keyed by (base_url, model, prompt), so
re-running a show is free and an interrupted run resumes where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from . import ROOT


class LLMError(RuntimeError):
    """Non-retryable failure (bad key, unknown model, daily quota)."""


class LLM:
    def __init__(self, cache_dir: str | Path | None = None):
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "")
        extra = os.getenv("LLM_EXTRA_JSON", "").strip()
        self.extra = json.loads(extra) if extra else {}
        self.tpm = int(os.getenv("LLM_TPM", "0") or 0)
        self.concurrency = max(1, int(os.getenv("LLM_CONCURRENCY", "4") or 4))
        self.json_mode = True
        self.cache_dir = Path(cache_dir or os.getenv("LLM_CACHE_DIR", ROOT / ".cache" / "llm"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self.usage = {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def describe(self) -> dict:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0] or "not set"
        return {"provider": host, "model": self.model or "not set", "key_configured": self.available,
                "tpm_budget": self.tpm, "concurrency": self.concurrency}

    # optional rolling 60s token window, shared by all worker threads
    def _throttle(self, est_tokens: int, log=None):
        if not self.tpm:
            return
        while True:
            with self._lock:
                now = time.time()
                while self._window and now - self._window[0][0] > 60:
                    self._window.popleft()
                used = sum(t for _, t in self._window)
                if used + est_tokens <= self.tpm or not self._window:
                    self._window.append((now, est_tokens))
                    return
                wait = 60 - (now - self._window[0][0]) + 0.5
            if log:
                log(f"rate budget: waiting {wait:.0f}s ({used} tokens used in the last minute)")
            time.sleep(max(1.0, wait))

    def _post(self, system: str, user: str, max_tokens: int) -> httpx.Response:
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": 0.3,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                **self.extra}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=300,
                          headers={"Authorization": f"Bearer {self.api_key}"})

    def chat_json(self, system: str, user: str, max_tokens: int = 3000, log=None, tag: str = "") -> dict:
        key = hashlib.sha256(json.dumps([self.base_url, self.model, system, user, max_tokens]).encode()).hexdigest()[:24]
        cpath = self.cache_dir / f"{key}.json"
        if cpath.exists():
            with self._lock:
                self.usage["cached"] += 1
            return json.loads(cpath.read_text())["parsed"]
        if not self.available:
            raise LLMError("LLM not configured: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL in .env")

        est = int((len(system) + len(user)) / 3.6) + max_tokens // 2
        last_err = ""
        for attempt in range(6):
            self._throttle(est, log)
            try:
                r = self._post(system, user, max_tokens)
            except httpx.HTTPError as e:
                last_err = str(e)
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 429:
                msg = r.text[:300]
                if "per day" in msg.lower():
                    raise LLMError(f"Daily token limit reached for {self.model}. Cached progress is kept; "
                                   f"re-run later or switch provider. {msg}")
                wait = max(float(r.headers.get("retry-after", "0") or 0), 5 * (attempt + 1))
                if log:
                    log(f"rate limited by provider, retrying in {wait:.0f}s")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403, 404):
                raise LLMError(f"HTTP {r.status_code} from {self.base_url} (check key/model): {r.text[:300]}")
            if r.status_code == 400 and self.json_mode and "response_format" in r.text:
                self.json_mode = False  # model without JSON mode: rely on the prompt instead
                continue
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(min(30, 2 ** attempt))
                continue
            data = r.json()
            content = data["choices"][0]["message"].get("content") or ""
            parsed = parse_json_loose(content)
            if parsed is None:
                last_err = f"unparseable JSON: {content[:200]}"
                continue
            u = data.get("usage", {}) or {}
            with self._lock:
                self.usage["calls"] += 1
                self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                self.usage["completion_tokens"] += u.get("completion_tokens", 0)
            cpath.write_text(json.dumps({"tag": tag, "base_url": self.base_url, "model": self.model,
                                         "system": system, "user": user, "raw": content, "usage": u,
                                         "parsed": parsed}, indent=1))
            return parsed
        raise LLMError(f"LLM call failed after retries: {last_err}")


def parse_json_loose(s: str):
    s = s.strip()
    # Strip markdown code fences
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE)
    # Strip <reasoning>...</reasoning> or <thinking>...</thinking> blocks
    # that some models (e.g. Bedrock o-series) prepend before the JSON
    s = re.sub(r"<(reasoning|thinking|scratchpad)>.*?</\1>", "", s, flags=re.S | re.I)
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Fallback: grab the first complete {...} JSON object in the text
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
