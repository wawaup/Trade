FROM python:3.11-slim

WORKDIR /app

COPY tradebot ./tradebot
COPY web ./web
COPY README.md ./README.md
COPY docs ./docs

EXPOSE 8765

CMD ["python", "-m", "tradebot.cli", "dashboard", "--host", "0.0.0.0", "--port", "8765"]
