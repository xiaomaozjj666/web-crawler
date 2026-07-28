# 基础镜像：Camoufox 与 Playwright 需要 Python 3.12
FROM python:3.12-slim

# 安装系统依赖：Firefox ESR + Playwright 运行所需的共享库
RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    libnss3 \
    libatk1.0-0 \
    libxkbcommon0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制项目文件（.dockerignore 会排除缓存与输出目录）
COPY pyproject.toml ./
COPY src/ ./src/
COPY app/ ./app/
COPY README.md ./
COPY demo.py ./

# 安装项目（含 all 可选依赖：curl_cffi / playwright / pydantic）
RUN pip install --no-cache-dir -e ".[all]"

# 安装 Playwright Firefox 浏览器及系统依赖
RUN python -m playwright install --with-deps firefox

# 暴露 Web UI 端口
EXPOSE 8765

# 默认命令：启动 Web UI，监听所有网卡
CMD ["python", "app/ui.py", "--host", "0.0.0.0", "--port", "8765"]
