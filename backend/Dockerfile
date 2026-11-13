FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-noto-core curl unzip ca-certificates git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --upgrade --pre "yt-dlp[default]" bgutil-ytdlp-pot-provider
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider && cd /opt/bgutil-ytdlp-pot-provider/server && deno install --allow-scripts=npm:canvas --frozen
COPY app ./app
ENV CLIPFORGE_DATA_DIR=/data
EXPOSE 10000
CMD ["sh", "-c", "cd /opt/bgutil-ytdlp-pot-provider/server/node_modules && deno run --allow-env --allow-net --allow-ffi=. --allow-read=. ../src/main.ts --host 127.0.0.1 & exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
