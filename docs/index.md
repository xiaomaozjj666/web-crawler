# web-crawler

> Scrapling 风格隐身爬虫库 + JS 逆向 Agent

`web-crawler` 是一个面向生产环境的 Python 爬虫库，对齐 [Scrapling](https://github.com/D4Vinci/Scrapling) 的核心 API，并在其基础上扩展出 **JS 逆向 Agent**、**TLS 指纹定制**、**多标签页管理**、**人类化输入轨迹** 等高级能力。

完整特性列表、安装方式与快速开始请见 [README](https://github.com/xiaomaozjj666/web-crawler#readme)。

## 文档导航

- [架构说明](architecture.md) — 模块树、各组件职责、数据流图
- [JS 逆向 Agent](reverse-agent.md) — 完整使用指南、配置项、能力矩阵
- [API 参考](api-reference.md) — 公开 API 自动生成文档

## 合规说明

Agent 仅模拟正常用户交互，不伪造登录凭证、不绕过付费墙。图片验证码（OCR / 滑块 / 点选）通过 `ImageCaptchaSolver` 自动识别；当页面返回 401/403 或出现无法处理的挑战时，Agent 会停止并返回"人工接管"状态。

## License

MIT. 见 [LICENSE](https://github.com/xiaomaozjj666/web-crawler/blob/master/LICENSE)。
