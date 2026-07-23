# Generic container — works on Render, Fly.io, Railway, Cloud Run, etc.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data (cards.db + uploads) lives here — mount a persistent volume at /var/data.
ENV DATA_DIR=/var/data
RUN mkdir -p /var/data

EXPOSE 8000
# $PORT is provided by most hosts; default 8000 locally.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
