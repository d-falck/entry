FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt
COPY handler.py worker.py speech.py server.py ring_access.py scheduling.py google_login.py ./
COPY web ./web
RUN mkdir /data
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
# Fly volume ownership is controlled by the host. Keep a restrictive process umask.
CMD ["python", "server.py"]
