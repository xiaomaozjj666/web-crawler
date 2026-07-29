# 基础镜像：Camoufox 与 Playwright 需要 Python 3.12
FROM python:3.12-slim

# 安装系统依赖：Firefox ESR + Playwright 运行所需的共享库
# fonts-noto-cjk 改善容器内中文渲染（截图 / 日志可读性）
RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    libnss3 \
    libatk1.0-0 \
    libxkbcommon0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制项目文件（.dockerignore 会排除缓存与输出目录）
COPY pyproject.toml ./
COPY src/ ./src/
COPY app/ ./app/
COPY README.md ./
COPY demo.py ./

# 安装项目（含全部可选依赖：curl_cffi / playwright / pydantic / camoufox / ddddocr / pycryptodome / Pillow）
RUN pip install --no-cache-dir -e ".[all,camoufox,captcha,crypto,visual]"

# 安装 Playwright Firefox 浏览器及系统依赖
RUN python -m playwright install --with-deps firefox

# 创建非 root 用户并切换（容器安全最佳实践）
# appuser 无 login shell 权限，仅用于运行时降权
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appgroup /app

# 运行时输出目录（与 docker-compose 卷挂载对应）
RUN mkdir -p /app/crawler_output /app/reverse_screenshots \
    && chown -R appuser:appgroup /app/crawler_output /app/reverse_screenshots

USER appuser

# 暴露 Web UI 端口
EXPOSE 8765

# 默认命令：启动 Web UI，监听所有网卡
CMD ["python", "app/ui.py", "--host", "0.0.0.0", "--port", "8765"]
