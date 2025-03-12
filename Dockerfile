# 베이스 이미지로 python:3.11 사용
FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 필요한 파일 복사 (코드 및 requirements.txt)
COPY bot.py .
COPY requirements.txt .

# 패키지 설치
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Fly.io에서는 $PORT 환경변수를 사용합니다.
ENV PORT=8080

# 컨테이너 실행 명령
CMD ["python", "bot.py"]