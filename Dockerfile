FROM python:3.10-slim AS builder

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir . uvicorn[standard]

FROM python:3.10-slim AS runner

WORKDIR /app
RUN useradd -m -u 1000 incidentops && chown -R incidentops:incidentops /app

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY packages/ /app/packages/
COPY services/ /app/services/
COPY apps/ /app/apps/
COPY evals/ /app/evals/
COPY pyproject.toml /app/

USER incidentops
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/evals/summary')" || exit 1

CMD ["python", "-m", "uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
