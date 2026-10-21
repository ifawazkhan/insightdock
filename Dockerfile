# Pilot target: Python 3.14 (configured development venv: 3.14.7).
# Floating patch tag is intentional; record the resolved image digest per release.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHATBOT_MODE=cloud \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    HOME=/home/chatbot

WORKDIR /app

# CA roots for verified Google/AI/MySQL TLS; fontconfig/freetype/fonts for
# vl-convert's Linux wheels and static PDF charts. No compiler toolchain.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libfontconfig1 libfreetype6 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chatbot \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin chatbot

COPY requirements-cloud.txt /app/requirements-cloud.txt
RUN python -m pip install --only-binary=:all: -r /app/requirements-cloud.txt \
    && python -m pip check

# Explicit source allowlist: never COPY the repository, datasets or secrets.
COPY chat_app.py /app/chat_app.py
COPY chatbot/ /app/chatbot/
COPY assets/ /app/assets/
COPY .streamlit/config.toml /app/.streamlit/config.toml

USER 10001:10001
EXPOSE 8501

# Process-only health: no authentication, provider calls, credentials or live DB.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3); assert r.status == 200 and r.read().strip() == b'ok'"]

CMD ["python", "-m", "streamlit", "run", "chat_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--server.fileWatcherType=none", "--server.enableXsrfProtection=true"]