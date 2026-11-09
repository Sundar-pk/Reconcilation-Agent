"""Chat model for your internally hosted LLM (Qwen, gpt-oss-120b, ...) over HTTP with a Bearer key.

All langchain / openai imports happen inside functions, so guardrails_hook.apply() has already
run (monkey patches in place) before those libraries are imported.

TLS: INTERNAL_LLM_VERIFY_SSL=false disables certificate verification (as requested). That also
disables protection for your Bearer key against interception, so prefer INTERNAL_LLM_CA_BUNDLE
(path to your org's CA file) as soon as you can get it.
"""
from __future__ import annotations

import json
import re

import config

_warned = False


def _verify_setting():
    global _warned
    if not config.LLM_VERIFY_SSL:
        if not _warned:
            print("[llm] WARNING: TLS certificate verification is DISABLED (INTERNAL_LLM_VERIFY_SSL=false). "
                  "Your Bearer key is exposed to interception on untrusted networks.")
            _warned = True
        return False
    return config.LLM_CA_BUNDLE or True


def configured() -> bool:
    return bool(config.LLM_URL and config.LLM_KEY and config.LLM_MODEL)


def get_llm(temperature: float = 0.0):
    missing = [n for n, v in (("INTERNAL_LLM_URL", config.LLM_URL), ("INTERNAL_LLM_KEY", config.LLM_KEY),
                              ("INTERNAL_LLM_MODEL", config.LLM_MODEL)) if not v]
    if missing:
        raise SystemExit(f"Missing settings in .env: {', '.join(missing)}")
    if config.LLM_MODE == "custom":
        return _build_custom(temperature)

    import httpx
    from langchain_openai import ChatOpenAI

    verify = _verify_setting()
    kwargs = {}
    if verify is not True:  # only inject our own client when needed, so the default path stays untouched
        kwargs["http_client"] = httpx.Client(verify=verify, timeout=config.LLM_TIMEOUT)
    return ChatOpenAI(
        base_url=config.LLM_URL,
        api_key=config.LLM_KEY,  # sent as "Authorization: Bearer <key>"
        model=config.LLM_MODEL,
        temperature=temperature,
        timeout=config.LLM_TIMEOUT,
        **kwargs,
    )


def _build_custom(temperature: float):
    """For endpoints that are NOT OpenAI-compatible. Adapt the two marked methods.
    Supports planning / explaining, but not the tool-calling chat agent."""
    import httpx
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    verify = _verify_setting()

    class InternalHTTPChatModel(BaseChatModel):
        url: str
        api_key: str
        model: str
        temperature: float = 0.0

        @property
        def _llm_type(self) -> str:
            return "internal-http"

        def _build_payload(self, messages) -> dict:  # <-- ADAPT to your API
            roles = {"human": "user", "ai": "assistant", "system": "system"}
            return {"model": self.model, "temperature": self.temperature,
                    "messages": [{"role": roles.get(m.type, "user"), "content": m.content} for m in messages]}

        def _parse_response(self, data: dict) -> str:  # <-- ADAPT to your API
            return data["choices"][0]["message"]["content"]

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            resp = httpx.post(self.url, headers={"Authorization": f"Bearer {self.api_key}",
                                                 "Content-Type": "application/json"},
                              json=self._build_payload(messages), timeout=config.LLM_TIMEOUT, verify=verify)
            resp.raise_for_status()
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._parse_response(resp.json())))])

    return InternalHTTPChatModel(url=config.LLM_URL, api_key=config.LLM_KEY, model=config.LLM_MODEL,
                                 temperature=temperature)


# ----------------------------------------------------------------------------- helpers
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates <think> blocks and code fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```(?:json)?", "", text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    raise ValueError("no JSON object found in the reply")


def llm_json(llm, system: str, user: str, retries: int = 2) -> dict:
    """Ask for a JSON object; on malformed output, tell the model and retry."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    msgs = [SystemMessage(content=system), HumanMessage(content=user)]
    last = None
    for _ in range(retries + 1):
        reply = _text(llm.invoke(msgs).content)
        try:
            return extract_json(reply)
        except ValueError as exc:
            last = exc
            msgs += [AIMessage(content=reply),
                     HumanMessage(content=f"That was not valid JSON ({exc}). Reply again with ONLY the JSON object.")]
    raise ValueError(f"LLM did not return valid JSON after {retries + 1} attempts: {last}")


def llm_text(llm, system: str, user: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    reply = _text(llm.invoke([SystemMessage(content=system), HumanMessage(content=user)]).content)
    return re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL | re.IGNORECASE).strip()
