"""item 类型化校验管道（0.7.0 backlog：对标 Scrapy Item 校验的现代答案）。

pydantic 是可选依赖，但本模块挂在 ``spider`` 包的 eager 导入链上
（``Spider`` 是最热符号），不能照抄 ai/schema.py 的模块顶层 try/except
软依赖模式，而是采用**构造期延迟导入 + 实例缓存**：

- 模块顶层零 pydantic 引用——``import web_crawler`` /
  ``import web_crawler.spider`` 绝不拉起 pydantic（懒加载红线）；
- 仅当用户以 ``BaseModel`` 子类构造 :class:`ValidationPipeline` 时才
  现场导入一次并缓存到实例属性，``process_item`` 热路径零导入开销；
- 每次构造现场探测（不做模块级 ``_HAS_PYDANTIC`` 缓存）——「装 /
  未装」两种环境行为确定，不受同进程先前探测结果的顺序污染。

dict schema 路径完全无第三方依赖：轻量字段约束（类型 / 必填 / 默认值），
刻意不做嵌套、正则与自定义校验器（YAGNI），进阶需求请改用 pydantic
模型路径。校验失败抛 :class:`DropItem`，由 Spider 执行链统一丢弃并计入
``stats.items_dropped``（与 ``items_scraped`` 互补）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..exceptions import ConfigError
from .spider import DropItem, ItemPipeline, Spider

if TYPE_CHECKING:
    # 仅供类型检查器解析签名；运行期绝不导入（懒加载红线）
    from pydantic import BaseModel, ValidationError

__all__ = ["ValidationPipeline"]

# 「字段约束未提供 default」的哨兵：区分 default=None（合法默认值）与未提供
_MISSING: Any = object()


@dataclass
class _FieldSpec:
    """dict schema 单字段的规范化约束（构造期解析完成，校验热路径零解析）。"""

    # 期望类型或类型元组；None = 不约束类型（仅存在性 / 默认值语义）
    expected: type | tuple[type, ...] | None
    required: bool
    # _MISSING = 未提供 default（required=False 且缺失时该字段保持缺失）
    default: Any = _MISSING


def _type_name(expected: type | tuple[type, ...]) -> str:
    """期望类型的人类可读名（元组按 ``int|float`` 风格拼接）。"""
    if isinstance(expected, tuple):
        return "|".join(t.__name__ for t in expected)
    return expected.__name__


def _is_type_spec(value: Any) -> bool:
    """值是否为合法的类型约束（单个 type 或非空 type 元组）。"""
    if isinstance(value, type):
        return True
    return isinstance(value, tuple) and bool(value) and all(isinstance(t, type) for t in value)


def _import_pydantic() -> tuple[type[BaseModel], type[ValidationError]]:
    """延迟导入 pydantic 的 BaseModel / ValidationError（构造期调用一次）。

    未安装时抛 :class:`ConfigError`——提示安装 ``web-crawler[all]`` 或
    改用零依赖的 dict schema，对齐 ai/schema.py 的软依赖降级语义。
    """
    try:
        from pydantic import BaseModel, ValidationError
    except ImportError as exc:
        raise ConfigError(
            "pydantic is not installed: install web-crawler[all] (or pydantic) "
            "to validate against a BaseModel schema, "
            "or pass a field-constraint dict instead"
        ) from exc
    return BaseModel, ValidationError


def _parse_dict_schema(schema: dict[str, Any]) -> dict[str, _FieldSpec]:
    """把声明式 dict schema 解析为逐字段的规范化约束（构造期快失败）。

    非法形态（字段约束不是 Mapping、``type`` 不是类型或类型元组、
    ``required`` 不是 bool）立即抛 :class:`ConfigError` 并指明字段名；
    未知的约束键宽松忽略（与整体轻量定位一致）。
    """
    fields: dict[str, _FieldSpec] = {}
    for name, constraints in schema.items():
        if not isinstance(constraints, Mapping):
            raise ConfigError(
                f"schema[{name!r}] must be a field-constraint mapping "
                f"(type/required/default), got {type(constraints).__name__}"
            )
        expected = constraints.get("type")
        if expected is not None and not _is_type_spec(expected):
            raise ConfigError(
                f"schema[{name!r}]['type'] must be a type or tuple of types, got {expected!r}"
            )
        required = constraints.get("required", True)
        if not isinstance(required, bool):
            raise ConfigError(f"schema[{name!r}]['required'] must be a bool, got {required!r}")
        fields[name] = _FieldSpec(
            expected=expected, required=required, default=constraints.get("default", _MISSING)
        )
    return fields


class ValidationPipeline(ItemPipeline):
    """item 结构校验管道：pydantic 模型（装了才可用）或无依赖的字典
    schema 校验，失败抛 :class:`DropItem` 并计入运行统计。

    与 Scrapy 的 Item 类不同，本管道采用「校验器」而非「容器」——
    item 保持裸 dict，校验声明与数据流转解耦（现代 pydantic 答案）。

    用法一（dict schema，零第三方依赖）::

        class ProductSpider(Spider):
            item_pipelines = [ValidationPipeline({
                "name": {"type": str},
                "price": {"type": (int, float), "required": False, "default": 0.0},
            })]

    用法二（pydantic ``BaseModel`` 子类，需安装 pydantic；规范化能力更
    强——默认值填充 / 类型强制 / 嵌套模型 / extra 策略）::

        class ProductItem(BaseModel):
            name: str
            price: float = 0.0

        item_pipelines = [ValidationPipeline(ProductItem)]

    dict schema 的字段约束 DSL（刻意极简）：

    - ``type`` — 期望类型或类型元组（如 ``(int, float)``）；省略则不
      校验类型（仅存在性 / 默认值语义）
    - ``required`` — 是否必填；默认 ``True``
    - ``default`` — ``required=False`` 且字段缺失时补的默认值；省略则
      该字段保持缺失

    语义要点：

    - 校验失败抛 ``DropItem("item 校验失败: <字段与原因>")``，消息聚合
      全部字段错误（缺失必填字段 / 期望类型 X 实际 Y）；被丢弃的 item
      不计入 ``items_scraped``、不进运行结果，计入
      ``stats.items_dropped``（Spider 执行链统一计数）。
    - 附加键（schema 之外的键）：dict 路径宽松保留透传；pydantic 路径
      按模型 ``model_config`` 的 ``extra`` 策略处理（默认忽略，不出现在
      ``model_dump`` 结果里）。
    - dict 路径校验通过时返回补默认值后的浅拷贝（不就地修改回调产出
      的原 item）；pydantic 路径返回 ``model_dump()`` 的规范化结果。
    - 嵌套结构 / 正则 / 自定义校验器不在 dict DSL 范围（YAGNI）——
      进阶校验需求请改用 pydantic 模型路径。
    - ``BaseModel`` schema 在 pydantic 未安装时构造抛
      :class:`~web_crawler.exceptions.ConfigError`（提示安装
      ``web-crawler[all]`` 或改用 dict schema）；dict schema 无任何
      第三方依赖。
    """

    def __init__(self, schema: type | dict[str, Any]) -> None:
        """以 pydantic ``BaseModel`` 子类或字段约束 dict 构造校验管道。

        Parameters
        ----------
        schema:
            pydantic ``BaseModel`` 子类（校验 + 规范化，需安装 pydantic），
            或 ``{字段名: {type/required/default 约束}}`` 形态的 dict（零
            依赖轻量校验）。约束 DSL 与语义详见类 docstring。非法形态
            （既非 ``BaseModel`` 子类也非 dict 的 schema、非 Mapping 的
            字段约束、非法 ``type`` / ``required`` 值）在构造期抛
            :class:`~web_crawler.exceptions.ConfigError`（快失败）。
        """
        self._model: type[BaseModel] | None
        self._validation_error: type[ValidationError] | None
        if isinstance(schema, dict):
            self._model = None
            self._validation_error = None
            self._fields: dict[str, _FieldSpec] = _parse_dict_schema(schema)
        elif isinstance(schema, type):
            base, validation_error = _import_pydantic()
            if not issubclass(schema, base):
                raise ConfigError(
                    f"unsupported schema class {schema.__name__!r}: "
                    "expected a pydantic BaseModel subclass or a field-constraint dict"
                )
            self._model = schema
            self._validation_error = validation_error
            self._fields = {}
        else:
            raise ConfigError(
                f"unsupported schema {schema!r}: expected a pydantic BaseModel "
                "subclass or a field-constraint dict"
            )

    def process_item(self, item: Any, spider: Spider) -> Any:
        """校验（并规范化）item；失败抛 :class:`DropItem`（计入 items_dropped）。"""
        if self._model is not None:
            return self._validate_model(item)
        return self._validate_fields(item)

    # ------------------------------------------------------------------
    # dict schema 路径（零第三方依赖）
    # ------------------------------------------------------------------

    def _validate_fields(self, item: Any) -> Any:
        """dict schema 校验：失败聚合全部字段错误后一次性抛 DropItem。"""
        if not isinstance(item, Mapping):
            # 空 schema = 无约束：任意 item（含非 dict）原样放行
            if not self._fields:
                return item
            raise DropItem(f"item 校验失败: item 应为 dict, 实际 {type(item).__name__}")
        errors: list[str] = []
        # 浅拷贝上补默认值：不就地修改回调产出的原 item
        result: dict[str, Any] = dict(item)
        for name, spec in self._fields.items():
            if name not in result:
                if spec.required:
                    errors.append(f"{name}: 缺失必填字段")
                elif spec.default is not _MISSING:
                    result[name] = spec.default
            elif spec.expected is not None and not isinstance(result[name], spec.expected):
                errors.append(
                    f"{name}: 期望类型 {_type_name(spec.expected)}, "
                    f"实际 {type(result[name]).__name__}"
                )
        if errors:
            raise DropItem("item 校验失败: " + "; ".join(errors))
        return result

    # ------------------------------------------------------------------
    # pydantic 模型路径（构造期已缓存模型与 ValidationError 类）
    # ------------------------------------------------------------------

    def _validate_model(self, item: Any) -> Any:
        """BaseModel 校验：通过则 ``model_dump()`` 规范化透传，失败抛 DropItem。"""
        assert self._model is not None and self._validation_error is not None
        try:
            model = self._model.model_validate(item)
        except self._validation_error as exc:
            # 逐条格式化为「字段路径: 原因」，与 dict 路径的消息风格一致
            details = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}"
                for e in exc.errors()
            )
            raise DropItem(f"item 校验失败: {details}") from exc
        # model_dump 完成规范化（默认值填充 / 类型强制）；附加键按模型
        # model_config 的 extra 策略处理（默认忽略）
        return model.model_dump()
