# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCAGENT_CORPUS_DIR=/data/corpus \
    DOCAGENT_DB_PATH=/data/docagent.db \
    DOCAGENT_BASELINE_PATH=/data/baseline.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docagent ./docagent
COPY console ./console
COPY jobs ./jobs
COPY data/generate_corpus.py ./data/generate_corpus.py
COPY deploy/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh && useradd --uid 10001 --no-create-home appuser \
    && mkdir -p /data && chown -R appuser /data /app
USER 10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
