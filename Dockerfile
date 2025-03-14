FROM python:3.11-slim

WORKDIR /app

# 먼저 requirements.txt만 복사
COPY requirements.txt .

# apt + pip install
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --upgrade pip \
  && pip install -r requirements.txt

# 그 다음 나머지 소스 (bot.py 등)
COPY bot.py .

CMD ["python", "bot.py"]