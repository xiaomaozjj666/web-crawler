"""VLM-based visual content extraction from screenshot tiles.

PixelRAG-inspired: instead of parsing HTML text, render pages as screenshot
tiles and feed them directly to a vision-language model (VLM) for content
extraction. This preserves tables, charts, layout, and visual hierarchy that
HTML-to-text conversion destroys.

Usage::

    from web_crawler import DynamicFetcher, VisualExtractor

    with DynamicFetcher() as fetcher:
        tiles = fetcher.screenshot_tiles("https://example.com")
        extractor = VisualExtractor(
            api_key="...", base_url="https://api.openai.com/v1", model="gpt-4o"
        )
        text = extractor.extract(tiles, "Extract the main content")
"""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen


class VisualExtractor:
    """Use a VLM (OpenAI-compatible vision API) to extract content from screenshot tiles.

    Parameters
    ----------
    api_key:
        API key for the vision model service.
    base_url:
        OpenAI-compatible base URL (e.g. ``https://api.deepseek.com/v1`` or
        ``https://dashscope.aliyuncs.com/compatible-mode/v1``).
    model:
        Vision-capable model name (e.g. ``gpt-4o``, ``qwen-vl-max``,
        ``qwen3.7-plus``, ``deepseek-chat``).
    max_tokens:
        Maximum output tokens (default 4096).
    timeout:
        HTTP request timeout in seconds (default 120).
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        tiles: list[dict[str, Any]],
        prompt: str = (
            "Please extract the main content from this web page screenshot. "
            "Preserve headings, paragraph structure, table data (as markdown tables), "
            "lists, and key numbers. If it is an article, summarize the key points. "
            "If it contains data tables or charts, describe the data accurately."
        ),
        *,
        temperature: float = 0.3,
        max_tiles: int = 20,
    ) -> str:
        """Extract structured text content from screenshot tiles via VLM.

        Parameters
        ----------
        tiles:
            List of tile dicts as returned by :meth:`DynamicFetcher.screenshot_tiles`.
            Each must have ``b64`` (base64-encoded image).
        prompt:
            Instruction for the VLM describing what to extract.
        temperature:
            Sampling temperature (0.0–2.0). Lower = more deterministic.
        max_tiles:
            Maximum number of tiles to send (capped to avoid token overflow).

        Returns
        -------
        str
            The VLM's extracted text content.
        """
        if not tiles:
            raise ValueError("tiles must not be empty")

        # Cap tile count to stay within typical VLM context windows
        tiles = tiles[:max_tiles]

        # Build vision API content array
        image_contents: list[dict[str, Any]] = []
        for i, tile in enumerate(tiles):
            b64_data = tile["b64"]
            mime = "image/png"
            image_contents.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64_data}",
                    "detail": "auto",
                },
            })
            # If sending multiple tiles, annotate each
            if len(tiles) > 1:
                image_contents.insert(
                    len(image_contents) - 1,
                    {"type": "text", "text": f"\n--- Page section {i + 1}/{len(tiles)} ---\n"},
                )

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *image_contents,
                ],
            },
        ]

        return self._call_api(messages, temperature)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_api(self, messages: list[dict[str, Any]], temperature: float) -> str:
        """Make an OpenAI-compatible chat completion request."""
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
        }).encode("utf-8")

        url = f"{self.base_url}/chat/completions"
        req = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"VLM API call failed: {exc}") from exc

        # Extract the assistant message
        choices = data.get("choices", [])
        if not choices:
            error_msg = data.get("error", {}).get("message", "unknown error")
            raise RuntimeError(f"VLM API returned no choices: {error_msg}")

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if content is None:
            # Some models return null content for refusal
            finish = choices[0].get("finish_reason", "unknown")
            raise RuntimeError(f"VLM returned empty content (finish_reason={finish})")

        return str(content).strip()

    def extract_with_client(
        self,
        tiles: list[dict[str, Any]],
        prompt: str,
        *,
        client: Any = None,
        temperature: float = 0.3,
        max_tiles: int = 20,
    ) -> str:
        """Like :meth:`extract` but accepts an existing OpenAI client instance.

        Pass an ``openai.OpenAI`` or ``openai.AsyncOpenAI`` client to reuse
        an existing connection pool. Requires the ``openai`` package.

        Parameters
        ----------
        client:
            An ``openai.OpenAI`` instance. If ``None``, falls back to
            :meth:`extract` using urllib.
        """
        if client is None:
            return self.extract(tiles, prompt, temperature=temperature, max_tiles=max_tiles)

        if not tiles:
            raise ValueError("tiles must not be empty")

        tiles = tiles[:max_tiles]

        image_contents: list[dict[str, Any]] = []
        for i, tile in enumerate(tiles):
            b64_data = tile["b64"]
            mime = "image/png"
            image_contents.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64_data}", "detail": "auto"},
            })
            if len(tiles) > 1:
                image_contents.insert(
                    len(image_contents) - 1,
                    {"type": "text", "text": f"\n--- Page section {i + 1}/{len(tiles)} ---\n"},
                )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *image_contents,
                ],
            },
        ]

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            raise RuntimeError(f"VLM client call failed: {exc}") from exc

        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("VLM returned empty content")
        return str(content).strip()


__all__ = ["VisualExtractor"]
