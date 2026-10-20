"""HTTP 响应磁盘缓存中间件（对标 Scrapy 的 ``HttpCacheMiddleware``）。

以 ``method + 规范化 URL`` 的 SHA-256 为缓存键，把 GET 响应落盘到
``cache_dir``：元数据（URL / 时间戳 / 状态码 / headers / encoding）存
``<key>.meta.json``，响应体经 zlib 压缩存 ``<key>.body``。写入走
"临时文件 + ``os.replace``" 原子替换；读取在 TTL 内直接重建
:class:`~web_crawler.response.Response` 短路下载。``offline`` 模式未命中
抛 :class:`~web_crawler.spider.IgnoreRequest`，实现零网络的离线回放。

线程模型：``threading.Lock`` 只保护内存计数器（hits / misses / stores），
所有磁盘 IO（读取 / 落盘 / 清理 / 遍历）都在锁外执行——Spider 的 worker
在事件循环线程内同步调用中间件，持锁做磁盘 IO 会阻塞整个事件循环（网络
盘 / 慢速磁盘 / 大 body 时单个操作可阻塞数百毫秒）；写入完整性由
"原子替换 + 先 body 后 meta 的落盘顺序"独立保证。读取侧带
``max_body_size`` 解压上限：投毒的 ``.body``（几十 KB 可解压出数 GB 的
zlib bomb）按损坏条目删除并视为未命中。

仅使用标准库，不引入任何新依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ..exceptions import ParseError
from ..response import Response
from .spider import DownloaderMiddleware, IgnoreRequest, Request, Spider

logger = logging.getLogger(__name__)

__all__ = ["CacheMiddleware"]

# 缓存文件命名：<sha256 十六进制>.meta.json / <sha256 十六进制>.body
_META_SUFFIX = ".meta.json"
_BODY_SUFFIX = ".body"
# purge 只删除匹配该模式的文件，避免误删用户放在缓存目录里的其他内容
_CACHE_FILE_RE = re.compile(r"^[0-9a-f]{64}\.(meta\.json|body)$")
# 元数据格式版本：未来格式变更时据此丢弃旧条目而不是读出垃圾数据
_META_VERSION = 1
# 分块解压的块大小（输入与单次输出上限）：解压时逐块喂入并限制单次输出，
# 把内存峰值钳制在 max_body_size + 一个块以内（zlib bomb 防护，见 _read_body）
_DECOMP_CHUNK = 64 * 1024
# os.replace 在 Windows 上并发替换同一目标时的瞬态 sharing violation
# 重试次数（每次退避 1ms 递增；替换冲突窗口远短于该间隔，见 _atomic_write）
_REPLACE_RETRIES = 5
# 同一目标的并发替换在 Windows 上本身就会互撞（目标被另一线程的替换窗口
# 短暂锁定）：用一把模块级锁只串行化 os.replace 这一步，写临时文件仍在锁外，
# 因此磁盘 IO 依旧不进临界区。它同时兜住「读线程短暂持有目标文件」的情形——
# 那种情况锁管不到，仍由上面的退避重试覆盖。
_REPLACE_LOCK = threading.Lock()


def _normalize_url(url: str) -> str:
    """规范化 URL 用于缓存键计算。

    规则：scheme 与 host 小写；query 以原始 ``key=value`` 串为单位排序
    （仅顺序不同视为同一 URL；不做解码/重编码，避免编码形态差异引入误判）；
    去掉 fragment 与 userinfo；显式默认端口（http:80 / https:443）被折叠。
    """
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        # 端口非法（如非数字）时无法归一，按无端口处理（键仍保持确定）
        port = None
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    pairs = [p for p in parts.query.split("&") if p] if parts.query else []
    return urlunparse((scheme, netloc, path, "", "&".join(sorted(pairs)), ""))


class _CachedResponse(Response):
    """从磁盘缓存重建的响应。

    引擎会把 ``process_request`` 短路返回的响应继续送入 ``process_response``
    链，本类型作为"来自缓存"的标记，让 :meth:`CacheMiddleware.process_response`
    跳过重复回写。
    """

    _from_cache = True


class CacheMiddleware(DownloaderMiddleware):
    """GET 响应磁盘缓存中间件（DownloaderMiddleware 协议）。

    - 仅缓存 GET：其他 method 直接透传（不读不写缓存）。
    - 磁盘存储：``cache_dir`` 下每个条目两个文件——``<sha256>.meta.json``
      （URL / 时间戳 / 状态码 / headers / encoding）与 ``<sha256>.body``
      （zlib 压缩的响应体）。写入先落临时文件再 ``os.replace`` 原子替换，
      并发读写不会见到半成品。
    - TTL：``ttl`` 秒后条目视为过期（未命中并被清理）；``None`` 表示永久。
    - ``process_request`` 命中且未过期时直接重建 Response 短路下载；
      ``offline=True`` 时未命中抛 :class:`IgnoreRequest`（绝不发网络请求，
      由引擎计入 ``requests_ignored``）。
    - ``process_response`` 在成功响应（2xx；``cache_4xx`` 时含 4xx）到达后
      写缓存并透传。
    - 缓存键 = ``method + 规范化 URL`` 的 SHA-256：query 顺序无关、忽略
      fragment 与 host/scheme 大小写、默认端口折叠。
    - ``ignore_lookup=True`` 跳过查找强制走网络，但下载结果仍回写缓存
      （缓存刷新模式）；与 ``offline`` 同时设置时 ``offline`` 优先生效。
    - 线程模型：``threading.Lock`` 只保护内存计数器；所有磁盘 IO 在锁外
      执行（事件循环内的 worker 不会被慢速磁盘卡住），写入完整性由
      "临时文件 + ``os.replace`` 原子替换 + 先 body 后 meta 的落盘顺序"
      独立保证，并发写同一 key 为 last-write-wins。

    缓存命中只恢复 ``url / status / headers / encoding / content``；
    ``meta``、``screenshots`` 等运行期字段不参与缓存。

    Parameters
    ----------
    cache_dir:
        缓存目录（不存在时在首次写入时创建），默认 ``.web_crawler_cache``。
    ttl:
        条目存活秒数；``None`` 表示永久缓存。
    offline:
        离线回放模式：命中直接返回、未命中抛 :class:`IgnoreRequest`。
    ignore_lookup:
        每次请求都发网络（跳过缓存查找），但响应仍回写缓存。
    cache_4xx:
        额外缓存 4xx 响应（默认仅 2xx；5xx 永不缓存）。
    max_body_size:
        缓存条目解压后的响应体大小上限（字节），默认 64MB（64 * 1024 * 1024）。
        64MB 的量级理由：Web 响应体绝大多数小于 10MB，64MB 已覆盖极端大
        页面，同时把单条目读取 / 解压的内存峰值限制在可接受范围。读取时
        压缩文件或解压输出超过该上限的条目按损坏处理（删除并视为未命中），
        防止投毒的 ``.body``（zlib bomb：几十 KB 压缩数据可解压出数 GB）
        单次命中即耗尽内存。

    Example
    -------
    常规缓存（1 小时 TTL）::

        class CachedSpider(Spider):
            start_urls = ["https://quotes.toscrape.com/"]
            middlewares = [CacheMiddleware(cache_dir=".web_crawler_cache", ttl=3600)]

            def parse(self, response):
                yield {"title": response.css_first("title").text}

    离线回放（同目录已有缓存时不发任何网络请求）::

        class ReplaySpider(Spider):
            middlewares = [CacheMiddleware(cache_dir=".web_crawler_cache", offline=True)]
    """

    def __init__(
        self,
        cache_dir: str | Path = ".web_crawler_cache",
        *,
        ttl: float | None = None,
        offline: bool = False,
        ignore_lookup: bool = False,
        cache_4xx: bool = False,
        max_body_size: int = 64 * 1024 * 1024,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl = ttl
        self.offline = offline
        self.ignore_lookup = ignore_lookup
        self.cache_4xx = cache_4xx
        self.max_body_size = max_body_size
        # Lock（而非 RLock）：磁盘 IO 已全部移出锁外，锁内只剩 _hits /
        # _misses / _stores 计数器的读改写——纯内存操作且不存在嵌套加锁
        # 的调用路径，普通 Lock 足够且开销更低
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._stores = 0

    # -- DownloaderMiddleware 协议 -----------------------------------------
    def process_request(self, request: Request, spider: Spider) -> Response | None:
        """GET 请求先查缓存：命中且未过期时重建 Response 短路下载。

        未命中（或非 GET / ``ignore_lookup``）返回 ``None`` 放行下载；
        ``offline=True`` 时未命中抛 :class:`IgnoreRequest`，由引擎计入
        ``requests_ignored``，保证离线模式下不发任何网络请求。
        """
        if request.method.upper() != "GET":
            return None
        # ignore_lookup 强制每次走网络（process_response 仍会回写缓存）；
        # offline 优先生效：离线模式下绝不能放行到网络
        if self.ignore_lookup and not self.offline:
            return None
        key = self.cache_key(request.method, request.url)
        # 磁盘读（含 TTL 过期 / 损坏条目的就地清理）在锁外执行：worker
        # 在事件循环线程内同步调用中间件，持锁做磁盘 IO 会阻塞整个事件
        # 循环（网络盘 / 慢速磁盘 / 大 body 时单个操作可阻塞数百毫秒）；
        # 锁内只剩计数器更新（int 的 ``+=`` 非原子，仍须锁保护）
        cached = self._load(key)
        if cached is not None:
            with self._lock:
                self._hits += 1
            logger.debug("cache hit: GET %s", request.url)
            return cached
        with self._lock:
            self._misses += 1
        if self.offline:
            raise IgnoreRequest(f"offline replay: no fresh cached response for GET {request.url}")
        return None

    def process_response(self, response: Response, request: Request, spider: Spider) -> Response:
        """成功响应（2xx；``cache_4xx`` 时含 4xx）写缓存后透传。"""
        if isinstance(response, _CachedResponse):
            # process_request 短路的缓存响应会被引擎再次送入本方法：
            # 标记为缓存来源，跳过回写避免重复落盘
            return response
        if request.method.upper() != "GET" or not self._should_store(response):
            return response
        key = self.cache_key(request.method, request.url)
        # 磁盘写在锁外：原子写（临时文件 + os.replace）自身不依赖锁，
        # "先 body 后 meta"的落盘顺序保证 meta 存在即 body 完整；并发写
        # 同一 key 仍是 last-write-wins，与移出锁前的语义一致
        self._store(key, request, response)
        with self._lock:
            self._stores += 1
        logger.debug("cache stored: GET %s (status %d)", request.url, response.status)
        return response

    # -- 公共管理接口 -------------------------------------------------------
    @staticmethod
    def cache_key(method: str, url: str) -> str:
        """计算缓存键：``method + 规范化 URL`` 的 SHA-256 十六进制摘要。"""
        h = hashlib.sha256()
        h.update(method.upper().encode("utf-8"))
        h.update(b"\x00")
        h.update(_normalize_url(url).encode("utf-8"))
        return h.hexdigest()

    def purge(self) -> None:
        """清空磁盘缓存（仅删除本中间件命名模式的文件）并重置统计。"""
        removed = 0
        # 文件遍历与删除在锁外（磁盘 IO 不进锁，理由见 process_request）
        if self.cache_dir.exists():
            for path in self.cache_dir.iterdir():
                if not _CACHE_FILE_RE.match(path.name):
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:  # pragma: no cover - Windows 文件占用等极端情况
                    logger.warning("failed to remove cache file %s", path)
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._stores = 0
        if removed:
            logger.info("cache purged: %d files removed from %s", removed, self.cache_dir)

    def stats(self) -> dict[str, int]:
        """返回命中统计：``hits`` / ``misses`` / ``stores`` 及磁盘条目数 ``entries``。

        ``hits`` / ``misses`` 只统计真正执行过缓存查找的 GET 请求（非 GET
        与 ``ignore_lookup`` 跳过查找时不计入）；``stores`` 为实际落盘次数。
        """
        # glob 磁盘遍历在锁外，锁内只取计数器快照（磁盘 IO 不进锁）
        entries = sum(1 for _ in self.cache_dir.glob(f"*{_META_SUFFIX}"))
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "stores": self._stores,
                "entries": entries,
            }

    # -- 存储内部 -----------------------------------------------------------
    def _should_store(self, response: Response) -> bool:
        """判断响应状态码是否值得缓存（默认仅 2xx，5xx 永不缓存）。"""
        if 200 <= response.status < 300:
            return True
        return bool(self.cache_4xx and 400 <= response.status < 500)

    def _meta_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}{_META_SUFFIX}"

    def _body_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}{_BODY_SUFFIX}"

    def _store(self, key: str, request: Request, response: Response) -> None:
        """将响应写入磁盘（先 body 后 meta：meta 存在即意味着 body 已落盘）。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "version": _META_VERSION,
            "url": response.url,
            "method": request.method.upper(),
            "status": int(response.status),
            # headers 统一转 str：JSON 只认基本类型
            "headers": {str(k): str(v) for k, v in response.headers.items()},
            "encoding": str(response.encoding),
            "stored_at": time.time(),
        }
        self._atomic_write(self._body_path(key), zlib.compress(response.content))
        self._atomic_write(
            self._meta_path(key), json.dumps(meta, ensure_ascii=False).encode("utf-8")
        )

    def _load(self, key: str) -> Response | None:
        """读取并重建缓存响应；未命中 / 过期 / 损坏返回 ``None``。

        过期与损坏（JSON 解析失败、body 缺失、版本不匹配、解压超限等）
        条目会被就地清理，避免后续请求反复读到坏数据。
        """
        meta_path = self._meta_path(key)
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("version") != _META_VERSION:
                raise ParseError(f"unsupported cache meta version: {meta.get('version')!r}")
            body = self._read_body(key)
            stored_at = float(meta["stored_at"])
            status = int(meta["status"])
            url = str(meta["url"])
        except (OSError, ValueError, TypeError, KeyError, zlib.error) as exc:
            # json.JSONDecodeError 是 ValueError 子类，已在覆盖范围内
            logger.warning("corrupt cache entry %s (%s): treating as miss", meta_path.name, exc)
            self._remove(key)
            return None
        if self.ttl is not None and (age := time.time() - stored_at) > self.ttl:
            logger.debug("cache expired for %s (age %.1fs > ttl %.1fs)", url, age, self.ttl)
            self._remove(key)
            return None
        headers = {str(k): str(v) for k, v in (meta.get("headers") or {}).items()}
        return _CachedResponse(
            url=url,
            status=status,
            content=body,
            headers=headers,
            encoding=str(meta.get("encoding", "utf-8")),
        )

    def _read_body(self, key: str) -> bytes:
        """读取并解压缓存 body，带解压输出上限（zlib bomb 防护）。

        两层上限，任一超出即抛 :class:`ValueError`（由 :meth:`_load` 按
        损坏条目清理）：

        1. 压缩文件 ``stat`` 超过 ``max_body_size``：异常巨量响应或投毒
           文件，读都不要读进内存；
        2. 分块解压输出累计超过 ``max_body_size``：zlib bomb 的典型形态
           （几十 KB 压缩数据可解压出数 GB）。用 ``zlib.decompressobj()``
           逐块喂入并限制单次输出（``max_length``），内存峰值被钳制在
           ``max_body_size + 一个块`` 以内，超限即刻中止。

        截断流 / 结束标记后仍有尾随数据时同样按损坏处理（与
        ``zlib.decompress`` 的严格语义保持一致）。
        """
        body_path = self._body_path(key)
        compressed_size = body_path.stat().st_size
        if compressed_size > self.max_body_size:
            raise ParseError(
                f"compressed body size {compressed_size} exceeds max_body_size {self.max_body_size}"
            )
        compressed = body_path.read_bytes()
        dec = zlib.decompressobj()
        out = bytearray()
        pos = 0
        total = len(compressed)
        while pos < total or dec.unconsumed_tail:
            if dec.unconsumed_tail:
                # 上一次输出被 max_length 截断：继续消费剩余输入
                chunk = dec.unconsumed_tail
            else:
                chunk = compressed[pos : pos + _DECOMP_CHUNK]
                pos += _DECOMP_CHUNK
            out += dec.decompress(chunk, _DECOMP_CHUNK)
            if len(out) > self.max_body_size:
                raise ParseError(
                    f"decompressed body size {len(out)} exceeds max_body_size {self.max_body_size}"
                )
        if not dec.eof or dec.unused_data:
            raise ParseError("truncated or padded zlib stream in cache body")
        return bytes(out)

    def _remove(self, key: str) -> None:
        """尽力删除一个条目的两个文件（best-effort，失败仅告警）。"""
        for path in (self._meta_path(key), self._body_path(key)):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - Windows 文件占用等极端情况
                logger.warning("failed to remove cache file %s", path)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """原子写入：同目录临时文件 + ``os.replace``（Windows/Linux 均原子）。

        临时文件名附加 pid 与线程 id：同 key 并发写互不踩踏，各自原子替换，
        最终内容为最后完成者（last-write-wins）。Windows 上并发替换同一
        目标可能得到瞬态 ``PermissionError``（目标正被另一线程的替换窗口
        短暂锁定），做有限次递增退避重试，仍失败才上抛。

        失败路径（``write_bytes`` 出错或替换重试穷尽）best-effort 清理残留
        的 ``.tmp`` 临时文件再上抛——``purge()`` 的 ``_CACHE_FILE_RE`` 不匹
        配 ``.tmp`` 后缀，无人清理会在长期运行的反复写失败场景下累积垃圾
        文件。替换成功路径 tmp 已被原子 rename 走，无需清理；重试退避等待
        期间同样不清（替换尚未最终失败）。
        """
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(data)
            for attempt in range(_REPLACE_RETRIES):
                try:
                    with _REPLACE_LOCK:
                        os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    # 瞬态 sharing violation：退避后重试同一 tmp（替换未发生，
                    # tmp 仍在）。毫秒级等待远小于磁盘 IO 本身的阻塞量级
                    time.sleep(0.001 * (attempt + 1))
        except Exception:
            # 异常路径清理（best-effort：清理失败不掩盖原异常）。PermissionError
            # 重试期间不进入此分支——只有最终上抛（write 失败 / 重试穷尽）才清理
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 极端情况下 unlink 自身失败
                pass
            raise
