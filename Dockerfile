# Python 3.11 slim 이미지 사용
FROM python:3.11-slim

# 작업 디렉터리 설정
WORKDIR /app

# requirements.txt 먼저 복사
COPY requirements.txt .

# slim 이미지에서 psycopg2-binary 설치를 위해 gcc, libpq-dev 필요
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

# 나머지 코드 (bot.py 등) 복사
COPY . .

# Fly.io에서 PORT=8080 (사실 Polling만 해도 프로세스가 안 죽지만 예시로 설정)
ENV PORT=8080

# 컨테이너 실행 시 bot.py 실행
CMD ["python", "bot.py"]