# Dockerfile
FROM python:3.11-slim

# 작업 디렉토리 생성
WORKDIR /app

# 빌드에 필요한 패키지 설치 (psycopg2 등)
RUN apt-get update && \
    apt-get install -y build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt 복사 및 의존성 설치
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# 컨테이너 시작 시 bot.py 실행
CMD ["python", "bot.py"]