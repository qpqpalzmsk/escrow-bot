# 1. 베이스 이미지
FROM python:3.11-slim

# 2. 작업 디렉토리
WORKDIR /app

# 3. requirements.txt 먼저 복사
COPY requirements.txt .

# 4. C라이브러리/라이브pq-dev + pip 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

# 5. 이제 bot.py 복사
COPY bot.py .

# Fly.io는 PORT=8080 기준
ENV PORT=8080

CMD ["python", "bot.py"]