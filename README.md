# Цифровое наследие — MVP

Сервис хранит зашифрованные записи Владельца (тексты и файлы) и передаёт назначенные записи
наследникам после смерти Владельца: наследник входит по ключу, загружает свидетельство о смерти,
документ проверяется автоматически (OCR + правила), Владелец получает письмо и может отменить
запрос в течение периода ожидания, после чего доступ открывается.

- Требования и архитектура: [`Архитектура.md`](Архитектура.md) — единственный источник требований.
- Толкования спецификации и отступления: [`NOTES.md`](NOTES.md).

Стек: Python 3.12, FastAPI + Jinja2, SQLAlchemy 2, PostgreSQL 16, Alembic, EasyOCR (PyTorch CPU),
Pico.css; Docker Compose; Caddy в эксплуатации; Mailpit в разработке.

## Требования

- Docker Engine 24+ и Docker Compose v2.20+ (`docker compose version`).
- `python3` на хосте — только для генерации ключей (можно заменить на `openssl`, см. ниже).
- Около 10 ГБ свободного места: образ содержит PyTorch (CPU) и веса EasyOCR.
- Не менее 4 ГБ RAM: распознавание документа занимает до 3 ГБ.

## Запуск (разработка)

```bash
git clone <адрес репозитория> digital-legacy
cd digital-legacy

# 1. Конфигурация из шаблона
cp .env.example .env

# 2. Генерация ключей (см. раздел «Ключи»)
sed -i.bak "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env
sed -i.bak "s|^MASTER_KEY=.*|MASTER_KEY=$(python3 -c 'import os, base64; print(base64.b64encode(os.urandom(32)).decode())')|" .env
rm -f .env.bak

# 3. Сборка и запуск: db, migrate, web, worker, mailpit
docker compose up --build
```

- Приложение: <http://localhost:8000>, проверка работоспособности: <http://localhost:8000/healthz>.
- Письма (Mailpit): <http://localhost:8025>.
- Первая сборка скачивает PyTorch и веса EasyOCR и занимает несколько минут. Тяжёлые слои
  кешируются: после изменения кода пересобираются только слои с кодом.
- Порты публикуются только на `127.0.0.1`.
- Worker при старте загружает модель OCR (10–20 секунд); до этого `/healthz` отвечает `503`.

Остановка: `docker compose down`. Полная очистка данных разработки (база и файлы):
`docker compose down -v`.

## Тесты

Тесты выполняются внутри образа, в отдельной базе `app_test` (создаётся `docker/db-init.sql`
при первом создании тома базы). Письма в тестах складываются в память, OCR подменяется, кроме
тестов с маркером `ocr`, которые используют настоящий EasyOCR.

```bash
docker compose run --rm web pytest                  # все тесты, включая реальный OCR
docker compose run --rm web pytest -m "not ocr"     # без реального OCR (быстрее)
docker compose run --rm web alembic upgrade head    # миграции основной базы вручную
```

- После изменения кода пересоберите образ: `docker compose build` (или `docker compose up --build`).
- Тесты не зависят от значений в `.env`: профиль test задаётся в `tests/conftest.py`.
- Если том `pgdata` создан до появления `docker/db-init.sql` и базы `app_test` нет:
  `docker compose exec db createdb -U app app_test`.

Приёмочные тесты названы по идентификаторам спецификации: `tests/test_at13_positive_path.py`
и т. д.

## Ключи

| Переменная | Назначение | Генерация |
|---|---|---|
| `SECRET_KEY` | подпись cookie сессий и ссылок в письмах; не короче 32 символов | `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` или `openssl rand -base64 48` |
| `MASTER_KEY` | мастер-ключ шифрования AES-256-GCM: base64 от ровно 32 байт | `python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"` или `openssl rand -base64 32` |

При старте web и worker проверяют ключи и параметры (3.4.1); при ошибке процесс завершается с
сообщением «Ошибка конфигурации» и перечнем причин.

Правила обращения:

- Храните оба ключа в менеджере паролей, `MASTER_KEY` — минимум в двух независимых местах и
  отдельно от резервных копий. **Без `MASTER_KEY` восстановить записи невозможно.**
- Ключи не попадают в Git (`.env` в `.gitignore`), в базу и в резервные копии. Не передавайте их
  через мессенджеры.
- Смена `SECRET_KEY` завершает все сессии и делает недействительными ссылки подтверждения и
  отмены из уже отправленных писем. Ротация `MASTER_KEY` в MVP не поддерживается.

## Синтетическое свидетельство о смерти

Для ручной проверки распознавания и сквозного сценария генератор создаёт PNG и PDF с
вымышленными данными (шрифт DejaVu Sans установлен в образе). ФИО должно совпадать с ФИО
Владельца, даты по умолчанию — сегодня (в часовом поясе `DISPLAY_TZ`):

```bash
mkdir -p /tmp/legacy-fixtures
docker compose run --rm --user "$(id -u):$(id -g)" -v /tmp/legacy-fixtures:/out web \
  python scripts/gen_fixtures.py --out /out --last Смирнова --first Анна --middle Сергеевна
# /tmp/legacy-fixtures/certificate.png и certificate.pdf
```

Параметры: `--death-date`, `--issue-date` (ГГГГ-ММ-ДД), `--series`, `--number`. Файлы не
добавляйте в репозиторий.

## Ручная сквозная проверка (4.6, п. 3)

1. В `.env` временно установите `WAITING_PERIOD_SECONDS=120` и `REMINDER_BEFORE_SECONDS=60`,
   затем `docker compose up -d` (контейнеры пересоздадутся с новыми значениями).
2. **Основной сценарий:** зарегистрируйтесь (ФИО как у свидетельства) → подтвердите email по
   письму в Mailpit → создайте текстовую и файловую записи → создайте наследника, отметьте
   часть записей и сохраните ключ → в другом браузере (или окне инкогнито) откройте
   «Я наследник», введите ключ, загрузите `certificate.png` → через 1–2 минуты в Mailpit
   приходит письмо E2, ещё через минуту — напоминание E3 → через две минуты после E2 статус
   становится «Доступ выдан» → наследник видит и скачивает только назначенные записи.
3. **Сценарий отмены:** повторите с новым наследником и отмените запрос по ссылке из письма E2 →
   вход по ключу этого наследника перестаёт работать.
4. Проверьте, что журналы не содержат ключа наследника и токенов из ссылок:
   `docker compose logs | grep -c "<ключ или токен>"` должно вернуть `0`.
5. Верните значения по умолчанию в `.env` и выполните `docker compose up -d`.

## Эксплуатация (деплой)

Требования: VPS на территории РФ (от 4 vCPU, 8 ГБ RAM, 40 ГБ SSD), Docker, домен с DNS-записью
`A` на адрес сервера, открытые порты 80 и 443, SMTP-провайдер с настроенными SPF, DKIM и DMARC.

1. Скопируйте репозиторий на сервер и создайте `.env` по разделу «Запуск», затем измените:

   ```dotenv
   APP_ENV=prod
   BASE_URL=https://legacy.example.ru   # домен без завершающей косой черты
   COOKIE_SECURE=true
   EMAIL_BACKEND=smtp
   OCR_PROVIDER=easyocr
   SMTP_HOST=smtp.provider.ru
   SMTP_PORT=587
   SMTP_USER=...
   SMTP_PASSWORD=...
   SMTP_FROM=no-reply@legacy.example.ru
   SMTP_STARTTLS=true
   POSTGRES_PASSWORD=<длинный случайный пароль>
   DATABASE_URL=postgresql+psycopg://app:<тот же пароль>@db:5432/app
   TEST_DATABASE_URL=postgresql+psycopg://app:<тот же пароль>@db:5432/app_test
   ```

   Пароль PostgreSQL задаётся при первом создании тома `pgdata`; менять его позже нужно
   командой `ALTER USER` в самой базе.

2. Ограничьте доступ к `.env`: `chmod 600 .env`.
3. Запуск:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   ```

   `docker-compose.prod.yml` добавляет Caddy (TLS с автоматическим сертификатом для домена из
   `BASE_URL`, ограничение тела запроса 12 МБ, заголовок HSTS, access-лог отключён), политику
   `restart: unless-stopped` и отключает Mailpit. Web принимает `X-Forwarded-For` только от
   Caddy.
4. Проверка: `curl https://legacy.example.ru/healthz` → `{"status": "ok", ...}`.
5. Обновление: `git pull`, затем та же команда `up -d --build` (миграции применяются
   автоматически сервисом `migrate`).

Журналы: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f web worker`.
В журналы не попадают пароли, ключи, токены, содержимое записей, текст документов и ФИО;
токены в путях ссылок маскируются.

## Резервное копирование и восстановление

Копируются база данных (`pg_dump`) и том `files` с зашифрованными файлами записей и
документами. `.env` и `MASTER_KEY` в резервную копию **не** входят и хранятся отдельно.
Требования: ежедневно (RPO — 24 часа), во внешнее хранилище, хранение 14 дней. Копия базы
содержит персональные данные в открытом виде (email, ФИО) — храните её с ограниченным доступом.

Ручное резервное копирование (из каталога проекта; в эксплуатации добавьте к командам
`docker compose` параметры `-f docker-compose.yml -f docker-compose.prod.yml`):

```bash
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
docker compose exec -T db pg_dump -U app -d app -Fc > "backups/db-$STAMP.dump"
docker compose run --rm --no-deps --user root -v "$PWD/backups:/backup" worker \
  tar czf "/backup/files-$STAMP.tar.gz" -C /data files
```

Восстановление (на чистой машине — после шагов 1–2 раздела «Запуск» или «Эксплуатация» с
**теми же** `MASTER_KEY` и `SECRET_KEY`):

```bash
docker compose up -d db
docker compose exec -T db pg_restore -U app -d app --clean --if-exists --no-owner < backups/db-<STAMP>.dump
docker compose run --rm --no-deps --user root -v "$PWD/backups:/backup" worker \
  sh -c 'rm -rf /data/files/* && tar xzf /backup/files-<STAMP>.tar.gz -C /data && chown -R app:app /data/files'
docker compose up -d
```

Проверьте восстановление: войдите Владельцем и откройте текстовую запись и файл — они
расшифровываются только при верном `MASTER_KEY`.

## Структура

```
app/            приложение: main.py, config.py, models.py, worker.py, routes/, templates/, static/
app/ocr/        подготовка изображения, EasyOCR и fake-провайдеры
app/email/      отправка писем и шаблоны E1–E9
migrations/     миграции Alembic (эталонная схема 2.3)
scripts/        gen_fixtures.py — синтетические свидетельства
tests/          приёмочные (test_atNN_*) и модульные тесты
docker/         db-init.sql — база app_test
```

## Устранение неполадок

- **`/healthz` отвечает 503** — Worker ещё загружает модель OCR или остановлен:
  `docker compose logs worker`.
- **Письма не приходят** — в разработке смотрите Mailpit; при недоступном SMTP Worker
  повторяет E2/E3 каждый цикл, а период ожидания начинается только после отправки E2.
- **Индекс PyTorch недоступен при сборке** — адрес CPU-индекса задаётся аргументом сборки
  `TORCH_INDEX_URL` в `Dockerfile` (по умолчанию `https://download.pytorch.org/whl/cpu`).
