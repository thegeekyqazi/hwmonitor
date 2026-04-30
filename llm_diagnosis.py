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

def diagnose(markdown_report: str, provider: str, api_key: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Main entry point. Returns diagnosis dict + metadata."""
    start = time.time()

    if provider == "claude":
        result = diagnose_with_claude(markdown_report, api_key, model or "claude-sonnet-4-5")
    elif provider == "openai":
        result = diagnose_with_openai(markdown_report, api_key, model or "gpt-4o-mini")
    elif provider == "gemini":
        result = diagnose_with_gemini(markdown_report, api_key, model or "gemini-2.0-flash")
    else:
        raise ValueError(f"Unknown provider: {provider}. Use 'claude', 'openai', or 'gemini'.")

    return {
        "provider": provider,
        "model": model,
        "elapsed_sec": round(time.time() - start, 2),
        "diagnosis": result,
    }