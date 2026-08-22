"""基于 VLM 的截图分块视觉内容提取。

受 PixelRAG 启发：不解析 HTML 文本，而是把页面渲染为截图分块，直接送入
视觉语言模型（VLM）提取内容。这样可以保留 HTML 转文本时会丢失的表格、
图表、布局与视觉层次。

用法::

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
    """用 VLM（OpenAI 兼容视觉 API）从截图分块中提取内容。

    Parameters
    ----------
    api_key:
        视觉模型服务的 API key。
    base_url:
        OpenAI 兼容的 base URL（如 ``https://api.deepseek.com/v1`` 或
        ``https://dashscope.aliyuncs.com/compatible-mode/v1``）。
    model:
        支持视觉的模型名（如 ``gpt-4o``、``qwen-vl-max``、
        ``qwen3.7-plus``、``deepseek-chat``）。
    max_tokens:
        最大输出 token 数（默认 4096）。
    timeout:
        HTTP 请求超时秒数（默认 120）。
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
    # 公开 API
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
        """通过 VLM 从截图分块提取结构化文本内容。

        Parameters
        ----------
        tiles:
            :meth:`DynamicFetcher.screenshot_tiles` 返回的分块 dict 列表，
            每个必须含 ``b64``（base64 编码图片）。
        prompt:
            给 VLM 的指令，说明要提取什么。
        temperature:
            采样温度（0.0–2.0）。越低越确定。
        max_tiles:
            最多发送的分块数（设上限避免 token 溢出）。

        Returns
        -------
        str
            VLM 提取出的文本内容。
        """
        if not tiles:
            raise ValueError("tiles must not be empty")

        # 限制分块数，保持在典型 VLM 上下文窗口内
        tiles = tiles[:max_tiles]

        # 构造视觉 API 的 content 数组
        image_contents: list[dict[str, Any]] = []
        for i, tile in enumerate(tiles):
            b64_data = tile["b64"]
            mime = "image/png"
            image_contents.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64_data}",
                        "detail": "auto",
                    },
                }
            )
            # 发送多个分块时给每块加标注
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
    # 内部实现
    # ------------------------------------------------------------------

    def _call_api(self, messages: list[dict[str, Any]], temperature: float) -> str:
        """发起 OpenAI 兼容的 chat completion 请求。"""
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": temperature,
            }
        ).encode("utf-8")

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

        # 提取助手消息
        choices = data.get("choices", [])
        if not choices:
            error_msg = data.get("error", {}).get("message", "unknown error")
            raise RuntimeError(f"VLM API returned no choices: {error_msg}")

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if content is None:
            # 部分模型拒答时返回 null content
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
        """类似 :meth:`extract`，但接受现成的 OpenAI client 实例。

        传入 ``openai.OpenAI`` 或 ``openai.AsyncOpenAI`` client 以复用现有
        连接池。需要安装 ``openai`` 包。

        Parameters
        ----------
        client:
            ``openai.OpenAI`` 实例。为 ``None`` 时回退到基于 urllib 的
            :meth:`extract`。
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
            image_contents.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64_data}", "detail": "auto"},
                }
            )
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
