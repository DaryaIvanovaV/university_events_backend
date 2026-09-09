#!/bin/sh
# Точка входу всіх трьох контейнерів (api, worker, bot).
#
# Єдина її задача — накотити міграції перед стартом процесу.
# Робить це ТІЛЬКИ контейнер з RUN_MIGRATIONS=true (у compose це api):
# якби `alembic upgrade head` запускали одночасно кілька
# контейнерів, вони побилися б за одну й ту саму DDL-транзакцію.
#
# Схема створюється Alembic'ом, а не create_all: останній не вміє ALTER для
# уже наявних таблиць — рівно на цьому проєкт одного разу зламався, коли
# додалася колонка meeting_code.

set -e

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
