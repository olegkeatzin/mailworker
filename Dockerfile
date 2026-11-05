# syntax=docker/dockerfile:1

# MailWorker: автоматическая обработка почтового ящика в журнал учёта.
# Образ ставит клиентскую библиотеку Firebird (нужна пакету fdb) и работает
# от непривилегированного пользователя.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="MailWorker" \
      org.opencontainers.image.description="Автоматическая обработка почтового ящика в журнал учёта" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# libfbclient2 — клиент Firebird для fdb; tzdata — корректные даты регистрации
RUN apt-get update \
 && apt-get install -y --no-install-recommends libfbclient2 ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY processor ./processor
COPY classifiers ./classifiers
COPY config.ini.example ./config.ini.example

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mailworker \
 && mkdir -p /app/config /app/data \
 && chown -R mailworker:mailworker /app

USER mailworker

# Состояние обработки, архив писем и логи
VOLUME ["/app/data"]

# Конфигурация монтируется в /app/config/config.ini; аргументы докер-команды
# добавляются к этой строке (например: docker run … --once или --check).
ENTRYPOINT ["python", "main.py", "--config", "/app/config/config.ini"]
CMD []
