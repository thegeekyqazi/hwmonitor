# llm_diagnosis.py
"""
LLM-powered diagnosis: takes a markdown diagnostic report and asks Claude
or OpenAI to analyze it. Returns a structured response with summary,
likely causes, hardware vs software determination, and recommendations.
"""
import os
import json
import time
from typing import Any, Dict, Optional


SYSTEM_PROMPT = """You are an expert systems diagnostic engineer analyzing a computer health report from ProcessLens, a real-time system monitoring tool.

You will be given a detailed report containing:
- System and hardware inventory (CPU, RAM, GPU, monitors, storage with SMART status, battery health, peripherals, etc.)
- Recent metric statistics (CPU%, RAM%, disk I/O, etc.)
- Detected anomalies with attributed suspect processes
- Pattern insights about repeat offenders and metric distribution

Your job is to provide a clear, actionable diagnosis in JSON format with these exact fields:

{
  "overall_health": "excellent" | "good" | "concerning" | "poor",
  "summary": "2-3 sentence plain-English overview of the system's state",
  "issues_found": [
    {
      "title": "Short descriptive title",
      "severity": "low" | "medium" | "high" | "critical",
      "category": "hardware" | "software" | "configuration" | "user_behavior",
      "description": "What's happening and why it matters",
      "evidence": "Specific data points from the report supporting this finding"
    }
  ],
  "hardware_vs_software": "A 1-2 sentence determination: are issues primarily hardware (failing components, thermal, etc.) or software (rogue processes, leaks, configurations)?",
  "recommendations": [
    {
      "action": "Specific actionable step the user can take",
      "priority": "immediate" | "soon" | "eventually",
      "rationale": "Why this helps"
    }
  ],
  "watch_for": [
    "Patterns or metrics the user should monitor going forward"
  ]
}

Important guidelines:
- Be specific. Cite process names, exact values, percentages from the report.
- Distinguish between "this process is misbehaving" (software) vs "this hardware is failing" (hardware).
- If SMART says a drive is failing, that's CRITICAL. Always.
- Battery wear over 30% is "concerning"; over 50% is "high severity hardware".
- Memory anomalies caused by browser tabs is software, not hardware.
- Sustained high CPU temps would be thermal (hardware), but high CPU usage from a runaway process is software.
- If the report shows mostly normal behavior, say so honestly. Don't invent issues.
- Output ONLY valid JSON. No preamble, no markdown fences, no commentary outside the JSON."""


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def diagnose_with_claude(markdown_report: str, api_key: str, model: str = "claude-sonnet-4-5") -> Dict[str, Any]:
    """Call Anthropic's API. Returns parsed JSON diagnosis."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Here is the diagnostic report:\n\n{markdown_report}\n\nProvide your diagnosis as JSON."
        }]
    )

    text = response.content[0].text
    return _parse_json_response(text, provider="claude")


def diagnose_with_openai(markdown_report: str, api_key: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
    """Call OpenAI's API. Returns parsed JSON diagnosis."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Here is the diagnostic report:\n\n{markdown_report}\n\nProvide your diagnosis as JSON."}
        ],
        response_format={"type": "json_object"},
        max_tokens=4096,
    )

    text = response.choices[0].message.content
    return _parse_json_response(text, provider="openai")

def diagnose_with_gemini(markdown_report: str, api_key: str, model: str = "gemini-2.0-flash") -> Dict[str, Any]:
    """Call Google's Gemini API. Returns parsed JSON diagnosis."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai package not installed. Run: pip install google-generativeai")

    genai.configure(api_key=api_key)

    # Gemini supports structured output via response_mime_type
    gen_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=SYSTEM_PROMPT,
        generation_config={
            "response_mime_type": "application/json",
            "max_output_tokens": 4096,
            "temperature": 0.4,
        }
    )

    response = gen_model.generate_content(
        f"Here is the diagnostic report:\n\n{markdown_report}\n\nProvide your diagnosis as JSON."
    )

    # Gemini returns text on .text attribute
    if not response.candidates or not response.text:
        raise RuntimeError("Gemini returned empty response")

    return _parse_json_response(response.text, provider="gemini")

def diagnose_with_ollama(markdown_report: str, base_url: str = "http://localhost:11434",
                        model: str = "llama3.2") -> Dict[str, Any]:
    """Call a local Ollama server. No API key needed."""
    import requests

    base_url = base_url.rstrip('/')

    # First, sanity check Ollama is running
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=3)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Cannot reach Ollama at {base_url}. Is `ollama serve` running?")
    except Exception as e:
        raise RuntimeError(f"Ollama health check failed: {e}")

    # Verify the model is pulled
    available = [m.get('name', '') for m in r.json().get('models', [])]
    model_present = any(m == model or m.startswith(model + ':') for m in available)
    if not model_present:
        raise RuntimeError(
            f"Model '{model}' not found in Ollama. "
            f"Run: `ollama pull {model}`. Available: {', '.join(available) or '(none)'}"
        )

    # Make the chat call with JSON-mode enforcement
    try:
        r = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Here is the diagnostic report:\n\n{markdown_report}\n\nProvide your diagnosis as JSON."},
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.4,
                    "num_predict": 4096,
                },
            },
            timeout=180,  # local models can be slow on first call
        )
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Ollama timed out (180s). Model '{model}' may be too slow on this hardware.")
    except Exception as e:
        raise RuntimeError(f"Ollama request failed: {e}")

    data = r.json()
    text = data.get('message', {}).get('content', '')
    if not text:
        raise RuntimeError("Ollama returned empty response")

    return _parse_json_response(text, provider="ollama")

def _parse_json_response(text: str, provider: str) -> Dict[str, Any]:
    """Parse JSON, handling stray markdown fences if model included them."""
    text = text.strip()
    # Strip ```json fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{provider} returned invalid JSON: {e}\n\nRaw response:\n{text[:500]}")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def diagnose(markdown_report: str, provider: str, api_key: Optional[str] = None,
             model: Optional[str] = None, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Main entry point. Returns diagnosis dict + metadata."""
    start = time.time()

    if provider == "claude":
        result = diagnose_with_claude(markdown_report, api_key, model or "claude-sonnet-4-5")
    elif provider == "openai":
        result = diagnose_with_openai(markdown_report, api_key, model or "gpt-4o-mini")
    elif provider == "gemini":
        result = diagnose_with_gemini(markdown_report, api_key, model or "gemini-2.0-flash")
    elif provider == "ollama":
        result = diagnose_with_ollama(
            markdown_report,
            base_url=base_url or "http://localhost:11434",
            model=model or "llama3.2",
        )
    else:
        raise ValueError(f"Unknown provider: {provider}. Use 'claude', 'openai', 'gemini', or 'ollama'.")

    return {
        "provider": provider,
        "model": model,
        "elapsed_sec": round(time.time() - start, 2),
        "diagnosis": result,
    }