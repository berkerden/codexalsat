FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend/src

WORKDIR /app

RUN useradd --create-home --uid 10001 spotlab

COPY requirements.lock ./
RUN pip install --requirement requirements.lock

COPY backend/src ./backend/src

ARG SPOTLAB_BUILD_REVISION=uncommitted
RUN printf '%s\n' "$SPOTLAB_BUILD_REVISION" \
      | grep -Eq '^([0-9a-f]{40}|uncommitted)$' \
    && printf '%s\n' "$SPOTLAB_BUILD_REVISION" > /app/BUILD_REVISION

RUN mkdir -p /app/data && chown -R spotlab:spotlab /app/data

USER spotlab
EXPOSE 8000

CMD ["uvicorn", "spotlab.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM node:22-alpine AS web

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN chown -R node:node /app

USER node
EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
