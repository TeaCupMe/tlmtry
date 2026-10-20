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

PostgreSQL  -->  Grafana (отдельный контейнер, показания всех датчиков)
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

## Дашборд (Grafana)

Отдельный контейнер читает ту же PostgreSQL и показывает **все** строки `samples`, не только адреса из `devices`. Другие скрипты пишут в ту же таблицу.

- адрес есть в `devices` — источник `collector`, имя и единица берутся из `metrics`
- адреса в `devices` нет — источник `uncatalogued`, на графике это `address N / metric M`

```bash
bash scripts/up_postgres.sh
bash scripts/up_dashboard.sh
# http://127.0.0.1:3000  — смотреть можно без входа
```

Два экрана, переключение в шапке Grafana:

- `/d/tlmtry-sensors` — общий график, последние значения и статус устройств
- `/d/tlmtry-tiles` — каждый показатель в своём окне, все сразу

Пример для стороннего скрипта (время — unix-секунды, как у сборщика; `type` как в протоколе: 0 = i32, 1 = f32, 2 = bool, 3 = u16):

```sql
INSERT INTO samples (address, metric_id, ts, value_real, value_text, type, quality)
VALUES (
    250,
    1,
    EXTRACT(EPOCH FROM clock_timestamp()),
    1.23,
    NULL,
    1,
    0
);
```

Новая пара address/metric сама появляется в фильтрах. Представление, которое читает Grafana, — `v_sensor_readings`. Схема ставится при `up_postgres.sh` и повторно при `up_dashboard.sh`.

Разные единицы измерения рисуются на одной оси. В легенде единица указана в имени ряда, фильтр «Датчик» оставляет сопоставимые графики.

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

## Тесты

```bash
cd collector && pip install -e ".[dev]" && pytest -q
# опционально: TLMTRY_TEST_PG=postgresql://... pytest -q
```
