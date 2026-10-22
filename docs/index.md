# web-crawler

> Scrapling 风格隐身爬虫库 + JS 逆向 Agent

`web-crawler` 是一个面向生产环境的 Python 爬虫库，对齐 [Scrapling](https://github.com/D4Vinci/Scrapling) 的核心 API，并在其基础上扩展出 **JS 逆向 Agent**、**TLS 指纹定制**、**多标签页管理**、**人类化输入轨迹** 等高级能力。

刚接触本项目？从 [快速开始](quickstart.md) 起步——10 分钟完成安装、第一个隐身请求与第一只 Spider。

## 文档导航

- [快速开始](quickstart.md) — 安装、最小示例、下一步方向
- [进阶指南](guides/spider.md)
    - [Spider 爬虫框架](guides/spider.md) — 中间件 / 管道 / 去重 / 限速 / 暂停续跑 / 统计
    - [隐身与网络](guides/stealth.md) — TLS 指纹 / 会话池 / 代理轮换 / 安全边界
    - [AI 能力](guides/ai.md) — AIExtractor / AIScrapeAgent / 图片验证码 / 逆向 Agent
- [架构说明](architecture.md) — 模块树、各组件职责、数据流图
- [JS 逆向 Agent](reverse-agent.md) — 完整使用指南、配置项、能力矩阵
- [API 参考](api-reference.md) — 全部 59 个公开符号的自动生成文档
- [更新日志](https://github.com/xiaomaozjj666/web-crawler/blob/master/CHANGELOG.md) — 版本历史

## 合规说明

Agent 仅模拟正常用户交互，不伪造登录凭证、不绕过付费墙。图片验证码（OCR / 滑块 / 点选）通过 `ImageCaptchaSolver` 自动识别；当页面返回 401/403 或出现无法处理的挑战时，Agent 会停止并返回"转人工处理"状态。

## License

MIT. 见 [LICENSE](https://github.com/xiaomaozjj666/web-crawler/blob/master/LICENSE)。
