#!/usr/bin/env bash
# Резервное копирование (1.1.3, 4.5 п. 6): pg_dump базы и архив тома files.
#
# Запуск из любого каталога; по умолчанию работает с конфигурацией эксплуатации.
# Ежедневно через cron, например:
#   15 3 * * * /opt/digital-legacy/scripts/backup.sh >> /var/log/digital-legacy-backup.log 2>&1
#
# Переменные (необязательные):
#   BACKUP_DIR        каталог копий на сервере (по умолчанию ./backups в каталоге проекта)
#   BACKUP_KEEP_DAYS  срок хранения, дней (по умолчанию 14)
#   BACKUP_REMOTE     назначение rsync во внешнем хранилище, например backup@host:/srv/legacy;
#                     каталог копий зеркалируется туда вместе с удалением старых копий
#   COMPOSE_FILE      файлы Compose (по умолчанию docker-compose.yml:docker-compose.prod.yml;
#                     для разработки — docker-compose.yml)
#
# .env и MASTER_KEY в копию не входят: храните их отдельно (README, «Ключи»).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
umask 077  # копия базы содержит персональные данные

echo "[$(date -u +%FT%TZ)] резервное копирование $STAMP → $BACKUP_DIR"

# 1. База данных: сначала во временный файл, чтобы не оставить обрезанную копию.
docker compose exec -T db pg_dump -U app -d app -Fc > "$BACKUP_DIR/db-$STAMP.dump.partial"
mv "$BACKUP_DIR/db-$STAMP.dump.partial" "$BACKUP_DIR/db-$STAMP.dump"

# 2. Том files (зашифрованные файлы записей и документы).
docker compose run --rm --no-deps --user root -v "$BACKUP_DIR:/backup" worker \
  sh -c "tar czf /backup/files-$STAMP.tar.gz.partial -C /data files && mv /backup/files-$STAMP.tar.gz.partial /backup/files-$STAMP.tar.gz && chmod 600 /backup/files-$STAMP.tar.gz"

# 3. Хранение: удаляются копии старше KEEP_DAYS дней.
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'db-*.dump' -o -name 'files-*.tar.gz' -o -name '*.partial' \) \
  -mtime "+$KEEP_DAYS" -delete

# 4. Копирование во внешнее хранилище.
if [ -n "${BACKUP_REMOTE:-}" ]; then
  rsync -a --delete "$BACKUP_DIR/" "$BACKUP_REMOTE/"
  echo "скопировано в $BACKUP_REMOTE"
else
  echo "BACKUP_REMOTE не задан: копия осталась только на сервере" >&2
fi

echo "[$(date -u +%FT%TZ)] готово: db-$STAMP.dump, files-$STAMP.tar.gz"
