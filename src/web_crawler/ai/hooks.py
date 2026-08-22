"""浏览器侧 JS Hook 脚本库，用于捕获前端生成的加密请求参数。

本模块生成需要通过 ``page.add_init_script`` 在页面加载前注入的 JavaScript
代码片段。注入后，Hook 会拦截 ``fetch`` / ``XMLHttpRequest`` / ``document.cookie``
/ ``crypto.subtle`` / webpack 加载器 / ``console`` 等关键路径，把请求 url、
method、headers、body 等信息统一写入 ``window.__hook_data__`` 数组，便于后续
通过 ``page.evaluate`` 读取分析。

典型用途是定位站点在前端动态生成的签名头（如 ``Anti-Content``、``X-Bogus``、
``a-bogus``、``_signature`` 等），这些参数往往由 JS 在请求发起前计算并塞入
headers，单纯抓包无法看到生成过程，必须在浏览器内拦截才能拿到原始输入。

设计要点
--------
- 每个 Hook 脚本都是自包含的 IIFE，不依赖外部库，可独立注入；
- 原始引用通过闭包变量保存，``XMLHttpRequest`` 等走原型链替换，避免使用
  ``Object.defineProperty`` 简单改写实例属性这种容易被检测的写法；
- ``document.cookie`` 因无原型链可走，只能基于 ``Document.prototype`` 上的
  原生描述符做包裹式替换，保留原始 get/set 引用以降低被检测概率；
- 所有 Hook 都有重入保护（``window.__hook_*__`` 标记），多次注入不会重复包裹；
- 数据格式统一为 ``{type, url, method, headers, body, timestamp, stack}``。

示例
----
>>> from web_crawler.ai.hooks import generate_combined_script, collect_hook_data
>>> js = generate_combined_script(["fetch_hook", "xhr_hook"])
>>> page.add_init_script(js)            # 在导航前注入
>>> page.goto("https://example.com")
>>> data = collect_hook_data(page)
>>> data["records"][0]["headers"]       # 拿到拦截到的请求头
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ---------------------------------------------------------------------------
# Hook 脚本常量
# 每个 JS 片段都是独立 IIFE，自包含 push helper 与 __hook_data__ 初始化，
# 因此可以单独注入也可以任意组合。
# ---------------------------------------------------------------------------

_FETCH_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_fetch__) return;
        window.__hook_fetch__ = true;
        const _fetch = window.fetch;
        if (typeof _fetch !== 'function') return;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        const _headers = (h) => {
            const out = {};
            try {
                if (!h) return out;
                if (typeof h.forEach === 'function') {
                    h.forEach((v, k) => { out[k] = v; });
                } else if (h instanceof Array) {
                    for (const pair of h) {
                        if (pair && pair.length >= 2) out[pair[0]] = pair[1];
                    }
                } else {
                    for (const k in h) {
                        if (Object.prototype.hasOwnProperty.call(h, k)) out[k] = h[k];
                    }
                }
            } catch (e) {}
            return out;
        };
        const _body = (b) => {
            try {
                if (b == null) return null;
                if (typeof b === 'string') return b;
                if (b instanceof URLSearchParams) return b.toString();
                if (b instanceof FormData) {
                    const o = {};
                    b.forEach((v, k) => { o[k] = v; });
                    return JSON.stringify(o);
                }
                if (b instanceof ArrayBuffer) return '[binary ArrayBuffer ' + b.byteLength + 'B]';
                if (b instanceof Blob) return '[Blob ' + b.type + ' ' + b.size + 'B]';
                try { return JSON.stringify(b); } catch (e) { return '[unserializable]'; }
            } catch (e) { return null; }
        };
        window.fetch = function(input, init) {
            try {
                const url = (typeof input === 'string')
                    ? input
                    : (input && input.url) || '';
                const method = (init && init.method)
                    || (input && input.method)
                    || 'GET';
                _push({
                    type: 'fetch',
                    url: url,
                    method: method,
                    headers: _headers((init && init.headers) || (input && input.headers)),
                    body: _body((init && init.body) || (input && input.body)),
                    timestamp: Date.now(),
                    stack: (new Error()).stack || ''
                });
            } catch (e) {}
            return _fetch.apply(this, arguments);
        };
    } catch (e) {}
})();
"""


_XHR_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_xhr__) return;
        window.__hook_xhr__ = true;
        const _XHR = window.XMLHttpRequest;
        if (typeof _XHR !== 'function') return;
        const _open = _XHR.prototype.open;
        const _send = _XHR.prototype.send;
        const _setHeader = _XHR.prototype.setRequestHeader;
        if (typeof _open !== 'function' || typeof _send !== 'function') return;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        const _body = (b) => {
            try {
                if (b == null) return null;
                if (typeof b === 'string') return b;
                if (b instanceof ArrayBuffer) return '[binary ArrayBuffer ' + b.byteLength + 'B]';
                if (b instanceof Blob) return '[Blob ' + b.type + ' ' + b.size + 'B]';
                if (b instanceof FormData) {
                    const o = {};
                    b.forEach((v, k) => { o[k] = v; });
                    return JSON.stringify(o);
                }
                if (b instanceof Document) return '[Document]';
                try { return JSON.stringify(b); } catch (e) { return '[unserializable]'; }
            } catch (e) { return null; }
        };
        _XHR.prototype.open = function(method, url) {
            try {
                this.__hook_meta__ = {
                    method: String(method || '').toUpperCase(),
                    url: String(url || ''),
                    headers: {}
                };
            } catch (e) {}
            return _open.apply(this, arguments);
        };
        _XHR.prototype.setRequestHeader = function(k, v) {
            try {
                if (this.__hook_meta__) this.__hook_meta__.headers[String(k)] = String(v);
            } catch (e) {}
            return _setHeader.apply(this, arguments);
        };
        _XHR.prototype.send = function(body) {
            try {
                const m = this.__hook_meta__ || { method: '', url: '', headers: {} };
                _push({
                    type: 'xhr',
                    url: m.url,
                    method: m.method,
                    headers: m.headers,
                    body: _body(body),
                    timestamp: Date.now(),
                    stack: (new Error()).stack || ''
                });
            } catch (e) {}
            return _send.apply(this, arguments);
        };
    } catch (e) {}
})();
"""


_COOKIE_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_cookie__) return;
        window.__hook_cookie__ = true;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        // document.cookie 没有原型链捷径，只能从 Document.prototype 上的原生
        // 描述符拿到原始 get/set，在闭包里保留后做包裹式替换。
        const proto = window.Document ? window.Document.prototype : null;
        const _desc = proto
            ? (Object.getOwnPropertyDescriptor(proto, 'cookie')
               || Object.getOwnPropertyDescriptor(window.HTMLDocument && window.HTMLDocument.prototype, 'cookie'))
            : null;
        if (!_desc || (typeof _desc.set !== 'function' && typeof _desc.get !== 'function')) return;
        const _origSet = _desc.set;
        const _origGet = _desc.get;
        const _newDesc = {
            configurable: true,
            enumerable: true,
            get: function() {
                try { return _origGet ? _origGet.call(this) : ''; } catch (e) { return ''; }
            },
            set: function(val) {
                try {
                    _push({
                        type: 'cookie',
                        url: location.href,
                        method: 'SET',
                        headers: {},
                        body: String(val),
                        timestamp: Date.now(),
                        stack: (new Error()).stack || ''
                    });
                } catch (e) {}
                if (_origSet) return _origSet.call(this, val);
            }
        };
        try { Object.defineProperty(proto, 'cookie', _newDesc); } catch (e) {}
    } catch (e) {}
})();
"""


_WEBCRYPTO_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_webcrypto__) return;
        window.__hook_webcrypto__ = true;
        const _crypto = window.crypto || window.msCrypto;
        const _subtle = _crypto && _crypto.subtle;
        if (!_subtle) return;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        const _bytes = (buf) => {
            try {
                if (buf == null) return null;
                let ab = null;
                if (buf instanceof ArrayBuffer) ab = buf;
                else if (ArrayBuffer.isView(buf)) ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
                if (ab) {
                    if (ab.byteLength > 4096) return '[binary ' + ab.byteLength + 'B]';
                    const arr = new Uint8Array(ab);
                    let s = '';
                    for (let i = 0; i < arr.length; i++) s += String.fromCharCode(arr[i]);
                    return btoa(s);
                }
                try { return JSON.stringify(buf); } catch (e) { return '[unserializable]'; }
            } catch (e) { return null; }
        };
        const _methods = ['digest', 'encrypt', 'decrypt', 'sign', 'verify', 'deriveBits', 'deriveKey'];
        _methods.forEach((name) => {
            try {
                const _orig = _subtle[name];
                if (typeof _orig !== 'function') return;
                _subtle[name] = function(alg) {
                    try {
                        const args = Array.prototype.slice.call(arguments, 1);
                        _push({
                            type: 'webcrypto',
                            url: location.href,
                            method: name,
                            headers: {
                                algorithm: (typeof alg === 'string') ? alg : JSON.stringify(alg)
                            },
                            body: args.map(_bytes),
                            timestamp: Date.now(),
                            stack: (new Error()).stack || ''
                        });
                    } catch (e) {}
                    return _orig.apply(this, arguments);
                };
            } catch (e) {}
        });
    } catch (e) {}
})();
"""


_WEBPACK_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_webpack__) return;
        window.__hook_webpack__ = true;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        // 1) 扫描 window 上的 webpack 相关键，记录站点使用的加载器命名
        const _scan = () => {
            try {
                const keys = [];
                for (const k in window) {
                    if (/webpack/i.test(k)) keys.push(k);
                }
                _push({
                    type: 'webpack',
                    url: location.href,
                    method: 'scan',
                    headers: { webpack_keys: keys.join(',') },
                    body: null,
                    timestamp: Date.now(),
                    stack: ''
                });
            } catch (e) {}
        };
        _scan();
        // 2) 包裹 webpackChunk*.push，记录运行时注册的模块块
        const _wrapPush = (arr, name) => {
            try {
                if (!arr || !Array.isArray(arr) || arr.__hook_wrapped__) return;
                const _push_orig = arr.push.bind(arr);
                arr.push = function(chunk) {
                    try {
                        _push({
                            type: 'webpack',
                            url: location.href,
                            method: 'chunk',
                            headers: {
                                name: name,
                                chunkIds: (chunk && chunk[0]) ? JSON.stringify(chunk[0]) : ''
                            },
                            body: null,
                            timestamp: Date.now(),
                            stack: ''
                        });
                    } catch (e) {}
                    return _push_orig.apply(this, arguments);
                };
                arr.__hook_wrapped__ = true;
            } catch (e) {}
        };
        const _candidateNames = ['webpackJsonp', 'webpackChunkwebpack', 'webpackChunk_N_E'];
        for (const k in window) {
            if (/webpackChunk/i.test(k)) _candidateNames.push(k);
        }
        _candidateNames.forEach((name) => {
            try {
                const arr = window[name];
                if (arr) _wrapPush(arr, name);
            } catch (e) {}
        });
        // 3) 留一个全局探针，方便外部通过 evaluate 主动触发扫描
        try { window.__hook_webpack_rescan__ = _scan; } catch (e) {}
    } catch (e) {}
})();
"""


_CONSOLE_HOOK_JS = r"""
(() => {
    try {
        if (window.__hook_console__) return;
        window.__hook_console__ = true;
        const _push = (rec) => {
            try {
                if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
                window.__hook_data__.push(rec);
            } catch (e) {}
        };
        const _stringify = (a) => {
            try {
                if (a == null) return String(a);
                if (typeof a === 'string') return a;
                if (typeof a === 'number' || typeof a === 'boolean') return String(a);
                if (a instanceof Error) return a.name + ': ' + a.message;
                try { return JSON.stringify(a); } catch (e) { return String(a); }
            } catch (e) { return '[unserializable]'; }
        };
        const _methods = ['log', 'warn', 'error', 'info', 'debug'];
        _methods.forEach((name) => {
            try {
                const _orig = console[name];
                if (typeof _orig !== 'function') return;
                console[name] = function() {
                    try {
                        _push({
                            type: 'console',
                            url: location.href,
                            method: name,
                            headers: {},
                            body: Array.prototype.map.call(arguments, _stringify).join(' '),
                            timestamp: Date.now(),
                            stack: (new Error()).stack || ''
                        });
                    } catch (e) {}
                    return _orig.apply(console, arguments);
                };
            } catch (e) {}
        });
    } catch (e) {}
})();
"""


# 引导脚本：在组合脚本最前面执行一次，确保 __hook_data__ 容器存在。
_BOOTSTRAP_JS = r"""
(() => {
    try {
        if (!Array.isArray(window.__hook_data__)) window.__hook_data__ = [];
        if (typeof window.__hook_meta__ !== 'object' || window.__hook_meta__ === null) {
            window.__hook_meta__ = { createdAt: Date.now(), ua: navigator.userAgent };
        }
    } catch (e) {}
})();
"""


@dataclass(frozen=True, slots=True)
class HookScript:
    """单个 Hook 脚本的描述。

    name        脚本名称，对应 :class:`HookLibrary` 的类属性名；
    script      可注入的 JavaScript 源码（IIFE，自包含）；
    description 人类可读的用途说明。
    """

    name: str
    script: str
    description: str


class HookLibrary:
    """预置 JS Hook 脚本集合。

    所有 Hook 脚本作为类属性暴露，可直接通过 ``HookLibrary.fetch_hook`` 取用，
    也可通过 :meth:`get` / :meth:`all` 按名称获取。脚本本身不可变，可在多个
    page 之间共享。
    """

    fetch_hook: ClassVar[HookScript] = HookScript(
        name="fetch_hook",
        script=_FETCH_HOOK_JS,
        description="拦截 window.fetch，记录 url/method/headers/body。",
    )
    xhr_hook: ClassVar[HookScript] = HookScript(
        name="xhr_hook",
        script=_XHR_HOOK_JS,
        description="拦截 XMLHttpRequest.prototype.open/send/setRequestHeader，记录请求元数据。",
    )
    cookie_hook: ClassVar[HookScript] = HookScript(
        name="cookie_hook",
        script=_COOKIE_HOOK_JS,
        description="拦截 document.cookie setter，记录写入的 cookie 字符串。",
    )
    webcrypto_hook: ClassVar[HookScript] = HookScript(
        name="webcrypto_hook",
        script=_WEBCRYPTO_HOOK_JS,
        description="拦截 crypto.subtle.digest/encrypt/sign 等，记录算法与输入字节。",
    )
    webpack_hook: ClassVar[HookScript] = HookScript(
        name="webpack_hook",
        script=_WEBPACK_HOOK_JS,
        description="检测 webpack 加载器，记录 webpackChunk*.push 与 window 上的 webpack 键。",
    )
    console_hook: ClassVar[HookScript] = HookScript(
        name="console_hook",
        script=_CONSOLE_HOOK_JS,
        description="拦截 console.log/warn/error/info/debug，记录输出（加密库常会调试打印）。",
    )

    _ALL: ClassVar[tuple[HookScript, ...]] = (
        fetch_hook,
        xhr_hook,
        cookie_hook,
        webcrypto_hook,
        webpack_hook,
        console_hook,
    )

    @classmethod
    def all(cls) -> list[HookScript]:
        """返回全部预置 Hook 脚本。"""
        return list(cls._ALL)

    @classmethod
    def names(cls) -> list[str]:
        """返回全部预置 Hook 脚本的名称。"""
        return [h.name for h in cls._ALL]

    @classmethod
    def get(cls, name: str) -> HookScript | None:
        """按名称获取 Hook 脚本，不存在返回 None。"""
        for h in cls._ALL:
            if h.name == name:
                return h
        return None


def generate_combined_script(hooks: list[str] | None = None) -> str:
    """组合多个 Hook 脚本为一段可注入的 JavaScript。

    hooks 为 None 时使用全部预置 Hook；指定名称时按给定顺序拼接，未识别的
    名称会被忽略。返回的字符串以引导脚本开头，确保 ``window.__hook_data__``
    容器存在，适合直接传给 ``page.add_init_script``。
    """
    if hooks is None:
        selected = HookLibrary.all()
    else:
        selected = [h for h in HookLibrary.all() if h.name in set(hooks)]

    parts: list[str] = [_BOOTSTRAP_JS]
    parts.extend(h.script for h in selected)
    return "\n".join(parts)


def collect_hook_data(page: Page) -> dict:
    """从 Playwright Page 对象读取 ``window.__hook_data__``。

    读取后会清空浏览器侧数组，避免重复采集时数据叠加。返回的 dict 结构稳定，
    便于上层序列化或断言，包含 ``records``（list[dict]）与 ``count``（int）。
    """
    records = page.evaluate(
        """() => {
            const data = window.__hook_data__ || [];
            const snapshot = data.slice();
            try { window.__hook_data__ = []; } catch (e) {}
            return snapshot;
        }"""
    )
    records = records or []
    return {"records": list(records), "count": len(records)}


__all__ = ["HookLibrary", "HookScript", "collect_hook_data", "generate_combined_script"]
