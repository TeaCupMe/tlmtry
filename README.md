# tlmtry

Система сбора телеметрии с устройств Arduino по RS-485 для изоляционного лабораторного эксперимента.

- **Arduino-библиотека** `Tlmtry` — регистрация датчиков, discovery, ответы на опрос
- **ПК-сборщик** `tlmtry-collector` — периодический опрос, SQLite или PostgreSQL, репликация в резервную SQLite/PostgreSQL
- **Автозапуск** через systemd (Linux) или Task Scheduler (Windows)
- До **100** устройств, у каждого своя периодичность опроса

## Архитектура

```
Arduino nodes (RS-485)  <-->  USB-RS485  <-->  tlmtry-collector  -->  primary (SQLite | PostgreSQL)
                                                              \-->  backup (SQLite | PostgreSQL)
```

Устройства сами сообщают манифест (метрики + желаемый интервал) по команде `DISCOVER`.

Спецификация: [docs/protocol.md](docs/protocol.md), железо: [docs/hardware.md](docs/hardware.md).

## Быстрый старт (ПК)

```bash
cd collector
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Без железа — симулятор двух устройств (SQLite):
tlmtry-collector run --sim --database /tmp/tlmtry-primary.db

# Статус / репликация:
tlmtry-collector status --database /tmp/tlmtry-primary.db
tlmtry-collector replicate --database /tmp/tlmtry-primary.db --to /tmp/tlmtry-backup.db
```

### PostgreSQL

```bash
pip install -e ".[postgres]"
# создайте БД и пользователя, затем:
tlmtry-collector run --sim \
  --database 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry'

# репликация primary → backup (оба могут быть Postgres или SQLite в любой комбинации):
tlmtry-collector replicate \
  --database 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry' \
  --to 'postgresql://tlmtry:secret@127.0.0.1:5432/tlmtry_backup'
```

В YAML поля `database` и `backup_path` принимают либо путь к файлу SQLite, либо URL `postgres://` / `postgresql://`.

С реальным адаптером скопируйте [config/collector.example.yaml](config/collector.example.yaml), укажите `port` и:

```bash
tlmtry-collector run --config /path/to/collector.yaml
```

## Автозапуск (Linux, systemd)

Сборщик рассчитан на работу как служба — вручную его держать не нужно.

```bash
sudo useradd --system --home /var/lib/tlmtry --shell /usr/sbin/nologin tlmtry
sudo usermod -aG dialout tlmtry
sudo mkdir -p /var/lib/tlmtry /etc/tlmtry
sudo cp config/collector.example.yaml /etc/tlmtry/collector.yaml
# отредактируйте port и database (SQLite-путь или PostgreSQL URL)

sudo pip install -e /opt/tlmtry/collector   # или скопируйте репозиторий в /opt/tlmtry
# для PostgreSQL: sudo pip install -e "/opt/tlmtry/collector[postgres]"
sudo cp deploy/tlmtry-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tlmtry-collector
sudo journalctl -u tlmtry-collector -f
```

Windows: см. [deploy/windows-autostart.md](deploy/windows-autostart.md).

Docker для доступа к COM/USB–RS485 на учебном стенде не используем (пути устройств и права усложняют стенд).

## Arduino

1. Установите библиотеку из `arduino/Tlmtry` в Arduino IDE / PlatformIO.
2. Откройте пример `BasicSensors`, задайте уникальный `DEVICE_ADDRESS` (1..100).
3. Подключите MAX485 (см. [docs/hardware.md](docs/hardware.md)).

```cpp
device.addMetric("temp_c", TlmtryMetricType::Float, "C", readTemp);
device.setPollIntervalMs(2000);
```

## CLI

| Команда | Назначение |
|---------|------------|
| `run` | Основной цикл сбора (для systemd) |
| `run --sim` | То же со встроенным симулятором шины |
| `discover` | Разовое сканирование устройств |
| `replicate --to PATH_OR_URL` | Инкрементальная выгрузка в резервную SQLite или PostgreSQL |
| `status` | Устройства и число сэмплов |

Как устроен один проход и как вызвать его из внешнего скрипта: [docs/replication.md](docs/replication.md).

Выгрузка в файл начиная с id и загрузка новых строк из этого файла:

```bash
python3 scripts/export_replication.py --database /tmp/tlmtry-primary.db --from-id 1 --output /tmp/tlmtry-batch.json
python3 scripts/import_replication.py --database /tmp/tlmtry-backup.db --input /tmp/tlmtry-batch.json
```

Виртуальные показания из CSV (каждая строка `unix_time; value;`, десятичная запятая допустима) пишутся в `samples` как обычные сэмплы и дальше реплицируются вместе с ними:

```bash
python3 scripts/import_virtual_readings.py \
  --database /var/lib/tlmtry/primary.db \
  --address 101 \
  --metric-id 0 \
  --create-virtual \
  --metric-name temp_c \
  --unit C \
  --every 5 \
  --input readings.csv
```

`--every` — период записи в секундах. В файл кладут только моменты, где значение меняется. Между этими метками скрипт пишет то же значение с шагом `--every` по оси времени файла. Первая ещё не записанная точка пишется сразу, дальше — пауза `--every` секунд на каждый шаг. После последней метки файла значение держится и записи продолжаются, пока процесс не остановят (`Ctrl+C` или `SIGTERM`).

Повторный запуск продолжает сетку с первой отсутствующей метки, без паузы на уже записанные шаги. `--once` вместо `--every` записывает только сами строки файла и выходит.

`--jitter 0.05` включает слабые колебания вокруг значения из файла: к нему прибавляется синусоида с амплитудой 0.05, полный период — 12 шагов. Без флага в базу пишется точное число. Колебания доступны только для метрик типа float.

`--create-virtual` заводит устройство и метрику типа float, если их ещё нет. Адрес лучше брать вне диапазона опроса сборщика (по умолчанию 1..100): следующий `DISCOVER` этого адреса заменяет каталог метрик. Для уже известной метрики флаг не нужен — тип берётся из каталога. Устройство от импорта не становится online; `last_seen` сдвигается только вперёд.

## Тесты

```bash
cd collector && pip install -e ".[dev]" && pytest -q
# опционально: TLMTRY_TEST_PG=postgresql://... pytest -q
```
