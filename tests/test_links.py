"""LinkExtractor / Link 的测试。

覆盖：基础提取（href/text/urljoin）、非 a 标签排除、allow/deny 正则
过滤（deny 优先）、allow_domains/deny_domains 后缀匹配（子域命中）、
unique 去重保序、canonicalize（查询参数排序 / 去 fragment）、strip 空白
折叠、attrs 附带属性、空白名单全放行、Response 与 str html 输入等价、
非法正则抛 ConfigError、http/https scheme 白名单（伪协议拒绝）、
空白文档短路、无 host URL 域过滤拒绝、畸形 URL（urlparse ValueError）。
"""

from __future__ import annotations

import pytest

from web_crawler import Link, LinkExtractor, Response
from web_crawler.exceptions import ConfigError
from web_crawler.parser.links import _hostname

BASE = "https://shop.example.com"


def _resp(html: str, url: str = f"{BASE}/list") -> Response:
    """按 utf-8 编码构造 200 Response。"""
    return Response(url, 200, html.encode("utf-8"))


def test_extracts_href_and_text() -> None:
    """提取 a 标签的 href 与文本（文档顺序）。"""
    html = '<html><body><a href="/a">Alpha</a><a href="/b">Beta</a></body></html>'
    links = LinkExtractor().extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/a", f"{BASE}/b"]
    assert [link.text for link in links] == ["Alpha", "Beta"]
    assert all(isinstance(link, Link) for link in links)


def test_relative_links_resolved_against_base() -> None:
    """相对链接经 urljoin 绝对化；绝对链接原样保留。

    RFC 3986 语义：基准 ``/list`` 是文件路径，相对 ``page2`` 替换其
    末段得到 ``/page2``（而非拼接成 ``/list/page2``）。
    """
    html = (
        "<html><body>"
        '<a href="page2">rel</a>'
        '<a href="/root">abs path</a>'
        '<a href="https://other.example/x">abs url</a>'
        "</body></html>"
    )
    links = LinkExtractor().extract(html, base_url=f"{BASE}/list")
    assert [link.url for link in links] == [
        f"{BASE}/page2",
        f"{BASE}/root",
        "https://other.example/x",
    ]


def test_ignores_anchors_without_href_and_non_anchor_tags() -> None:
    """无 href 的 a 标签与 link/img 等非 a 标签都不产出链接。"""
    html = (
        "<html><head>"
        '<link rel="stylesheet" href="/style.css">'
        "</head><body>"
        '<a name="anchor">no href</a>'
        '<img src="/pic.png" alt="pic">'
        '<a href="/real">real</a>'
        "</body></html>"
    )
    links = LinkExtractor().extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/real"]


def test_allow_regex_whitelist_filters_urls() -> None:
    """allow 正则白名单：只保留命中（re.search 语义）的 URL。"""
    html = (
        '<html><body><a href="/page/1">1</a>'
        '<a href="/page/2">2</a><a href="/about">about</a></body></html>'
    )
    links = LinkExtractor(allow=(r"/page/\d+",)).extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/page/1", f"{BASE}/page/2"]


def test_deny_overrides_allow() -> None:
    """deny 命中优先级最高：白名单内的链接也被拒。"""
    html = '<html><body><a href="/page/1">1</a><a href="/page/2">2</a></body></html>'
    links = LinkExtractor(allow=(r"/page/\d+",), deny=(r"2",)).extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/page/1"]


def test_allow_domains_suffix_matching() -> None:
    """allow_domains 后缀匹配：子域命中，无关域被拒。"""
    html = (
        '<html><body><a href="https://example.com/x">root</a>'
        '<a href="https://sub.example.com/y">sub</a>'
        '<a href="https://other.org/z">other</a></body></html>'
    )
    links = LinkExtractor(allow_domains=("example.com",)).extract(html, base_url=BASE)
    assert [link.url for link in links] == [
        "https://example.com/x",
        "https://sub.example.com/y",
    ]


def test_deny_domains_blocks_subdomains() -> None:
    """deny_domains 后缀匹配：命中域及其子域都被拒。"""
    html = (
        '<html><body><a href="https://bad.example/x">bad</a>'
        '<a href="https://sub.bad.example/y">sub bad</a>'
        '<a href="https://good.example/z">good</a></body></html>'
    )
    links = LinkExtractor(deny_domains=("bad.example",)).extract(html, base_url=BASE)
    assert [link.url for link in links] == ["https://good.example/z"]


def test_unique_dedupes_canonicalized_urls_preserving_order() -> None:
    """unique 去重按规范化 URL 判定（fragment 差异折叠），保留首现顺序。"""
    html = (
        '<html><body><a href="/a#x">1</a>'
        '<a href="/b">2</a><a href="/a#y">3</a><a href="/a">4</a></body></html>'
    )
    links = LinkExtractor().extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/a", f"{BASE}/b"]
    # 保留的是首次出现的 Link（其文本来自首个匹配的 a 标签）
    assert links[0].text == "1"


def test_canonicalize_sorts_query_and_drops_fragment() -> None:
    """canonicalize 对查询参数排序并去 fragment。"""
    html = '<html><body><a href="/p?b=2&a=1#frag">P</a></body></html>'
    links = LinkExtractor().extract(html, base_url=BASE)
    assert links[0].url == f"{BASE}/p?a=1&b=2"


def test_canonicalize_false_keeps_url_verbatim() -> None:
    """canonicalize=False 时 URL 原样保留（含 fragment 与参数顺序）。"""
    html = '<html><body><a href="/p?b=2&a=1#frag">P</a></body></html>'
    links = LinkExtractor(canonicalize=False).extract(html, base_url=BASE)
    assert links[0].url == f"{BASE}/p?b=2&a=1#frag"


def test_unique_false_keeps_duplicates() -> None:
    """unique=False 时重复 URL 逐条保留。"""
    html = '<html><body><a href="/a">1</a><a href="/a">2</a></body></html>'
    links = LinkExtractor(unique=False).extract(html, base_url=BASE)
    assert [link.url for link in links] == [f"{BASE}/a", f"{BASE}/a"]
    assert [link.text for link in links] == ["1", "2"]


def test_strip_collapses_whitespace() -> None:
    """strip 默认折叠空白；strip=False 保留 itertext 原样拼接。"""
    html = '<html><body><a href="/x">  Hello   <b>World</b>  </a></body></html>'
    collapsed = LinkExtractor().extract(html, base_url=BASE)
    assert collapsed[0].text == "Hello World"
    raw = LinkExtractor(strip=False).extract(html, base_url=BASE)
    assert raw[0].text == "  Hello   World  "


def test_attrs_carry_extra_attributes() -> None:
    """attrs 携带 href 之外的原生属性（title/class 等）。"""
    html = '<html><body><a href="/x" title="Tip" class="btn">go</a></body></html>'
    links = LinkExtractor().extract(html, base_url=BASE)
    assert links[0].attrs == {"title": "Tip", "class": "btn"}


def test_empty_filters_pass_everything() -> None:
    """空白名单（默认构造）全放行：跨域、任意路径都保留。"""
    html = '<html><body><a href="/x">x</a><a href="https://anywhere.org/y">y</a></body></html>'
    links = LinkExtractor().extract(html, base_url=BASE)
    assert len(links) == 2


def test_response_input_equivalent_to_html_input() -> None:
    """Response 输入与 str html + base_url 输入产出等价（复用其解析树）。"""
    html = (
        '<html><body><a href="/page/1?b=2&a=1">1</a>'
        '<a href="https://other.org/z">z</a></body></html>'
    )
    from_html = LinkExtractor().extract(html, base_url=f"{BASE}/list")
    from_response = LinkExtractor().extract(_resp(html))
    assert from_response == from_html
    # Response 输入以自身 url 为基准解析相对链接
    assert from_response[0].url == f"{BASE}/page/1?a=1&b=2"


def test_response_with_meta_charset_extracts_unicode_text() -> None:
    """带 meta charset 的 Response 输入正确提取中文文本。"""
    html = '<html><head><meta charset="utf-8"></head><body><a href="/x">下一页</a></body></html>'
    links = LinkExtractor().extract(_resp(html))
    assert links[0].text == "下一页"


def test_invalid_regex_raises_config_error() -> None:
    """非法正则串在构造期抛 ConfigError（allow 与 deny 同样校验）。"""
    with pytest.raises(ConfigError):
        LinkExtractor(allow=("(",))
    with pytest.raises(ConfigError):
        LinkExtractor(deny=("[z-a",))


# ===========================================================================
# scheme 白名单（M-4）：只产 http/https 链接
# ===========================================================================


def test_pseudo_scheme_urls_rejected_by_default() -> None:
    """默认配置拒绝 javascript:/data:/mailto:/tel: 伪协议 URL。"""
    html = (
        "<html><body>"
        '<a href="javascript:void(0)">js</a>'
        '<a href="data:text/html,x">data</a>'
        '<a href="mailto:a@b.com">mail</a>'
        '<a href="tel:12345678">tel</a>'
        '<a href="https://x.com/ok">https</a>'
        '<a href="/rel">relative</a>'
        "</body></html>"
    )
    links = LinkExtractor().extract(html, base_url=BASE)
    # 伪协议全拒；https 与经 urljoin 绝对化的相对链接通过
    assert [link.url for link in links] == ["https://x.com/ok", f"{BASE}/rel"]


def test_http_relative_link_resolved_and_allowed() -> None:
    """http 基准下相对链接经 urljoin 绝对化后通过 scheme 校验。"""
    html = '<html><body><a href="page2">next</a></body></html>'
    links = LinkExtractor().extract(html, base_url=f"{BASE}/list")
    assert [link.url for link in links] == [f"{BASE}/page2"]


def test_pseudo_scheme_allow_regex_cannot_bypass_scheme_check() -> None:
    """allow 正则无法豁免伪协议：scheme 校验先于正则执行。"""
    html = '<html><body><a href="javascript:void(0)">js</a></body></html>'
    links = LinkExtractor(allow=(r"javascript:.*",)).extract(html, base_url=BASE)
    assert links == []


def test_relative_url_without_base_kept_as_is() -> None:
    """无 base_url 的相对链接按原样保留（空 scheme 放行交下游处理）。"""
    html = '<html><body><a href="/rel">rel</a></body></html>'
    links = LinkExtractor().extract(html)
    assert [link.url for link in links] == ["/rel"]


# ===========================================================================
# 边界输入与畸形 URL
# ===========================================================================


def test_blank_document_returns_empty_list() -> None:
    """空白文档（纯空白 / 空串）短路返回 []，不进 lxml 解析。"""
    assert LinkExtractor().extract("   \n\t  ") == []
    assert LinkExtractor().extract("") == []


def test_no_host_url_with_allow_domains_rejected() -> None:
    """无 host 的 URL（无 base 的相对路径）+ allow_domains：拒绝。"""
    html = '<html><body><a href="/rel">rel</a></body></html>'
    links = LinkExtractor(allow_domains=("example.com",)).extract(html)
    assert links == []


def test_malformed_url_rejected_gracefully() -> None:
    """畸形 URL（无闭合括号的 IPv6 host）不炸提取：scheme 校验拒绝。"""
    html = '<html><body><a href="http://[invalid">bad</a></body></html>'
    # 无 base_url（不经 urljoin）+ canonicalize=False：直达 _url_allowed
    links = LinkExtractor(canonicalize=False).extract(html)
    assert links == []


def test_hostname_of_invalid_ipv6_url_returns_empty() -> None:
    """_hostname 对 urlparse 抛 ValueError 的畸形 URL 返回空串（不炸）。"""
    assert _hostname("http://[invalid") == ""
