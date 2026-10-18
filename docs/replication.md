# Репликация базы tlmtry

Инкрементальное копирование телеметрии из основной базы в резервную. Источник и приёмник независимы: каждый может быть файлом SQLite или URL PostgreSQL (`postgres://` или `postgresql://`).

Один проход делает функция `replicate`. Её вызывает и CLI, и фоновый поток сборщика. Внешний скрипт запускает тот же проход сам — сборщик для этого не нужен.

## Что копируется

| Таблица | Как переносится |
|---------|-----------------|
| `devices` | Полный каталог при каждом проходе, upsert по `address` |
| `metrics` | Полный каталог при каждом проходе, upsert по `(address, metric_id)` |
| `samples` | Только строки с `id` больше водяного знака. `id` сохраняется |
| `replication_state` | На источнике — курсор `samples_id`. На приёмнике — метка `last_replicate_ts` |

Сэмплы на приёмнике вставляются с исходным `id`. Повторная вставка того же `id` ничего не меняет (`INSERT OR IGNORE` в SQLite, `ON CONFLICT (id) DO NOTHING` в PostgreSQL). Каталог устройств и метрик обновляется целиком, даже если новых сэмплов нет.

Схема на приёмнике создаётся при открытии, если таблиц ещё нет. Отдельный SQL-скрипт для резервной базы не нужен.

## Один проход

1. Прочитать водяной знак `samples_id` из `replication_state` источника. Нет строки — знак равен `0`.
2. Выбрать из `samples` строки с `id` больше знака, по возрастанию `id`.
3. Прочитать все строки `devices` и `metrics`.
4. Открыть приёмник и в одной транзакции записать каталог, новые сэмплы и `last_replicate_ts`.
5. Если скопирован хотя бы один сэмпл, записать на источник новый знак: максимальный скопированный `id`.
6. Закрыть приёмник. Соединение с источником остаётся у вызывающего кода.

Водяной знак сдвигается только после успешной транзакции приёмника. Ошибка соединения, обрыв или откат транзакции оставляют знак на месте, следующий проход повторяет те же строки. Повтор безопасен: приёмник игнорирует уже записанные `id`.

Знак хранится на источнике и один на всю базу. Он отмечает, что уже ушло в резерв, а не «куда». Второй приёмник с тем же источником этот знак не получит: после первого успешного прохода строки для него уже считаются скопированными.

Все новые сэмплы читаются в память одним списком. Отдельных порций нет. Долгий простой источника перед первым проходом означает один большой вызов.

## Куда можно писать

`Storage` отличает PostgreSQL от SQLite по схеме URL. Подходят только `postgres://` и `postgresql://`. Строка `host=... dbname=...` будет принята за путь к файлу SQLite.

Для PostgreSQL нужен пакет с драйвером:

```bash
pip install -e "/opt/tlmtry/collector[postgres]"
```

Пароль в журнал не попадает: в сообщениях `replicate` URL печатается с `***` вместо пароля.

Съёмный SQLite-файл может быть на отмонтированной флешке. `backup_ready` возвращает `False`, если родительский каталог пути не существует. Проверка нужна до вызова `replicate`: без неё `Storage` создаст недостающие каталоги на корневой файловой системе. Для PostgreSQL `backup_ready` всегда возвращает `True`; доступность сервера видна только в момент соединения.

## Кто двигает водяной знак

Знак двигает тот процесс, который вызвал `replicate`. Одновременно это должен делать только один процесс и только в одну резервную базу.

Фоновый поток сборщика включается, только если в YAML заданы оба поля:

```yaml
backup_path: /media/backup/tlmtry-backup.db
replicate_every_s: 3600
```

`backup_path` — путь SQLite или URL PostgreSQL. Поток ждёт `replicate_every_s` секунд и только потом делает первый проход. Неготовое съёмное хранилище пропускается, ошибка прохода пишется в журнал `tlmtry.replicate` и сбор не останавливается.

Если расписание принадлежит внешнему скрипту, эти два поля в конфиге сборщика не задают. Иначе сборщик и скрипт делят один `samples_id`.

## Интеграция из другого скрипта

Скрипт открывает источник, один раз вызывает `replicate` и сам закрывает источник. Приёмник открывает и закрывает `replicate`.

Минимальный вызов:

```python
import logging

from tlmtry_collector.replicate import backup_ready, replicate
from tlmtry_collector.storage import Storage

logging.basicConfig(level=logging.INFO)

PRIMARY = "/var/lib/tlmtry/primary.db"
BACKUP = "/media/backup/tlmtry-backup.db"


def run_replication(primary_db: str, backup: str) -> dict[str, int] | None:
    """Скопировать новые сэмплы. None — приёмник сейчас недоступен."""
    if not backup_ready(backup):
        logging.warning("backup is not ready, skip: %s", backup)
        return None

    primary = Storage(primary_db)
    try:
        return replicate(primary, backup)
    finally:
        primary.close()


if __name__ == "__main__":
    result = run_replication(PRIMARY, BACKUP)
    if result is not None:
        print(result)
```

Успешный результат:

```python
{"copied_samples": 12, "watermark_before": 40, "watermark_after": 52}
```

Повторный вызов без новых сэмплов возвращает `copied_samples == 0` и тот же знак в обоих полях. Каталог на приёмнике при этом всё равно обновляется.

Адреса можно взять из того же YAML, что использует сборщик. `load_config` не запускает репликацию сам:

```python
from tlmtry_collector.config import load_config

cfg = load_config("/etc/tlmtry/collector.yaml")
if not cfg.backup_path:
    raise SystemExit("в конфиге нет backup_path")

result = run_replication(cfg.database, cfg.backup_path)
```

Свой цикл, если скрипт должен работать постоянно:

```python
import time

INTERVAL_S = 3600

while True:
    try:
        run_replication(PRIMARY, BACKUP)
    except Exception:
        logging.exception("replication failed")
    time.sleep(INTERVAL_S)
```

Исключение из `replicate` до сдвига знака означает, что приёмник не зафиксировал этот проход. Следующая итерация повторяет его. Интервал и число попыток задаёт вызывающий скрипт.

Проверка знака без копирования:

```python
primary = Storage(PRIMARY)
try:
    print(primary.get_replication_watermark())
finally:
    primary.close()
```

Тот же счётчик печатает `tlmtry-collector status`.

### Вызов командой

Из shell или чужого языка тот же проход запускается CLI. Код выхода `0` — проход выполнен, `2` — не задан приёмник. Ошибка базы завершает процесс ненулевым кодом, знак не сдвигается.

```bash
tlmtry-collector replicate \
  --database /var/lib/tlmtry/primary.db \
  --to /media/backup/tlmtry-backup.db
```

Либо цель из конфига (`backup_path`), если `--to` не передан:

```bash
tlmtry-collector -c /etc/tlmtry/collector.yaml replicate
```

В stdout печатается repr словаря Python, не JSON. Разбирать его из shell не нужно. Для расписания cron достаточно кода выхода; для числа скопированных строк вызывайте функцию из Python.

Команда `replicate` не проверяет `backup_ready`. Для съёмного диска проверку делает обёртка до запуска CLI:

```bash
#!/bin/sh
set -eu

backup=/media/backup/tlmtry-backup.db
if [ ! -d "$(dirname "$backup")" ]; then
    echo "backup mount is absent, skip" >&2
    exit 0
fi

exec tlmtry-collector replicate \
    --database /var/lib/tlmtry/primary.db \
    --to "$backup"
```

PostgreSQL в любую сторону передаётся тем же `--database` и `--to`:

```bash
tlmtry-collector replicate \
  --database 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry' \
  --to 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry_backup'
```

### Правила для внешнего скрипта

- Один процесс вызывает `replicate` для одной пары источник → приёмник. Второе расписание на тот же источник не добавляют.
- Пока скрипт сам копирует базу, в конфиге сборщика не задают `backup_path` вместе с `replicate_every_s`.
- Перед SQLite на съёмном носителе вызывают `backup_ready` (или проверяют каталог в shell).
- Источник закрывает вызывающий код. Приёмник закрывать не нужно.
- `replicate` не режет выборку на части. Пауза между проходами должна оставлять объём новых строк приемлемым для одного чтения в память.
- Пользователь источника должен читать `devices`, `metrics`, `samples` и писать `replication_state`. Пользователь приёмника должен создавать таблицы и писать во все четыре.

Чтение резервной копии для отчётов к знаку отношения не имеет: знак живёт на источнике, на приёмнике лежит только `last_replicate_ts`.
