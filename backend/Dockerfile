FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-noto-core curl unzip ca-certificates git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --upgrade "yt-dlp[default]"
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh && deno --version && yt-dlp --version && python -m pip show yt-dlp-ejs
COPY app ./app
ENV CLIPFORGE_DATA_DIR=/data
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
