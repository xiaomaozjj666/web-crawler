"""AI-assisted extraction: generate, validate, and self-heal CSS selectors.

This layer augments the deterministic adaptive engine (fingerprint + structural
similarity) with an LLM. You describe *what* you want in plain language (a
``schema`` of ``field -> description``) and the extractor asks the model to
propose CSS selectors, then **validates every selector against the real page
using the project's own** :class:`~web_crawler.parser.selector.Selector`. Fields
that come back empty trigger a self-heal round where the model is re-prompted
with the failing fields and a fresh HTML sample.

Nothing here bypasses site protections: it only turns already-fetched HTML into
structured data. The heavy lifting stays in the existing, offline Selector.

Example
-------
>>> from web_crawler import Fetcher, AIExtractor
>>> resp = Fetcher().get("https://example.com")
>>> extractor = AIExtractor()  # DeepSeek-V4-Pro by default
>>> data = extractor.extract(resp, {
...     "title": "the main page heading",
...     "links": "all anchor hrefs in the nav",
... })
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .llm import LLMMessage, LLMProvider, get_provider

if TYPE_CHECKING:
    from ..parser.selector import Selector
    from ..response import Response

# 从模型回复里提取 JSON 对象（容忍 ```json 代码块包裹）
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are a web-scraping assistant. Given an HTML snippet and a list of "
    "fields to extract, respond with ONLY a JSON object mapping each field "
    "name to a single CSS selector that locates its value. Use the Scrapling "
    "pseudo-element '::attr(name)' to target an attribute (e.g. "
    "'a.link::attr(href)'); omit it to target the element's text. Return valid "
    "JSON with no commentary."
)


@dataclass
class ExtractionResult:
    """Outcome of an :meth:`AIExtractor.extract` call."""

    data: dict[str, Any]
    selectors: dict[str, str]
    missing: list[str] = field(default_factory=list)
    rounds: int = 1

    @property
    def ok(self) -> bool:
        return not self.missing


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort parse of a JSON object embedded in ``text``."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


def _as_selector(source: Response | Selector) -> Selector:
    """Accept a :class:`Response` or a :class:`Selector` and return a Selector."""
    # Response exposes a `.selector` property; Selector has `.css` directly.
    sel = getattr(source, "selector", None)
    if sel is not None and hasattr(sel, "css_first"):
        return sel
    return source  # type: ignore[return-value]


class AIExtractor:
    """LLM-backed selector generation with validation and self-healing.

    Parameters
    ----------
    provider:
        Any object satisfying :class:`~web_crawler.ai.llm.LLMProvider`. When
        omitted, a DeepSeek provider (model ``DeepSeek-V4-Pro``) is created.
    model:
        Convenience: model name forwarded to the default provider.
    max_html_chars:
        Upper bound on the HTML sample sent to the model (keeps prompts small).
    max_heal_rounds:
        How many extra correction rounds to attempt for empty fields.
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        model: str | None = None,
        max_html_chars: int = 12000,
        max_heal_rounds: int = 2,
    ) -> None:
        if provider is None:
            provider = get_provider(model=model) if model else get_provider()
        self.provider = provider
        self.max_html_chars = max_html_chars
        self.max_heal_rounds = max_heal_rounds

    # -- prompt building ----------------------------------------------------
    def _html_sample(self, sel: Selector) -> str:
        # Selector exposes the serialized markup via its `.html` property.
        html = getattr(sel, "html", None)
        if not isinstance(html, str):
            html = str(html)
        return html[: self.max_html_chars]

    def _build_prompt(
        self,
        html: str,
        schema: dict[str, str],
        *,
        failing: dict[str, str] | None = None,
    ) -> list[LLMMessage]:
        fields_desc = "\n".join(f"- {name}: {desc}" for name, desc in schema.items())
        parts = [f"Fields to extract:\n{fields_desc}"]
        if failing:
            broken = "\n".join(
                f"- {name}: {css!r} returned nothing" for name, css in failing.items()
            )
            parts.append(
                "These selectors from the previous attempt failed; propose "
                f"better ones for ONLY these fields:\n{broken}"
            )
        parts.append(f"HTML snippet:\n{html}")
        return [
            LLMMessage("system", _SYSTEM_PROMPT),
            LLMMessage("user", "\n\n".join(parts)),
        ]

    def suggest_selectors(
        self,
        source: Response | Selector,
        schema: dict[str, str],
        *,
        failing: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Ask the model for a ``{field: css}`` mapping (no validation)."""
        sel = _as_selector(source)
        messages = self._build_prompt(self._html_sample(sel), schema, failing=failing)
        reply = self.provider.chat(messages, temperature=0.0)
        raw = _extract_json(reply.content)
        # 只保留 schema 内、值为字符串的字段
        return {k: v for k, v in raw.items() if k in schema and isinstance(v, str)}

    # -- application --------------------------------------------------------
    @staticmethod
    def _apply(sel: Selector, css: str) -> Any:
        """Apply one selector, returning text or attribute value(s) or None."""
        first = sel.css_first(css)
        if first is None:
            return None
        # css_first already resolves ::attr(...) to a value; element -> text.
        value = first if isinstance(first, str) else getattr(first, "text", None)
        return str(value).strip() if value is not None else None

    def extract(
        self,
        source: Response | Selector,
        schema: dict[str, str],
        *,
        self_heal: bool = True,
    ) -> ExtractionResult:
        """Extract ``schema`` fields, validating and self-healing selectors.

        Returns an :class:`ExtractionResult` with the extracted ``data``, the
        final ``selectors`` used, and any ``missing`` fields still unresolved.
        """
        sel = _as_selector(source)
        selectors = self.suggest_selectors(source, schema)
        data: dict[str, Any] = {}
        for field_name, css in selectors.items():
            data[field_name] = self._apply(sel, css)

        rounds = 1
        if self_heal:
            while rounds <= self.max_heal_rounds:
                failing = {name: selectors.get(name, "") for name in schema if not data.get(name)}
                if not failing:
                    break
                fixes = self.suggest_selectors(source, schema, failing=failing)
                if not fixes:
                    break
                for name, css in fixes.items():
                    value = self._apply(sel, css)
                    if value:  # 仅在自愈确有结果时才覆盖
                        selectors[name] = css
                        data[name] = value
                rounds += 1

        missing = [name for name in schema if not data.get(name)]
        return ExtractionResult(data=data, selectors=selectors, missing=missing, rounds=rounds)


__all__ = ["AIExtractor", "ExtractionResult"]
