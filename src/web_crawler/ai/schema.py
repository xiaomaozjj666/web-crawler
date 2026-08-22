"""结构化抽取 — 依据预期 schema 校验 LLM 的 JSON 输出。

- :class:`SchemaValidator` — JSON schema 校验
- :class:`StructuredExtractor` — 带自动重试的 LLM 结构化抽取
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Pydantic 软依赖：缺失时降级为关键字段存在性检查
try:  # pragma: no cover - 取决于是否安装了 pydantic
    from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

    _HAS_PYDANTIC = True
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore[assignment, misc]
    ValidationError = None  # type: ignore[assignment, misc]
    TypeAdapter = None  # type: ignore[assignment, misc]
    ConfigDict = None  # type: ignore[assignment, misc]
    _HAS_PYDANTIC = False


# ---------------------------------------------------------------------------
# 内置 Schema
# ---------------------------------------------------------------------------

if _HAS_PYDANTIC:

    class ExtractedParams(BaseModel):
        """目标参数抽取结果的 schema。

        用于校验 Agent ``extract`` 动作或 LLM 返回的目标参数表。
        """

        model_config = ConfigDict(extra="allow")

        params: dict[str, str] = field(default_factory=dict)
        confidence: float = 0.0

    class HookRecord(BaseModel):
        """单条 Hook 捕获记录的 schema。"""

        model_config = ConfigDict(extra="allow")

        type: str = ""
        url: str = ""
        method: str = ""
        headers: dict[str, str] = field(default_factory=dict)
        body: str | None = None
        timestamp: float | None = None

    class WebPageState(BaseModel):
        """网页状态摘要的 schema。"""

        model_config = ConfigDict(extra="allow")

        url: str
        title: str = ""
        captcha_type: str = "none"
        hook_count: int = 0
        network_count: int = 0
        script_count: int = 0

else:  # pragma: no cover - Pydantic 不可用时的占位类型，仅用于类型注解

    class ExtractedParams:  # type: ignore[no-redef]
        """目标参数抽取结果的 schema（Pydantic 未安装时的占位）。"""

    class HookRecord:  # type: ignore[no-redef]
        """单条 Hook 捕获记录的 schema（Pydantic 未安装时的占位）。"""

    class WebPageState:  # type: ignore[no-redef]
        """网页状态摘要的 schema（Pydantic 未安装时的占位）。"""


# ---------------------------------------------------------------------------
# 验证结果
# ---------------------------------------------------------------------------


@dataclass
class FieldError:
    """单字段验证错误。"""

    loc: list[str]
    msg: str
    type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"loc": list(self.loc), "msg": self.msg, "type": self.type}


@dataclass
class ValidationResult:
    """验证结果。"""

    valid: bool
    errors: list[FieldError] = field(default_factory=list)
    # 强制类型转换后的数据（pydantic v2 会做 coerce）
    coerced: dict[str, Any] = field(default_factory=dict)
    # 原始输入
    raw: Any = None

    @property
    def error_messages(self) -> list[str]:
        """人类可读的错误消息列表。"""
        return [f"{'.'.join(e.loc)}: {e.msg}" for e in self.errors]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "coerced": self.coerced,
            "raw": self.raw if isinstance(self.raw, (str, int, float, bool, list, dict)) else None,
        }


# ---------------------------------------------------------------------------
# 验证器
# ---------------------------------------------------------------------------


class SchemaValidator:
    """结构化数据验证器。

    Parameters
    ----------
    schema:
        Pydantic BaseModel 子类或 TypeAdapter 实例。Pydantic 未安装时
        仅支持 dict schema（关键字段存在性检查）。
    required_keys:
        Pydantic 不可用时的降级检查：必须存在的字段名列表。
    """

    def __init__(
        self,
        schema: Any | None = None,
        *,
        required_keys: list[str] | None = None,
    ) -> None:
        self.schema = schema
        self.required_keys = list(required_keys) if required_keys else []
        self._adapter: Any | None = None
        if _HAS_PYDANTIC and schema is not None:
            try:
                self._adapter = TypeAdapter(schema)
            except Exception:
                self._adapter = None

    # ------------------------------------------------------------------
    # 同步入口
    # ------------------------------------------------------------------

    def validate(self, data: Any) -> ValidationResult:
        """同步：验证 ``data`` 是否符合 schema。"""
        # Pydantic 可用 + adapter 构造成功
        if self._adapter is not None:
            try:
                coerced = self._adapter.validate_python(data)
                # 转 dict 便于序列化
                if hasattr(coerced, "model_dump"):
                    coerced_dict = coerced.model_dump()
                elif isinstance(coerced, dict):
                    coerced_dict = coerced
                else:
                    coerced_dict = {"value": coerced}
                return ValidationResult(
                    valid=True,
                    coerced=coerced_dict,
                    raw=data,
                )
            except ValidationError as exc:
                errors = [
                    FieldError(
                        loc=[str(p) for p in e.get("loc", [])],
                        msg=str(e.get("msg", "")),
                        type=str(e.get("type", "")),
                    )
                    for e in exc.errors()
                ]
                return ValidationResult(valid=False, errors=errors, raw=data)

        # Pydantic 不可用 或 schema 未设置：走降级路径
        return self._validate_fallback(data)

    # ------------------------------------------------------------------
    # 异步入口
    # ------------------------------------------------------------------

    async def validate_async(self, data: Any) -> ValidationResult:
        """异步：验证 ``data`` 是否符合 schema。

        Pydantic 是同步库，这里只是把同步调用包装为 async 以匹配接口。
        """
        return self.validate(data)

    # ------------------------------------------------------------------
    # 降级路径
    # ------------------------------------------------------------------

    def _validate_fallback(self, data: Any) -> ValidationResult:
        """Pydantic 不可用 / schema 未设置时的降级验证。"""
        if not self.required_keys:
            # 没有约束 → 默认通过
            return ValidationResult(
                valid=True,
                coerced=data if isinstance(data, dict) else {"value": data},
                raw=data,
            )
        if not isinstance(data, dict):
            return ValidationResult(
                valid=False,
                errors=[
                    FieldError(
                        loc=[], msg=f"expected dict, got {type(data).__name__}", type="type_error"
                    )
                ],
                raw=data,
            )
        errors: list[FieldError] = []
        coerced = dict(data)
        for key in self.required_keys:
            if key not in data:
                errors.append(FieldError(loc=[key], msg="field required", type="missing"))
            elif data[key] in (None, "", []):
                errors.append(FieldError(loc=[key], msg="value is empty", type="value_error.empty"))
        return ValidationResult(
            valid=not errors,
            errors=errors,
            coerced=coerced,
            raw=data,
        )

    # ------------------------------------------------------------------
    # 便捷：验证 + 自动修复 prompt
    # ------------------------------------------------------------------

    def validate_and_repair_prompt(self, data: Any) -> tuple[ValidationResult, str]:
        """验证并生成"修复提示词"。

        验证通过时返回空字符串；失败时返回一段可发给 LLM 的修复指令。
        """
        result = self.validate(data)
        if result.valid:
            return result, ""
        # 生成修复提示词
        errors_json = json.dumps(
            [e.to_dict() for e in result.errors],
            ensure_ascii=False,
            indent=2,
        )
        raw_json = json.dumps(data, ensure_ascii=False, default=str) if data is not None else "null"
        prompt = (
            "你之前返回的 JSON 不符合预期 schema，错误如下：\n"
            f"{errors_json}\n\n"
            "原始返回：\n"
            f"{raw_json}\n\n"
            "请修正并只输出符合 schema 的 JSON 对象，不要任何额外说明文字。"
        )
        return result, prompt


__all__ = [
    "ExtractedParams",
    "FieldError",
    "HookRecord",
    "SchemaValidator",
    "ValidationResult",
    "WebPageState",
]
