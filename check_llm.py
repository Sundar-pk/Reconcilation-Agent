"""Smoke-test your LLM settings before using the agent:   python check_llm.py

Applies your guardrails first, then sends a tiny request with your Bearer key (honouring
INTERNAL_LLM_VERIFY_SSL), and checks the model can return JSON, which the planner needs.
"""
import guardrails_hook

guardrails_hook.apply()

import config  # noqa: E402
from llm import get_llm, llm_json, llm_text  # noqa: E402


def main():
    print(f"URL   : {config.LLM_URL}\nModel : {config.LLM_MODEL}\nMode  : {config.LLM_MODE}   verify_ssl={config.LLM_VERIFY_SSL}")
    llm = get_llm()
    print("\n1) plain reply ...")
    print("   ->", llm_text(llm, "You are terse.", "Reply with the single word: OK"))
    print("2) JSON reply ...")
    out = llm_json(llm, "Reply with ONE JSON object and nothing else.", 'Return {"status": "ready", "numbers": [1, 2, 3]}')
    print("   ->", out)
    print("\nOK: the planner and explainer will work with this model.")
    if config.LLM_MODE == "openai":
        print("Tip: 'python main.py chat' additionally needs tool calling; if it fails, use 'reconcile' (no tool calling needed).")


if __name__ == "__main__":
    main()
