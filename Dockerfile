# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RADIO_LISTEN=0.0.0.0:8080 \
    FFMPEG=/usr/bin/ffmpeg

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame

WORKDIR /app
COPY radio.py healthcheck.py /app/

RUN useradd --system --uid 10001 --no-create-home radio
USER radio

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "/app/healthcheck.py"]

ENTRYPOINT ["python3", "/app/radio.py"]
