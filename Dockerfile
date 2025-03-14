FROM python:3.11-slim

WORKDIR /app

# requirements.txt 먼저 복사
COPY requirements.txt .

# apt + pip install
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

# 나머지 파일들 (bot.py 등)
COPY bot.py .
# ... 필요한 모든 소스 COPY

# fly.io에서 필요 없는 HTTP services라면 없어도 됨
CMD ["python", "bot.py"]