FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 spotlab

COPY pyproject.toml README.md ./
COPY backend/src ./backend/src

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /app/data && chown -R spotlab:spotlab /app/data

USER spotlab
EXPOSE 8000

CMD ["uvicorn", "spotlab.main:app", "--host", "0.0.0.0", "--port", "8000"]
