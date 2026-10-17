FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 plugin \
    && useradd --uid 10001 --gid plugin --no-create-home plugin

WORKDIR /app
COPY invoice_extract.py limited_exec.py ./
USER 10001:10001
ENV HOST=0.0.0.0 PORT=8080 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "invoice_extract.py"]
