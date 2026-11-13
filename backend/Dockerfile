FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fonts-noto-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --upgrade yt-dlp
COPY app ./app
ENV CLIPFORGE_DATA_DIR=/data
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
