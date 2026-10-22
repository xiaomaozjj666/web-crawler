"""ValidationPipeline（item 类型化校验管道）的测试。

覆盖 dict schema 路径（必填缺失 / 类型不符 / 默认值填充 / 附加键透传 /
空 schema 放行 / 非 dict item / 构造期快失败）、pydantic 模型路径
（model_dump 规范化 / 校验失败 DropItem / 未安装降级 ConfigError）、
非法 schema 的 ConfigError、端到端 Spider 集成（坏 item 丢弃并汇入
``items_dropped`` 计数）与懒加载红线（``import web_crawler`` 及
``import web_crawler.spider`` 均不拉起 pydantic——validation.py 顶层
零 pydantic 引用，构造期才延迟导入）。

mock fetcher 构造 Spider 的方式对齐 test_feed.py：内存 FakeFetcher
返回按 URL 预置的 HTML，无网络。
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest
from pydantic import BaseModel

from web_crawler import Response, Spider, ValidationPipeline
from web_crawler.exceptions import ConfigError
from web_crawler.spider.spider import DropItem

HOST = "shop.example.com"
BASE = f"https://{HOST}"


class FakeFetcher:
    """返回按 URL 预置的 HTML，无网络；记录调用顺序。"""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(url)
        content = self.pages.get(url, b"<html><body></body></html>")
        status = 200 if url in self.pages else 404
        return Response(url, status, content)

    async def async_get(self, url: str, **kwargs: Any) -> Response:
        return self.get(url, **kwargs)


class ProductModel(BaseModel):
    """商品 item 的 pydantic 模型（pydantic 路径的校验目标）。"""

    name: str
    price: float = 0.0


class PlainClass:
    """非 BaseModel 的普通类（非法 schema 用例）。"""


# ===========================================================================
# dict schema：必填 / 类型 / 默认值 / 附加键
# ===========================================================================


def test_dict_schema_missing_required_drops_with_field_name() -> None:
    """必填字段缺失抛 DropItem：消息含字段名与缺失原因（多字段错误聚合）。"""
    pipe = ValidationPipeline({"name": {"type": str}, "tags": {}})
    with pytest.raises(DropItem, match="name.*缺失必填字段") as exc_info:
        pipe.process_item({"other": 1}, Spider())
    # 第二个必填字段（无类型约束的 tags）的错误聚合在同一条消息里
    assert "tags" in str(exc_info.value)


def test_dict_schema_type_mismatch_drops_with_expected_type() -> None:
    """类型不符抛 DropItem（消息含期望与实际类型）；元组类型任一成员可过。"""
    pipe = ValidationPipeline({"name": {"type": str}, "price": {"type": (int, float)}})
    # 元组类型：int / float 成员均通过
    assert pipe.process_item({"name": "n", "price": 9}, Spider())["price"] == 9
    assert pipe.process_item({"name": "n", "price": 9.5}, Spider())["price"] == 9.5
    # 单一类型不符 + 元组全不符：两个字段错误聚合在一条消息里
    with pytest.raises(
        DropItem, match=r"name: 期望类型 str, 实际 int; price: 期望类型 int\|float, 实际 str"
    ):
        pipe.process_item({"name": 42, "price": "x"}, Spider())


def test_dict_schema_optional_default_filled() -> None:
    """required=False 缺失时补 default；无 default 则保持缺失；原 item 不被就地修改。"""
    pipe = ValidationPipeline(
        {
            "price": {"type": (int, float), "required": False, "default": 0.0},
            "note": {"required": False},
        }
    )
    item = {"name": "x"}
    out = pipe.process_item(item, Spider())
    # price 补默认值；note 无 default：保持缺失（不补占位值）
    assert out == {"name": "x", "price": 0.0}
    assert item == {"name": "x"}  # 原 item 不被就地修改（浅拷贝上补默认值）


def test_dict_schema_mutable_default_not_shared_between_items() -> None:
    """M-5 回归：可变 default（list/dict/set）深拷贝——独立对象、修改互不影响。"""
    schema = {
        "tags": {"required": False, "default": []},
        "attrs": {"required": False, "default": {"k": 1}},
        "ids": {"required": False, "default": {1, 2}},
    }
    pipe = ValidationPipeline(schema)
    out1 = pipe.process_item({"name": "a"}, Spider())
    out2 = pipe.process_item({"name": "b"}, Spider())
    for field in ("tags", "attrs", "ids"):
        # 两个 item 的默认值不是同一对象，也不是 schema 声明里的本体
        # （共享引用会让原地修改互相污染）
        assert out1[field] is not out2[field]
        assert out1[field] is not schema[field]["default"]
        # 原地修改 out1 不影响 out2 与 schema 本体
        value: Any = out1[field]
        if isinstance(value, list):
            value.append("mutated")
        elif isinstance(value, dict):
            value["mutated"] = True
        else:
            value.add("mutated")
        assert "mutated" not in out2[field]
        assert "mutated" not in schema[field]["default"]


def test_dict_schema_int_type_accepts_bool_isinstance_semantics() -> None:
    """L-1 固化：dict schema 的 {"type": int} 放行 bool（isinstance 语义）。

    bool 是 int 的子类，与 pydantic 路径（严格类型，int 拒收 bool）
    行为不同——固化现状并已在类 docstring 写明，进阶需求用 pydantic。
    """
    pipe = ValidationPipeline({"flag": {"type": int}})
    assert pipe.process_item({"flag": True}, Spider())["flag"] is True
    assert pipe.process_item({"flag": False}, Spider())["flag"] is False


def test_dict_schema_extra_keys_passed_through() -> None:
    """附加键（schema 之外的键）宽松保留透传（docstring 写明的语义）。"""
    pipe = ValidationPipeline({"name": {"type": str}})
    out = pipe.process_item({"name": "n", "extra": 1, "tags": ["a"]}, Spider())
    assert out == {"name": "n", "extra": 1, "tags": ["a"]}


def test_dict_schema_empty_passes_any_item() -> None:
    """空 schema 无约束：dict 与非 dict item 一律原样放行。"""
    pipe = ValidationPipeline({})
    assert pipe.process_item({"any": "thing"}, Spider()) == {"any": "thing"}
    assert pipe.process_item("raw-string", Spider()) == "raw-string"


def test_dict_schema_non_dict_item_drops() -> None:
    """非空 schema 遇非 dict item 抛 DropItem（消息含实际类型名）。"""
    pipe = ValidationPipeline({"name": {}})
    with pytest.raises(DropItem, match="实际 list"):
        pipe.process_item(["not", "a", "dict"], Spider())


def test_dict_schema_invalid_constraints_raise_config_error() -> None:
    """字段约束非法（非 Mapping / type 非类型 / required 非 bool）构造期 ConfigError。"""
    with pytest.raises(ConfigError, match="must be a field-constraint mapping"):
        ValidationPipeline({"x": "not-a-mapping"})
    with pytest.raises(ConfigError, match=r"schema\['price'\]\['type'\]"):
        ValidationPipeline({"price": {"type": "not-a-type"}})
    with pytest.raises(ConfigError, match=r"schema\['age'\]\['required'\]"):
        ValidationPipeline({"age": {"required": "yes"}})


# ===========================================================================
# pydantic 模型路径（本 venv 已安装 pydantic）
# ===========================================================================


def test_pydantic_model_validates_and_normalizes() -> None:
    """BaseModel 路径：校验通过后以 model_dump 规范化透传。

    覆盖默认值填充、str→float 类型强制与 extra 默认忽略（schema 之外
    的键不出现在 model_dump 结果里）。
    """
    pipe = ValidationPipeline(ProductModel)
    out = pipe.process_item({"name": "widget"}, Spider())
    assert out == {"name": "widget", "price": 0.0}  # 默认值填充
    out = pipe.process_item({"name": "widget", "price": "9.5", "extra": "ignored"}, Spider())
    assert out == {"name": "widget", "price": 9.5}  # 类型强制 + extra 忽略
    assert isinstance(out["price"], float)


def test_pydantic_model_failure_drops_with_field_name() -> None:
    """BaseModel 校验失败抛 DropItem：消息含字段名与 pydantic 的具体原因。"""
    pipe = ValidationPipeline(ProductModel)
    with pytest.raises(DropItem, match="name.*Field required"):
        pipe.process_item({"price": 3}, Spider())
    with pytest.raises(DropItem, match="price"):
        pipe.process_item({"name": "n", "price": "not-a-number"}, Spider())


def test_pydantic_missing_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic 未安装（sys.modules 置 None 模拟 ImportError）时构造抛 ConfigError。"""
    monkeypatch.setitem(sys.modules, "pydantic", None)
    with pytest.raises(ConfigError, match="pydantic"):
        ValidationPipeline(ProductModel)


# ===========================================================================
# 非法 schema（构造期快失败）
# ===========================================================================


@pytest.mark.parametrize("bad", [123, "name", ["name"], None, int, PlainClass])
def test_invalid_schema_raises_config_error(bad: Any) -> None:
    """既非 BaseModel 子类也非字段约束 dict 的 schema 构造期抛 ConfigError。"""
    with pytest.raises(ConfigError, match="unsupported schema"):
        ValidationPipeline(bad)


# ===========================================================================
# 端到端：Spider + ValidationPipeline（丢弃计数汇入 stats）
# ===========================================================================


class ShopSpider(Spider):
    """单页产出三条 item（一条完好、两条校验不过）的端到端 spider。"""

    name = "validation_e2e"
    start_urls = [f"{BASE}/"]

    def parse(self, response: Response) -> Any:
        yield {"name": "good", "price": 10, "extra": "keep"}
        yield {"name": "no-price"}
        yield {"name": 123, "price": 5}


def test_end_to_end_spider_drops_bad_items_with_stats() -> None:
    """端到端：坏 item 被 DropItem 丢弃（不进结果）、好 item 通过且附加键保留；
    丢弃计数汇入 stats.items_dropped（与 items_scraped 互补）。"""
    schema = {"name": {"type": str}, "price": {"type": (int, float)}}

    class S(ShopSpider):
        item_pipelines = [ValidationPipeline(schema)]

    spider = S(fetcher=FakeFetcher({f"{BASE}/": b"<html><body></body></html>"}))
    items = spider.run()
    # 好 item 通过且附加键保留；缺 price 与 name 类型不符的两条被丢弃
    assert items == [{"name": "good", "price": 10, "extra": "keep"}]
    assert spider.stats.items_scraped == 1
    assert spider.stats.items_dropped == 2


# ===========================================================================
# 懒加载红线：import web_crawler / import web_crawler.spider 不拉起 pydantic
# ===========================================================================


def test_import_web_crawler_does_not_load_pydantic() -> None:
    """懒加载红线：import web_crawler 与 import web_crawler.spider 均不拉起
    pydantic（validation.py 顶层零 pydantic 引用，构造期才延迟导入）。"""
    # 保存 & 恢复 sys.modules，模式对齐 test_lazy_import.py
    saved = dict(sys.modules)
    for mod in list(sys.modules):
        if mod.startswith(("web_crawler", "pydantic.")) or mod == "pydantic":
            del sys.modules[mod]
    try:
        importlib.import_module("web_crawler")
        assert "pydantic" not in sys.modules, "import web_crawler loaded pydantic"
        # validation 挂在 spider 包的 eager 导入链上（from web_crawler import
        # Spider 的必经路径）：导入 spider 包同样不得拉起 pydantic
        importlib.import_module("web_crawler.spider")
        assert "pydantic" not in sys.modules, "import web_crawler.spider loaded pydantic"
    finally:
        sys.modules.clear()
        sys.modules.update(saved)
