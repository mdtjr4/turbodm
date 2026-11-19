FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TURBODM_CLOUD_MODE=1 \
    TURBODM_DATA_DIR=/app/data \
    TURBODM_DOWNLOAD_DIR=/app/data/downloads \
    PORT=7860

# Install FFmpeg (for HD video/audio muxing) and Node.js (for yt-dlp JS challenges)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY app.py README.md ./

# Ensure writable directories for both root and non-root cloud runners (e.g., Hugging Face Spaces)
RUN mkdir -p /app/data/downloads /app/bin && chmod -R 777 /app/data /app/bin

EXPOSE 7860

CMD ["sh", "-c", "uvicorn backend.server:app --host 0.0.0.0 --port ${PORT:-7860}"]
