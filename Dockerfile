# Python 이미지 설정 (3.11 버전 사용)
FROM python:3.11

# 작업 디렉터리 설정
WORKDIR /app

# 현재 디렉터리의 모든 파일을 컨테이너에 복사
COPY . /app

# 필요한 패키지 설치
RUN pip install -r requirements.txt

# 봇 실행 명령어
CMD ["python", "bot.py"]