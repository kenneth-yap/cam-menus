"""Extraction components — fetch, reduce, and LLM extraction.

These are the three workhorse functions, ported from the proof-of-concept
notebook into a clean importable module. The pipeline calls them in order:
    fetch_html  ->  html_to_text  ->  extract_menu

Each does ONE job, so when something breaks you know which stage to inspect.
"""

from __future__ import annotations

import os

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

from .schema import CollegeMenu

# ---------------------------------------------------------------------------
# Model adapters + fallback chain
# ---------------------------------------------------------------------------
# Each provider/model is wrapped in an ADAPTER: a small object exposing a common
# interface (`.name` and `.extract(college, text) -> CollegeMenu`). The chain is
# an ordered list of adapters. extract_menu() walks them: a transient error
# (overload, rate limit) advances to the next; a genuine error raises at once.
#
# Why this shape: it decouples the pipeline from any one provider. Adding a new
# provider later (e.g. Claude) is writing ONE new adapter class and appending an
# instance to MODEL_CHAIN — the pipeline and storage never change. This is the
# anti-corruption boundary applied to the model layer itself.

# Substrings that mark an error as TRANSIENT (worth trying the next adapter).
# A genuine bad-input/auth error is NOT here, so it raises immediately rather
# than pointlessly retrying a malformed page against every model.
_TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded")


def _is_transient(exc: Exception) -> bool:
    """True if the error is worth retrying on the next adapter in the chain."""
    msg = str(exc)
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


class GeminiAdapter:
    """Adapter for any Gemini model via the google-genai SDK.

    Uses Gemini's structured-output mode: response_schema constrains the model to
    emit JSON matching our Pydantic schema, and the SDK parses it back into a
    CollegeMenu on `.parsed`. temperature=0 makes extraction as repeatable as
    possible.
    """

    def __init__(self, model: str):
        self.name = model           # e.g. 'gemini-3.1-flash-lite'
        self._client = None         # built lazily on first use

    def extract(self, college: str, page_text: str) -> CollegeMenu:
        if self._client is None:
            self._client = _build_client()
        response = self._client.models.generate_content(
            model=self.name,
            contents=f"College: {college}\n\nPage text:\n{page_text}",
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=CollegeMenu,
                temperature=0.0,
            ),
        )
        return response.parsed


# ---------------------------------------------------------------------------
# To add Claude later, write a class with the same shape and append it below:
#
#   class ClaudeAdapter:
#       def __init__(self, model: str):
#           self.name = model
#           self._client = None
#       def extract(self, college: str, page_text: str) -> CollegeMenu:
#           # call Anthropic API with tool/structured output, return CollegeMenu
#           ...
#
# then: MODEL_CHAIN.append(ClaudeAdapter("claude-haiku-4-5"))
# Nothing in pipeline.py or storage.py needs to change.
# ---------------------------------------------------------------------------

# Ordered fallback chain, lightest-capable first. Flash-Lite is genuinely
# well-suited to structured extraction, not merely a cheap fallback.
MODEL_CHAIN = [
    GeminiAdapter("gemini-3.1-flash-lite"),   # lightest, highest throughput
    GeminiAdapter("gemini-3.5-flash"),        # stronger backup
    GeminiAdapter("gemini-2.5-flash"),        # older but reliable third option
]


def _build_client() -> genai.Client:
    """Create the Gemini client from the environment key.

    Done lazily in a function so importing this module never fails just because
    a key is missing — only calling extract_menu requires it.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. In PowerShell:\n"
            '    $env:GEMINI_API_KEY = "your-key-here"'
        )
    return genai.Client(api_key=api_key)


# A realistic User-Agent. Some sites serve stripped pages to unknown clients.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Stage 1 — fetch
# ---------------------------------------------------------------------------
def fetch_html(url: str, timeout: int = 20) -> str:
    """Return raw HTML for a URL. Raises on HTTP error or timeout."""
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Stage 2 — reduce
# ---------------------------------------------------------------------------
def html_to_text(html: str) -> str:
    """Strip boilerplate and return readable plain text.

    Remove script/style/nav/footer/header tags, then extract text with newline
    separators so the day/meal structure survives for the model. This cuts token
    cost and improves accuracy by removing noise.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 3 — extract
# ---------------------------------------------------------------------------
_SYSTEM_INSTRUCTION = """\
You extract dining-hall menus from college webpage text into a strict schema.

Rules:
- Include breakfast, brunch, lunch, and dinner sittings where present. Brunch is
  common at weekends. Ignore opening hours, contact details, and prose.
- Normalise dietary tags to lowercase from this set only:
  vegan, vegetarian, halal, gluten-free.
  Map common codes: (V)/V -> vegetarian, (VG)/VG/Vegan -> vegan, (H)/H -> halal,
  GF -> gluten-free. Strip these codes out of the dish 'name'.
- Clean dish names: remove leading bullets, asterisks, and allergen codes.
- If a course (starter/main/side/dessert) is indicated, set it; otherwise null.
- For week_commencing, give the Monday of the menu week as YYYY-MM-DD if you can
  derive it from the page; otherwise null.
- If you cannot find a menu at all, return the college with an empty days list.
- Never invent dishes. Only report what the text supports.
"""


def extract_menu(college: str, page_text: str) -> tuple[CollegeMenu, str]:
    """Turn reduced page text into a validated CollegeMenu.

    Walks MODEL_CHAIN in order, trying each adapter. Returns (menu, model_used)
    so the caller can log which model produced the extraction — important
    because different models extract slightly differently, and recording it
    keeps your data honest for later analysis.

    A transient error (overload, rate limit) advances to the next adapter. Any
    other error raises immediately: retrying a malformed page on a different
    model would just waste quota. If every adapter fails transiently, the last
    error is raised so the pipeline records a clean 'failed'.
    """
    last_error: Exception | None = None

    for adapter in MODEL_CHAIN:
        try:
            menu = adapter.extract(college, page_text)
            return menu, adapter.name
        except Exception as exc:  # noqa: BLE001
            if not _is_transient(exc):
                raise          # genuine failure — don't try other adapters
            last_error = exc   # transient — fall through to the next adapter

    raise RuntimeError(
        f"all adapters in chain failed; last error: {last_error!r}"
    )