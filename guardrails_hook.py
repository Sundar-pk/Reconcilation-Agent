"""Loads your organisation's guardrails (monkey patches) BEFORE LangChain / OpenAI are imported.

Monkey patches only take effect if applied before the patched classes are imported and used.
main.py calls apply() first, and every langchain / openai import in this project is deferred
(done inside functions) until after it.

.env:
    GUARDRAILS_MODULE=my_org_guardrails            # imported; its import-time side effects patch things
    GUARDRAILS_ENTRYPOINT=my_org_guardrails:apply  # optional, called after the import
"""
import importlib
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def apply() -> None:
    module_name = os.getenv("GUARDRAILS_MODULE", "").strip()
    entrypoint = os.getenv("GUARDRAILS_ENTRYPOINT", "").strip()

    if module_name:
        importlib.import_module(module_name)
        print(f"[guardrails] loaded module: {module_name}")

    if entrypoint:
        mod_name, _, func_name = entrypoint.partition(":")
        getattr(importlib.import_module(mod_name), func_name)()
        print(f"[guardrails] ran entrypoint: {entrypoint}")

    if not module_name and not entrypoint:
        print("[guardrails] none configured (set GUARDRAILS_MODULE in .env to enable)")
