# AI 能力指南

`web-crawler` 的 AI 层提供四类能力：**可插拔 LLM 供应商**、**AI 选择器生成**
（`AIExtractor`）、**礼貌 AI 爬虫**（`AIScrapeAgent`）与**图片验证码求解**
（`ImageCaptchaSolver`），并以 JS 逆向 Agent（`ReverseAgent`）收束。API 细节见
[API 参考 · AI 能力](../api-reference.md#ai)。

## 配置 API Key

所有 AI 组件默认使用 DeepSeek（模型 `DeepSeek-V4.1-Flash`），只需一个环境变量：

```bash
set DEEPSEEK_API_KEY=sk-...         # Windows
export DEEPSEEK_API_KEY=sk-...       # Linux / macOS
```

三种注入方式（优先级从高到低）：

1. 构造参数：`get_provider(api_key="sk-...")`、`DeepSeekProvider(api_key="sk-...")`；
2. 环境变量：`DEEPSEEK_API_KEY`（DeepSeek）/ `LLM_API_KEY`（自定义兼容端点）；
3. 项目根目录 `.env` 文件——进程启动时自动读取（可选依赖 python-dotenv，
   存在则读、不存在则跳过、不覆盖已有环境变量，且已入 `.gitignore`）。

key 不记入日志、不出现在异常消息中、不落盘缓存——详见
[SECURITY.md · LLM API key 处理](https://github.com/xiaomaozjj666/web-crawler/blob/master/SECURITY.md)。

## LLM 供应商层

供应商层是纯 `httpx` 实现的 OpenAI 兼容抽象，四个工厂函数即可玩转：

```python
from web_crawler import get_provider, register_provider, available_providers

print(available_providers())
# ['deepseek', 'openai', 'openai-compatible', 'qwen', ...]

provider = get_provider()                      # 默认 DeepSeek / DeepSeek-V4.1-Flash
provider = get_provider("deepseek", model="deepseek-flash")
```

接入任意 OpenAI 兼容端点（自建网关、vLLM、其他云厂商）：

```python
from web_crawler import OpenAICompatibleProvider, register_provider

register_provider("my-gateway", lambda **kw: OpenAICompatibleProvider(
    base_url="https://llm.internal.example.com/v1",
    model="my-model",
    api_key_env="LLM_API_KEY",
    **kw,
))

provider = get_provider("my-gateway")
```

`register_provider` 大小写不敏感；`OpenAICompatibleProvider` 的常用参数：
`model` / `api_key` / `base_url` / `timeout` / `default_headers`。

底层协议非常小——实现 `chat(messages) -> LLMResponse` 即可替换整个供应商层
（`LLMProvider` 为 `Protocol`，鸭子类型即满足）：

```python
reply = provider.chat([LLMMessage("user", "你好")])
print(reply.content)
```

## AIExtractor：自然语言生成选择器

把「想要什么字段」用自然语言描述出来，LLM 生成 CSS 选择器并在本地校验、
对空字段自动自愈（重新生成并重试，默认最多 2 轮）：

```python
from web_crawler import Fetcher, AIExtractor

schema = {
    "title": "商品标题，页面顶部的大号文字",
    "price": "当前售价（含货币符号）",
    "availability": "库存状态文案",
}

extractor = AIExtractor()          # 默认 DeepSeek；也可传 provider=
resp = Fetcher(impersonate="chrome131").get("https://shop.example.com/item/42")

result = extractor.extract(resp, schema)
print(result.ok)                   # 全部字段命中时为 True
print(result.data)                 # {"title": ..., "price": ...}
print(result.selectors)            # {"title": "h1.product-name", ...}  可复用！
print(result.missing)              # 仍未能解析的字段名
```

要点：

- `result.selectors` 是校验过的成品选择器，可固化到代码里长期复用，
  AI 只在改版时再请一次；
- 自愈只针对值为 `None` 的字段（空字符串等合法假值不算缺失）；
- `max_html_chars`（默认 12000）控制发给模型的 HTML 样本体积；
  `max_heal_rounds`（默认 2）控制自愈轮数；
- HTML 片段按不可信输入处理，prompt 中明确指示忽略其中的指令文本
  （防 prompt 注入）。

## AIScrapeAgent：礼貌 AI 爬虫

`AIScrapeAgent` = 抓取 + AIExtractor 抽取 + 礼貌策略全家桶：

```python
from web_crawler import AIScrapeAgent

agent = AIScrapeAgent(
    min_delay=1.0,          # 相邻请求最小间隔
    respect_robots=True,    # 跳过 robots.txt 禁止的 URL（默认开）
    max_retries=3,          # 429/503 指数退避重试，遵循 Retry-After
    detect_blocks=True,     # 检测反爬/验证码墙 → 移交人工而非硬闯
)

result = agent.scrape("https://example.com/product/42", {
    "title": "商品标题",
    "price": "售价",
})

if result.ok:
    print(result.data, result.selectors)
elif result.needs_human:
    print(f"被拦截，转人工: {result.block_reason}")
```

- JS 重页面传 `render=True`，内部自动换用 `DynamicFetcher`；
- 也可显式传入任意带 `get(url)` 的 fetcher；
- 命中反爬墙时跳过抽取、置位 `needs_human` 并调用 `on_block` 回调
  （通知运维 / 打开人工浏览器），这是对「硬绕」的负责任替代；
- 支持上下文管理器：`with AIScrapeAgent() as agent: ...`。

## ImageCaptchaSolver：图片验证码

无需浏览器即可离线识别三类图片挑战，后端按 `ddddocr` → numpy 模板匹配 →
LLM Vision 依次降级：

```python
from web_crawler import ImageCaptchaSolver

solver = ImageCaptchaSolver()   # 传入支持 vision 的 provider 可解锁点选题

text = solver.solve_text(image_bytes)                      # 文本 OCR（4-8 位）
gap_x = solver.solve_slider(bg_bytes, puzzle_bytes)        # 滑块缺口 x 坐标
coords = solver.solve_click(image_bytes, prompt="按文字顺序点击")  # 点选坐标
```

- **文本 OCR**：`ddddocr`（需 `pip install -e ".[captcha]"`）；
- **滑块缺口**：Pillow + numpy 模板匹配（需 `[visual]`）；
- **点选坐标**：视觉 LLM（provider 需具备 vision 能力，如
  `ImageCaptchaSolver(provider=get_provider(...))`）。

在 JS 逆向场景中通常不直接调用它——`CaptchaManager` 在
`enable_image_captcha=True`（默认）时自动注入图片求解器，端到端处理
hCaptcha / reCAPTCHA v2 图片挑战与 GeeTest 拼图。

!!! note "合规边界"
    验证码识别仅用于你已授权的目标；页面返回 401/403 或出现无法处理的
    挑战时，Agent 会停止并返回「转人工处理」状态，不伪造登录凭证、
    不绕过付费墙。

## ReverseAgent：JS 逆向

`ReverseAgent` 通过「观察 → 思考 → 行动」循环逆向站点前端动态生成的加密参数
（`Anti-Content`、`X-Bogus`、`_signature` 等）：注入 JS Hook 捕获运行时数据，
拆分 webpack 包，让 LLM 反混淆并在 Python 中重实现签名算法。

```python
from web_crawler import ReverseAgent, ReverseAgentConfig

config = ReverseAgentConfig(
    headless=True,
    target_params=["anti_content", "sign"],
    max_steps=20,
    enable_checkpoint=True,      # 断点续跑
    enable_screenshot=True,      # 每步截图
)
agent = ReverseAgent(config=config)
result = agent.run("https://target.example.com", task="提取签名参数")
print(result["success"], result["target_params_found"])
agent.close()
```

完整手册（工作流程、9 个浏览器动作、多标签页、人类化输入、生产级 Agent
能力矩阵、MCP / CLI / Web UI 接入方式）见 **[JS 逆向 Agent](../reverse-agent.md)**，
不在本页重复。

## 下一步

- [JS 逆向 Agent](../reverse-agent.md) — 完整逆向手册
- [Spider 爬虫框架指南](spider.md) — 把 AI 组件接入爬虫流水线
- [API 参考 · AI 能力](../api-reference.md#ai) — 全部类的自动生成文档
