# Образ спільний для трьох процесів: api, worker і bot. Вони відрізняються лише
# командою запуску (див. docker-compose.yml) — код і залежності одні й ті самі,
# тому збирати три образи нема сенсу.

# 3.13, а не 3.14, хоча проєкт розробляється на 3.14. Причина конкретна:
# firebase-admin тягне grpcio, у якого під свіжу версію Python може не
# знайтися готових wheel — тоді збірка йде компілювати C-розширення
# (десятки хвилин і компілятор в образі). Коду, специфічного для 3.14, у
# проєкті немає. Переконаєтеся, що колеса з'явилися, — підніміть значення:
#   docker compose build --build-arg PYTHON_VERSION=3.14
ARG PYTHON_VERSION=3.13


# --- Етап 1: збірка залежностей ---
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential потрібен, лише якщо для якогось пакета не знайшлося wheel.
# У фінальний образ цей шар не потрапляє.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Окреме оточення, яке цілком переїде в runtime-образ.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Спершу тільки requirements: шар із залежностями перевикористовується, поки
# файл не змінився, і правка коду не спричиняє перевстановлення пакетів.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# --- Етап 2: робочий образ ---
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Europe/Kyiv

# Користувач без root: процес у контейнері не повинен мати прав, яких
# йому не потрібно. Якщо том змонтується на хост, файли не належатимуть root.
RUN useradd --create-home --uid 1000 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app . .

COPY --chown=app:app docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app

EXPOSE 8000

# Проба живості через стандартну бібліотеку — щоб не тягти в образ curl
# заради одного рядка. /health/live навмисно не ходить у БД: недоступна база
# не має призводити до перезапуску контейнера API, лагодити треба не його.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
