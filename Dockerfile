FROM python:3.12-alpine

RUN apk add --no-cache ca-certificates \
    && addgroup -S jarvis-image \
    && adduser -S -D -H -G jarvis-image jarvis-image
WORKDIR /app
COPY --chown=root:root gateway/image_gateway.py /app/image_gateway.py
COPY --chown=root:root app/jarvis/certs/russian_trusted_root_ca.pem /app/russian_trusted_root_ca.pem

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_GATEWAY_HOST=0.0.0.0
EXPOSE 8780
USER jarvis-image
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8780')+'/health', timeout=3).read()"
CMD ["python3", "/app/image_gateway.py"]
