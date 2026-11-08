FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd -m appuser && mkdir -p runs uploads .cache && chown -R appuser /app && chmod +x start.sh
USER appuser

EXPOSE 8000
CMD ["./start.sh"]
