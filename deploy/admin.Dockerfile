FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/tlmtry
COPY collector /opt/tlmtry/collector
RUN pip install --no-cache-dir "/opt/tlmtry/collector[postgres]" \
    && mkdir -p /data

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow \
    TLMTRY_DATABASE=/data/primary.db

EXPOSE 8765
VOLUME ["/data"]
CMD ["tlmtry-collector", "admin", "--host", "0.0.0.0", "--port", "8765"]
