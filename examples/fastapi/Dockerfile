FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY protoprompt ./protoprompt
COPY examples/fastapi_memory_service.py ./examples/fastapi_memory_service.py
RUN python -m pip install --no-cache-dir ".[fastapi]" \
    && useradd --create-home --uid 10001 protoprompt \
    && mkdir -p /data \
    && chown -R protoprompt:protoprompt /data

USER 10001
ENV PROTOPROMPT_DB=/data/memory.db
EXPOSE 8000
CMD ["python", "examples/fastapi_memory_service.py"]
