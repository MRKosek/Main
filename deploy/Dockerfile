FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /app
RUN useradd --create-home --uid 10001 bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY dashboard ./dashboard
COPY strategy.md ./
RUN mkdir -p data reports && chown -R bot:bot /app
USER bot
CMD ["python", "-m", "bot.runner"]
