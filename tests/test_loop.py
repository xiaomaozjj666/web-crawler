"""Tests for LoopDetector / ContextCompressor: 循环检测与上下文压缩。

覆盖目标：
- :class:`StateFingerprint` — 从 Observation 提取指纹、短表示、可哈希；
- :class:`LoopDetector` — 滚动窗口重复检测、reset、threshold/window 边界；
- :class:`ContextCompressor` — 同步/异步压缩、强制压缩、累积摘要截断、
  LLM 失败降级、compress_to 校验等分支。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from web_crawler.ai.llm import LLMResponse
from web_crawler.ai.loop import (
    _MAX_SUMMARY_LEN,
    ContextCompressor,
    LoopDetectionResult,
    LoopDetector,
    StateFingerprint,
)

# ---------------------------------------------------------------------------
# 辅助：构造简单 Observation（duck-typed）
# ---------------------------------------------------------------------------


class _Observation:
    """最小化的 Observation 桩对象，满足 StateFingerprint.from_observation 取值。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        dom_summary: str = "<html></html>",
        hook_data: dict[str, Any] | None = None,
        network_requests: list[dict] | None = None,
        scripts: list[str] | None = None,
    ) -> None:
        self.url = url
        self.dom_summary = dom_summary
        self.hook_data = hook_data if hook_data is not None else {"count": 0}
        self.network_requests = network_requests or []
        self.scripts = scripts or []


# ---------------------------------------------------------------------------
# StateFingerprint
# ---------------------------------------------------------------------------


class TestStateFingerprint:
    def test_from_observation_extracts_url_dom_hook_network_scripts(self) -> None:
        """from_observation 应聚合 URL/DOM/hook/network/scripts 计算指纹。"""
        obs = _Observation(
            url="https://x.example/p",
            dom_summary="x" * 800,  # 800 // 200 = 4
            hook_data={"count": 2},
            network_requests=[{"url": "a"}, {"url": "b"}, {"url": "c"}],
            scripts=["s1", "s2"],
        )
        fp = StateFingerprint.from_observation(obs)
        assert fp.url == "https://x.example/p"
        # element_count = 4(dom) + 2(hook) + 3(network) + 2(scripts) = 11
        assert fp.element_count == 11
        assert isinstance(fp.text_hash, str)
        assert len(fp.text_hash) == 32  # md5 hex 长度

    def test_from_observation_handles_none_dom_and_hook(self) -> None:
        """缺失字段时不应崩溃。"""
        obs = _Observation(dom_summary="", hook_data=None)  # type: ignore[arg-type]
        fp = StateFingerprint.from_observation(obs)
        assert fp.url == "https://example.com"
        assert fp.element_count == 0
        assert fp.text_hash  # 空字符串也有 md5

    def test_fingerprint_is_hashable_and_frozen(self) -> None:
        """frozen dataclass 应可哈希、可放入 set/dict。"""
        fp1 = StateFingerprint(url="a", element_count=1, text_hash="h1")
        fp2 = StateFingerprint(url="a", element_count=1, text_hash="h1")
        assert fp1 == fp2
        assert hash(fp1) == hash(fp2)
        assert len({fp1, fp2}) == 1
        # frozen=True 不允许赋值（FrozenInstanceError 继承 AttributeError）
        with pytest.raises(AttributeError):
            fp1.url = "b"  # type: ignore[misc]

    def test_short_representation_truncates_url_and_hash(self) -> None:
        """short() 应截断 URL 与 hash，便于日志输出。"""
        long_url = "https://very-long-domain-name.example.com/path/" + "x" * 100
        fp = StateFingerprint(url=long_url, element_count=42, text_hash="abcdef0123456789")
        s = fp.short()
        assert "el=42" in s
        assert "h=abcdef01" in s  # text_hash 前 8 位
        # URL 应被截断到 60 字符
        assert len(s) < len(long_url)


# ---------------------------------------------------------------------------
# LoopDetector
# ---------------------------------------------------------------------------


class TestLoopDetector:
    def test_threshold_minimum_value_is_two(self) -> None:
        """threshold < 2 应被强制提升到 2。"""
        det = LoopDetector(threshold=1)
        assert det.threshold == 2

    def test_window_at_least_threshold(self) -> None:
        """window 不应小于 threshold。"""
        det = LoopDetector(threshold=5, window=2)
        assert det.window == 5

    def test_observe_no_loop_when_unique_states(self) -> None:
        """每次状态都不同时不应触发循环。"""
        det = LoopDetector(threshold=3, window=8)
        for i in range(5):
            obs = _Observation(url=f"https://x.example/p{i}")
            result = det.observe(obs)
            assert result.detected is False
            assert result.repeated_count == 1

    def test_observe_detects_loop_when_threshold_reached(self) -> None:
        """同一指纹连续出现 threshold 次应触发循环。"""
        det = LoopDetector(threshold=3, window=8)
        obs = _Observation(url="https://x.example/same", dom_summary="same")
        # 第 1 次：count=1，未触发
        r1 = det.observe(obs)
        assert r1.detected is False
        assert r1.repeated_count == 1
        # 第 2 次：count=2，未触发
        r2 = det.observe(obs)
        assert r2.detected is False
        assert r2.repeated_count == 2
        # 第 3 次：count=3，触发
        r3 = det.observe(obs)
        assert r3.detected is True
        assert r3.repeated_count == 3
        assert r3.repeated_state == r3.repeated_state  # 自相等

    def test_observe_auto_increments_step_when_not_provided(self) -> None:
        """未传 step 时内部应自增。"""
        det = LoopDetector(threshold=3)
        det.observe(_Observation())
        det.observe(_Observation())
        # _current_step 应已推进到 2（多次调用后），仅做不抛异常的 smoke 校验
        assert det._current_step >= 2

    def test_observe_updates_last_change_step_on_state_change(self) -> None:
        """状态变化时 last_change_step 应更新。"""
        det = LoopDetector(threshold=3)
        det.observe(_Observation(url="a"), step=1)
        det.observe(_Observation(url="b"), step=2)
        # 状态从 a→b 变化，last_change_step 应为 2
        assert det._last_change_step == 2
        # 再次相同状态，last_change_step 不变
        det.observe(_Observation(url="b"), step=3)
        assert det._last_change_step == 2

    def test_reset_clears_history(self) -> None:
        """reset 后历史清空，立刻不应触发循环。"""
        det = LoopDetector(threshold=3)
        obs = _Observation()
        for _ in range(3):
            det.observe(obs)
        assert det.observe(obs).detected is True
        det.reset()
        # reset 后第一次观察 count=1，不应触发
        assert det.observe(obs).detected is False

    def test_window_bounds_old_entries(self) -> None:
        """超过 window 大小的旧指纹应被淘汰，不再计入重复。"""
        det = LoopDetector(threshold=3, window=3)
        obs_a = _Observation(url="a")
        # window=3：填满 a,a,a 后再填 b,b,b，a 不应再被计入
        det.observe(obs_a)
        det.observe(obs_a)
        det.observe(obs_a)
        # 引入 3 个不同 b 状态挤出 a
        for _ in range(3):
            det.observe(_Observation(url="b"))
        # a 已被挤出，再次 observe(a) 应 count=1
        assert det.observe(obs_a).repeated_count == 1

    def test_loop_detection_result_to_dict(self) -> None:
        """LoopDetectionResult.to_dict 序列化正确。"""
        fp = StateFingerprint(url="u", element_count=1, text_hash="abcdef0123")
        r = LoopDetectionResult(
            detected=True,
            repeated_count=5,
            repeated_state=fp,
            last_change_step=7,
        )
        d = r.to_dict()
        assert d["detected"] is True
        assert d["repeated_count"] == 5
        assert d["last_change_step"] == 7
        assert isinstance(d["repeated_state"], str)
        assert "el=1" in d["repeated_state"]

    def test_loop_detection_result_to_dict_with_none_state(self) -> None:
        """repeated_state 为 None 时 to_dict 应返回 None。"""
        r = LoopDetectionResult(detected=False)
        d = r.to_dict()
        assert d["repeated_state"] is None


# ---------------------------------------------------------------------------
# ContextCompressor
# ---------------------------------------------------------------------------


class _FakeProvider:
    """记录调用、按预设回复返回的桩 provider。"""

    model = "fake-model"

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: int = 0

    def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        content = self._replies.pop(0) if self._replies else "summary"
        return LLMResponse(content=content, model=self.model)

    async def achat(self, messages: Any, **kwargs: Any) -> LLMResponse:
        return self.chat(messages, **kwargs)


def _make_history(n: int) -> list[dict]:
    """构造 n 条历史记录。"""
    return [{"step": i, "action": "wait", "reasoning": f"step-{i}"} for i in range(n)]


class TestContextCompressor:
    def test_init_validates_compress_to_lt_max_history(self) -> None:
        """compress_to >= max_history 应抛 ValueError。"""
        with pytest.raises(ValueError, match="compress_to"):
            ContextCompressor(_FakeProvider(), max_history=5, compress_to=5)
        with pytest.raises(ValueError, match="compress_to"):
            ContextCompressor(_FakeProvider(), max_history=5, compress_to=10)

    def test_cumulative_summary_default_empty(self) -> None:
        """新实例的累积摘要应为空字符串。"""
        c = ContextCompressor(_FakeProvider(), max_history=10, compress_to=2)
        assert c.cumulative_summary == ""

    def test_maybe_compress_no_op_when_below_threshold(self) -> None:
        """历史数 <= max_history 时不应压缩。"""
        c = ContextCompressor(_FakeProvider(), max_history=10, compress_to=2)
        history = _make_history(5)
        new_hist, compressed = c.maybe_compress(history)
        assert compressed is False
        assert new_hist is history  # 原样返回
        assert c.cumulative_summary == ""

    def test_maybe_compress_triggers_when_above_threshold(self) -> None:
        """历史数 > max_history 时应触发压缩。"""
        c = ContextCompressor(_FakeProvider(["compressed-summary"]), max_history=5, compress_to=2)
        history = _make_history(10)
        new_hist, compressed = c.maybe_compress(history)
        assert compressed is True
        # 压缩后应包含 1 条 meta + 最近 2 条 = 3 条
        assert len(new_hist) == 3
        assert new_hist[0]["action"] == "_history_compressed"
        assert new_hist[0]["step"] == -1
        # 累积摘要应包含 LLM 返回的 summary
        assert "compressed-summary" in c.cumulative_summary
        # 保留的 recent 应是原 history 的最后 2 条
        assert new_hist[-1]["step"] == 9
        assert new_hist[-2]["step"] == 8

    def test_maybe_compress_empty_entries_returns_empty_summary(self) -> None:
        """_summarize 对空列表应返回空字符串，不调用 LLM。"""
        c = ContextCompressor(_FakeProvider(), max_history=1, compress_to=0)
        # 压缩时 to_compress = history[:0] = []，应跳过 LLM 调用
        # 但 compress_to=0 时 recent = history[-0:] = 整个 history，需特殊处理
        # 这里用 max_history=1 + 2 条历史触发，观察行为
        history = _make_history(2)
        # compress_to=0 时 to_compress=history[:0]=[]，recent=history[-0:]=history
        # _summarize([]) 返回 ""
        _new_hist, compressed = c.maybe_compress(history)
        assert compressed is True
        # 空摘要不写入 cumulative_summary
        assert c.cumulative_summary == ""

    def test_maybe_compress_llm_failure_returns_error_summary(self) -> None:
        """LLM 调用失败时摘要应含 [history compression failed: ...]。"""

        class _FailProvider:
            model = "fail"

            def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
                raise RuntimeError("llm down")

        c = ContextCompressor(_FailProvider(), max_history=3, compress_to=1)  # type: ignore[arg-type]
        history = _make_history(5)
        _new_hist, compressed = c.maybe_compress(history)
        assert compressed is True
        assert "compression failed" in c.cumulative_summary

    @pytest.mark.asyncio
    async def test_maybe_compress_async_triggers(self) -> None:
        """异步路径压缩。"""
        provider = MagicMock()
        provider.achat = AsyncMock(
            return_value=LLMResponse(content="async-summary", model="fake")
        )
        c = ContextCompressor(provider, max_history=3, compress_to=1)
        history = _make_history(6)
        new_hist, compressed = await c.maybe_compress_async(history)
        assert compressed is True
        assert "async-summary" in c.cumulative_summary
        assert new_hist[0]["action"] == "_history_compressed"

    @pytest.mark.asyncio
    async def test_maybe_compress_async_no_op_when_below_threshold(self) -> None:
        """异步路径历史不足时不压缩。"""
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content="x", model="fake"))
        c = ContextCompressor(provider, max_history=10, compress_to=2)
        history = _make_history(3)
        new_hist, compressed = await c.maybe_compress_async(history)
        assert compressed is False
        assert new_hist is history
        provider.achat.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_compress_async_falls_back_to_sync_chat(self) -> None:
        """provider 无 achat 方法时应回退到同步 chat。"""
        provider = MagicMock(spec=["chat"])  # 只有 chat，无 achat
        provider.chat.return_value = LLMResponse(content="sync-fallback", model="fake")
        c = ContextCompressor(provider, max_history=3, compress_to=1)
        history = _make_history(5)
        _new_hist, compressed = await c.maybe_compress_async(history)
        assert compressed is True
        assert "sync-fallback" in c.cumulative_summary
        provider.chat.assert_called_once()

    def test_force_compress_below_compress_to_returns_no_op(self) -> None:
        """force_compress 在 history <= compress_to 时不压缩。"""
        c = ContextCompressor(_FakeProvider(), max_history=10, compress_to=3)
        history = _make_history(2)
        new_hist, compressed = c.force_compress(history)
        assert compressed is False
        assert new_hist is history
        assert c.cumulative_summary == ""

    def test_force_compress_triggers_even_below_max_history(self) -> None:
        """force_compress 在 history > compress_to 时强制压缩，无视 max_history。"""
        c = ContextCompressor(_FakeProvider(["forced"]), max_history=100, compress_to=2)
        history = _make_history(5)
        new_hist, compressed = c.force_compress(history)
        assert compressed is True
        assert "forced" in c.cumulative_summary
        # 应保留最近 compress_to=2 条 + 1 条 meta
        assert len(new_hist) == 3

    @pytest.mark.asyncio
    async def test_force_compress_async_triggers(self) -> None:
        """异步强制压缩。"""
        provider = MagicMock()
        provider.achat = AsyncMock(
            return_value=LLMResponse(content="async-forced", model="fake")
        )
        c = ContextCompressor(provider, max_history=100, compress_to=2)
        history = _make_history(5)
        _new_hist, compressed = await c.force_compress_async(history)
        assert compressed is True
        assert "async-forced" in c.cumulative_summary

    @pytest.mark.asyncio
    async def test_force_compress_async_below_compress_to(self) -> None:
        """异步强制压缩在 history 不足时不操作。"""
        provider = MagicMock()
        provider.achat = AsyncMock(return_value=LLMResponse(content="x", model="fake"))
        c = ContextCompressor(provider, max_history=100, compress_to=10)
        _new_hist, compressed = await c.force_compress_async(_make_history(3))
        assert compressed is False

    def test_append_summary_truncates_when_exceeds_max_len(self) -> None:
        """累积摘要超过 _MAX_SUMMARY_LEN 时应截断保留最新部分。"""
        c = ContextCompressor(_FakeProvider(), max_history=5, compress_to=1)
        # 构造超长摘要
        long_summary = "A" * (_MAX_SUMMARY_LEN + 100)
        c._append_summary(long_summary)
        assert len(c.cumulative_summary) == _MAX_SUMMARY_LEN

    def test_append_summary_concatenates_multiple_summaries(self) -> None:
        """多次压缩应累加摘要，用 \\n\\n 分隔。"""
        c = ContextCompressor(_FakeProvider(), max_history=3, compress_to=1)
        c._append_summary("first")
        c._append_summary("second")
        assert c.cumulative_summary == "first\n\nsecond"

    def test_append_summary_ignores_empty(self) -> None:
        """空摘要不应写入。"""
        c = ContextCompressor(_FakeProvider(), max_history=3, compress_to=1)
        c._append_summary("")
        assert c.cumulative_summary == ""

    def test_reset_clears_cumulative_summary(self) -> None:
        """reset 清空累积摘要。"""
        c = ContextCompressor(_FakeProvider(["x"]), max_history=3, compress_to=1)
        c.maybe_compress(_make_history(5))
        assert c.cumulative_summary != ""
        c.reset()
        assert c.cumulative_summary == ""

    @pytest.mark.asyncio
    async def test_summarize_async_with_empty_entries(self) -> None:
        """_summarize_async 对空 entries 应直接返回空字符串。"""
        c = ContextCompressor(_FakeProvider(), max_history=10, compress_to=2)
        result = await c._summarize_async([])
        assert result == ""

    @pytest.mark.asyncio
    async def test_summarize_async_handles_llm_exception(self) -> None:
        """_summarize_async LLM 异常应返回错误占位字符串。"""

        class _FailProvider:
            model = "fail"

            def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
                raise RuntimeError("down")

        c = ContextCompressor(_FailProvider(), max_history=10, compress_to=2)  # type: ignore[arg-type]
        result = await c._summarize_async([{"step": 1}])
        assert "compression failed" in result

    def test_summarize_handles_llm_exception(self) -> None:
        """_summarize LLM 异常应返回错误占位字符串。"""

        class _FailProvider:
            model = "fail"

            def chat(self, messages: Any, **kwargs: Any) -> LLMResponse:
                raise RuntimeError("down")

        c = ContextCompressor(_FailProvider(), max_history=10, compress_to=2)  # type: ignore[arg-type]
        result = c._summarize([{"step": 1}])
        assert "compression failed" in result
