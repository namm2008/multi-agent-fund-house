FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt && \
    apt-get update && apt-get install -y --no-install-recommends curl tzdata && rm -rf /var/lib/apt/lists/* && \
    curl -fsSL -o /usr/local/bin/supercronic https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 && \
    chmod +x /usr/local/bin/supercronic
ENV TZ=Asia/Hong_Kong PYTHONUNBUFFERED=1
COPY deploy/docker/crontab /etc/fundhouse.cron
CMD sh -c "python scripts/sentinel.py & supercronic /etc/fundhouse.cron"
