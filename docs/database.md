# База телеметрии

Сборщик пишет в одну базу: файл SQLite или базу PostgreSQL. Схема одинаковая, её создаёт `Storage` при открытии (`CREATE TABLE IF NOT EXISTS` в `collector/tlmtry_collector/storage.py`). Повторный запуск сборщика таблицы не удаляет и не мигрирует.

Подключение задаётся полем `database` в YAML или флагом `--database`:

| Значение | Движок |
|----------|--------|
| путь к файлу, например `/var/lib/tlmtry/primary.db` | SQLite (WAL) |
| URL `postgres://` или `postgresql://` | PostgreSQL |

Локальный PostgreSQL из `scripts/up_postgres.sh` по умолчанию: `postgresql://tlmtry:tlmtry@127.0.0.1:5432/tlmtry`. Писать нужно в primary. Резервная база — копия, её обновляет `tlmtry-collector replicate`.

Четыре таблицы: `devices`, `metrics`, `samples`, `replication_state`. Рядом можно завести свои таблицы: сборщик их не трогает и не реплицирует.

## Как это связано

```
devices (address PK)
   │
   ├── metrics (address, metric_id)  — текущий каталог имён
   │
   └── samples (address, metric_id, ts)  — история, только дописывается
```

`samples` не имеет внешнего ключа ни на `devices`, ни на `metrics`. Строка сэмпла живёт, даже если каталог метрики потом перезаписали. `metrics` ссылается на `devices(address)`: в PostgreSQL это проверяется, в SQLite внешние ключи не включены (`PRAGMA foreign_keys` сборщик не ставит).

Один опрос устройства — несколько строк `samples` с одним и тем же `ts`. Имена метрик в сэмпл не копируются, их нужно брать из `metrics`.

## `devices`

Текущее состояние узла. Одна строка на адрес.

| Колонка | Тип | Смысл |
|---------|-----|--------|
| `address` | INTEGER, PK | Адрес на шине. Протокол использует 1..100, в базе ограничения нет |
| `device_id` | INTEGER | Идентификатор из прошивки (`u16`), это не адрес |
| `name` | TEXT | Имя из манифеста |
| `fw_major`, `fw_minor` | INTEGER | Версия прошивки |
| `poll_interval_ms` | INTEGER | Период опроса. Если в конфиге есть `interval_overrides` для адреса, в базу пишется он, а не значение с устройства |
| `last_seen` | REAL / DOUBLE PRECISION | Unix-время последнего успешного discover или чтения, секунды с дробной частью (`time.time()`). NULL, пока успехов не было |
| `online` | INTEGER | `1` — последний обмен успешен, `0` — нет. Это не булев тип SQL |
| `last_error` | TEXT | Текст последней ошибки опроса. NULL после успеха |

Успешный `DISCOVER` делает upsert по `address` и выставляет `online = 1`, `last_error = NULL`. Неуспешный `READ` ставит `online = 0` и пишет `last_error`, `last_seen` при этом не двигается.

## `metrics`

Каталог метрик, который устройство отдало на последнем `DISCOVER`. Ключ — `(address, metric_id)`.

| Колонка | Тип | Смысл |
|---------|-----|--------|
| `address` | INTEGER, FK → `devices.address` | Адрес узла |
| `metric_id` | INTEGER | Номер метрики внутри узла. С шины приходит `u8` (0..255) |
| `name` | TEXT | Имя, например `temp_c` |
| `type` | INTEGER | Код типа, см. таблицу ниже |
| `unit` | TEXT | Единица (`C`, `mV`). Пустая строка, если единицы нет |

Перед записью нового манифеста сборщик удаляет все строки `metrics` этого адреса и вставляет список заново. Старые `samples` этого адреса остаются. Если прошивка переиспользовала `metric_id` под другим именем, история в `samples` смешается: у сэмпла нет своего имени.

## `samples`

История значений. Строки только добавляются, сборщик их не обновляет и не чистит.

| Колонка | SQLite | PostgreSQL | Смысл |
|---------|--------|------------|--------|
| `id` | INTEGER PK AUTOINCREMENT | BIGSERIAL PK | Монотонный идентификатор. Репликация идёт по нему |
| `address` | INTEGER | INTEGER | Адрес источника |
| `metric_id` | INTEGER | INTEGER | Номер метрики |
| `ts` | REAL | DOUBLE PRECISION | Unix-время замера, секунды. Общее на все метрики одного опроса |
| `value_real` | REAL | DOUBLE PRECISION | Число, если тип числовой или bool |
| `value_text` | TEXT | TEXT | Текст для bool; NULL для чисел |
| `type` | INTEGER | INTEGER | Тип на момент записи. Может разойтись с текущей строкой `metrics` |
| `quality` | INTEGER, default 0 | INTEGER, default 0 | Сборщик всегда пишет `0` и нигде его не интерпретирует. Репликация копирует как есть |

Индекс `idx_samples_addr_ts` по `(address, ts)`. Уникальности по `(address, metric_id, ts)` нет: два замера в одну и ту же секунду — две строки.

Коды `type` совпадают с протоколом (`MetricType`):

| `type` | Имя | `value_real` | `value_text` |
|--------|-----|--------------|--------------|
| 0 | INT32 | число | NULL |
| 1 | FLOAT | число | NULL |
| 2 | BOOL | `1.0` или `0.0` | `true` или `false` |
| 3 | UINT16 | число | NULL |

Читать значение так: для типа 2 смотреть `value_text` (или `value_real <> 0`), для остальных — `value_real`.

Пример выборки:

```sql
SELECT s.ts, s.metric_id, m.name, m.unit, s.type, s.value_real, s.value_text, s.quality
FROM samples AS s
LEFT JOIN metrics AS m
  ON m.address = s.address AND m.metric_id = s.metric_id
WHERE s.address = 1
ORDER BY s.ts, s.metric_id;
```

`LEFT JOIN`, потому что каталог могли перезаписать и строка в `metrics` уже другая или её нет.

## `replication_state`

Пара ключ–значение, оба поля TEXT.

| `key` | Где | `value` |
|-------|-----|---------|
| `samples_id` | primary | Последний `samples.id`, уже скопированный в backup. Нет строки — ноль |
| `last_replicate_ts` | backup | Unix-время последнего применения реплики |

Репликация копирует все `devices`, все `metrics` и `samples` с `id` больше watermark. На backup вставка сэмпла идёт с явным `id` и игнорирует конфликт (`ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`). После неё последовательность PostgreSQL сдвигается на `MAX(id)`.

## Что делает сборщик с уже лежащими строками

Планировщик опрашивает **каждую** строку `devices`, а не только адреса из `address_min`..`address_max`. Адрес без записи в расписании опрашивается сразу. Неответ помечает узел offline (`last_error = 'read timeout/nack'`) и держит шину на время таймаута (по умолчанию 200 мс). Поэтому посторонняя строка в `devices` замедляет сбор.

`DISCOVER` перезаписывает `devices` и заново собирает `metrics` только для адресов, которые ответили на шине (диапазон сканирования, по умолчанию 1..100). Чужой адрес вне этого диапазона discover не трогает, но цикл опроса — трогает, если строка в `devices` есть.

`samples` и `replication_state` опрос не чистит.

## Куда класть другие данные

### Свои таблицы в той же базе

Подходит, если данные не являются метрикой узла на шине: журнал эксперимента, ручные отметки, внешний датчик.

Сборщик создаёт только свои четыре таблицы и больше ничего в базе не удаляет. Новая таблица переживёт перезапуск. `replicate` её не копирует — на backup её нужно вести отдельно.

Имеет смысл держать время в том же виде, что `samples.ts`: число секунд Unix, не `timestamptz`. Тогда ряды можно сопоставлять без преобразования.

```sql
CREATE TABLE IF NOT EXISTS lab_events (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    name TEXT NOT NULL,
    value_real DOUBLE PRECISION,
    value_text TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_lab_events_ts ON lab_events(ts);
```

В SQLite тот же смысл: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `ts REAL`, `value_real REAL`.

### Строки в `samples`

Подходит, если нужен тот же ряд, что у телеметрии, и чтобы репликация унесла его на backup.

Писать в primary. Колонку `id` не задавать: её выдаст AUTOINCREMENT / BIGSERIAL. Явный `id` на primary ломает догоняющую репликацию, на backup — может занять номер будущего сэмпла с шины (`DO NOTHING` оставит вашу строку и молча отбросит настоящую).

`replication_state` не менять. Ключ `samples_id` сдвинет сам сборщик, когда скопирует новые id.

Пока процесс `tlmtry-collector run` запущен, не вставляйте строку в `devices`: её начнут опрашивать по RS-485. Каталог `metrics` для адреса с шины тоже не стоит дополнять — следующий успешный discover его сотрёт. Сэмпл без пары в `metrics` допустим, выборка выше как раз на это рассчитана.

Договорённость для таких строк:

- `address` — адрес реального узла, к которому относится величина, либо свободное число вне диапазона сканирования, если ряда в `devices` нет;
- `metric_id` — номер, которого нет в манифесте этого узла (для узла с шины разумно `>= 256`, прошивочные id занимают 0..255);
- `type`, `value_real`, `value_text` — как в таблице типов выше;
- `quality` — не `0`. Сборщик ноль не читает, но по ненулевому коду ваш ряд отличают от опроса шины. Код выберите сами и держите постоянным, например `1` для ручного ввода.

Один замер — одна транзакция, один `ts` на все метрики этого замера.

PostgreSQL:

```sql
INSERT INTO samples (address, metric_id, ts, value_real, value_text, type, quality)
VALUES
    (1, 256, EXTRACT(EPOCH FROM clock_timestamp()), 21.5, NULL, 1, 1);
```

SQLite (`ts` — секунды Unix):

```sql
INSERT INTO samples (address, metric_id, ts, value_real, value_text, type, quality)
VALUES (1, 256, strftime('%s','now'), 21.5, NULL, 1, 1);
```

Тот же путь через API сборщика, если каталог и узел заводить можно (сборщик в этот момент не запущен, иначе адрес попадёт в опрос):

```python
import time

from tlmtry_collector.protocol import (
    DeviceManifest,
    MetricInfo,
    MetricType,
    SampleValue,
)
from tlmtry_collector.storage import Storage

db = "postgresql://tlmtry:tlmtry@127.0.0.1:5432/tlmtry"  # или путь к файлу SQLite
storage = Storage(db)
try:
    storage.upsert_manifest(
        DeviceManifest(
            address=201,
            device_id=0,
            name="lab-import",
            fw_major=0,
            fw_minor=0,
            poll_interval_ms=0,
            metrics=[MetricInfo(0, MetricType.FLOAT, "C", "manual_temp_c")],
        )
    )
    storage.insert_samples(201, [SampleValue(0, MetricType.FLOAT, 21.5)], ts=time.time())
finally:
    storage.close()
```

`insert_samples` само раскладывает значение по `value_real` / `value_text` и ставит `quality = 0`. Ненулевой `quality` этим методом не задаётся — для метки источника нужен прямой `INSERT`. `upsert_manifest` ещё и удалит прежние `metrics` адреса 201.

SQLite в сборщике открыт в режиме WAL, таймаут ожидания блокировки не выставлен. Пишите из другого процесса короткой транзакцией. Если соединение получит `database is locked`, откройте базу с `timeout` в несколько секунд и повторите. У PostgreSQL одновременная вставка в `samples` нормальна.

Учётная запись для записи — владелец базы из URL (`tlmtry` у `scripts/up_postgres.sh`). `scripts/init_readonly_user.sql` заводит отдельный логин только на `SELECT`; этим логином скрипты загрузки не подключаются.
