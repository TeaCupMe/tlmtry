"""Local admin page for registering a virtual device and recording readings.

Virtual samples are stored in the same tables as bus polls, with quality = 1.
The page does not talk to the RS-485 bus and does not generate series itself.
"""

from __future__ import annotations

import html
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .protocol import DeviceManifest, MetricInfo, MetricType, SampleValue
from .storage import DeviceRow, MetricCatalogRow, SampleRow, Storage

VIRTUAL_QUALITY = 1
METRIC_SLOTS = 8
MAX_BODY_BYTES = 65_536
NAME_LIMIT = 64
UNIT_LIMIT = 16

_TYPE_BY_LABEL = {
    "i32": MetricType.INT32,
    "f32": MetricType.FLOAT,
    "bool": MetricType.BOOL,
    "u16": MetricType.UINT16,
}
_LABEL_BY_TYPE = {int(kind): label for label, kind in _TYPE_BY_LABEL.items()}


class FormError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AdminApp:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.lock = threading.RLock()

    def save_device(self, form: dict[str, list[str]]) -> None:
        address = _parse_address(_first(form, "address"))
        name = _clean_text(_first(form, "name"), NAME_LIMIT, "имя", allow_empty=False)
        metrics = _parse_metric_slots(form)
        if not metrics:
            raise FormError("нужна хотя бы одна метрика")

        existing = {d.address: d for d in self.storage.list_devices()}
        prev = existing.get(address)
        if prev is None:
            device_id, fw_major, fw_minor, poll_ms = address, 0, 0, 1000
        else:
            device_id = prev.device_id
            fw_major = prev.fw_major
            fw_minor = prev.fw_minor
            poll_ms = prev.poll_interval_ms

        manifest = DeviceManifest(
            device_id=device_id,
            fw_major=fw_major,
            fw_minor=fw_minor,
            name=name,
            poll_interval_ms=poll_ms,
            metrics=metrics,
            address=address,
        )
        self.storage.upsert_manifest(manifest)

    def save_readings(self, form: dict[str, list[str]]) -> None:
        address = _parse_address(_first(form, "address"))
        devices = {d.address: d for d in self.storage.list_devices()}
        if address not in devices:
            raise FormError("устройство с таким адресом не зарегистрировано")
        metrics = [
            m
            for m in self.storage.list_metrics()
            if m.address == address
        ]
        metrics.sort(key=lambda m: m.metric_id)
        if not metrics:
            raise FormError("у устройства нет метрик")

        stamp = _parse_timestamp(_first(form, "ts"))
        samples: list[SampleValue] = []
        for metric in metrics:
            raw = _first(form, f"value_{metric.metric_id}")
            if raw.strip() == "":
                raise FormError(f"метрика {metric.name}: нет значения")
            kind = _metric_kind(metric)
            samples.append(
                SampleValue(
                    metric_id=metric.metric_id,
                    type=kind,
                    value=_parse_reading(metric.name, kind, raw),
                )
            )
        self.storage.insert_samples(
            address, samples, ts=stamp, quality=VIRTUAL_QUALITY
        )

    def render(self, *, notice: str = "", error: str = "") -> str:
        devices = self.storage.list_devices()
        metrics = self.storage.list_metrics()
        samples = self.storage.list_recent_samples(limit=30)
        by_address: dict[int, list[MetricCatalogRow]] = {}
        for metric in metrics:
            by_address.setdefault(metric.address, []).append(metric)
        for rows in by_address.values():
            rows.sort(key=lambda m: m.metric_id)
        names = {(m.address, m.metric_id): m for m in metrics}

        parts = [
            "<!DOCTYPE html>",
            '<html lang="ru">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Tlmtry — виртуальные показания</title>",
            "<style>",
            _CSS,
            "</style>",
            "</head>",
            "<body>",
            "<h1>Виртуальные показания</h1>",
            "<p class=\"lede\">Запись идёт в ту же базу, что и у сборщика, "
            "с <code>quality = 1</code>. Опрос шины пишет <code>quality = 0</code>. "
            "Страница не обращается к RS-485 и сама ряды не генерирует.</p>",
            f"<p>База: <code>{_esc(self.storage.location)}</code> "
            f"({_esc(self.storage.backend)})</p>",
        ]
        if notice:
            parts.append(f'<p class="ok">{_esc(notice)}</p>')
        if error:
            parts.append(f'<p class="error">{_esc(error)}</p>')

        parts.append("<h2>Устройства</h2>")
        if not devices:
            parts.append("<p>Пока нет ни одного устройства.</p>")
        else:
            parts.append(
                "<table><thead><tr>"
                "<th>Адрес</th><th>Имя</th><th>Прошивка</th><th>Интервал, мс</th>"
                "<th>На связи</th><th>Последний раз</th><th>Метрики</th>"
                "</tr></thead><tbody>"
            )
            for device in devices:
                parts.append(_device_row(device, by_address.get(device.address, [])))
            parts.append("</tbody></table>")

        parts.append("<h2>Виртуальное устройство</h2>")
        parts.append(
            "<p>Адрес 1..100. Пустые строки метрик пропускаются. "
            "<code>metric_id</code> берётся из поля id (0..255), а не обнуляется. "
            "Не занимайте адрес живого узла: следующий DISCOVER перезапишет манифест.</p>"
        )
        parts.append('<form method="post" action="/devices" accept-charset="utf-8">')
        parts.append(
            '<label>Адрес <input name="address" required inputmode="numeric"></label>'
            '<label>Имя <input name="name" required maxlength="64"></label>'
        )
        parts.append(
            '<div class="metric-head"><span>id</span><span>Имя метрики</span>'
            "<span>Тип</span><span>Единица</span></div>"
        )
        for slot in range(METRIC_SLOTS):
            parts.append(
                '<div class="metric-row">'
                f'<input name="metric_id" inputmode="numeric" value="{slot}">'
                '<input name="metric_name" maxlength="64">'
                '<select name="metric_type">'
                '<option value="f32" selected>f32</option>'
                '<option value="i32">i32</option>'
                '<option value="bool">bool</option>'
                '<option value="u16">u16</option>'
                "</select>"
                '<input name="metric_unit" maxlength="16">'
                "</div>"
            )
        parts.append('<button type="submit">Записать устройство</button></form>')

        parts.append("<h2>Доложить показание</h2>")
        if not devices:
            parts.append("<p>Сначала запишите устройство.</p>")
        for device in devices:
            device_metrics = by_address.get(device.address, [])
            if not device_metrics:
                parts.append(
                    f"<p>Адрес {_esc(device.address)}: нет метрик, показание записать нельзя.</p>"
                )
                continue
            parts.append('<form method="post" action="/readings" accept-charset="utf-8">')
            parts.append(
                f"<h3>{_esc(device.address)} · {_esc(device.name)}</h3>"
                f'<input type="hidden" name="address" value="{device.address}">'
            )
            for metric in device_metrics:
                label = _LABEL_BY_TYPE.get(metric.type, str(metric.type))
                unit = f", {_esc(metric.unit)}" if metric.unit else ""
                parts.append(
                    "<label>"
                    f"{_esc(metric.name)} (id {_esc(metric.metric_id)}, {_esc(label)}{unit}) "
                    f'<input name="value_{metric.metric_id}" required>'
                    "</label>"
                )
            parts.append(
                '<label>Время (пусто — сейчас) '
                '<input type="datetime-local" name="ts" step="1"></label>'
                '<button type="submit">Записать показание</button>'
                "</form>"
            )

        parts.append("<h2>Последние показания</h2>")
        if not samples:
            parts.append("<p>Показаний ещё нет.</p>")
        else:
            parts.append(
                "<table><thead><tr>"
                "<th>id</th><th>Адрес</th><th>metric_id</th><th>Метрика</th><th>Значение</th>"
                "<th>Время</th><th>Источник</th>"
                "</tr></thead><tbody>"
            )
            for sample in samples:
                metric = names.get((sample.address, sample.metric_id))
                metric_label = metric.name if metric else "—"
                parts.append(
                    "<tr>"
                    f"<td>{sample.id}</td>"
                    f"<td>{sample.address}</td>"
                    f"<td>{sample.metric_id}</td>"
                    f"<td>{_esc(metric_label)}</td>"
                    f"<td>{_esc(_format_value(sample))}</td>"
                    f"<td>{_esc(_format_ts(sample.ts))}</td>"
                    f"<td>{_esc(_source_label(sample.quality))}</td>"
                    "</tr>"
                )
            parts.append("</tbody></table>")

        parts.append("</body></html>")
        return "\n".join(parts)


def make_server(storage: Storage, host: str, port: int) -> ThreadingHTTPServer:
    app = AdminApp(storage)
    return ThreadingHTTPServer((host, port), _handler_class(app))


def serve_admin(storage: Storage, host: str, port: int) -> None:
    server = make_server(storage, host, port)
    bound_host, bound_port = server.server_address[:2]
    print(
        f"admin: http://{bound_host}:{bound_port}  database: {storage.location}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_class(app: AdminApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/":
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            query = parse_qs(parsed.query, keep_blank_values=False)
            with app.lock:
                page = app.render(
                    notice=_first(query, "ok"),
                    error=_first(query, "err"),
                )
            self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                form = self._read_form()
                with app.lock:
                    if parsed.path == "/devices":
                        app.save_device(form)
                        self._redirect("/?ok=" + quote("Устройство записано"))
                        return
                    if parsed.path == "/readings":
                        app.save_readings(form)
                        self._redirect("/?ok=" + quote("Показание записано"))
                        return
                self._send(404, "not found\n", "text/plain; charset=utf-8")
            except FormError as exc:
                with app.lock:
                    page = app.render(error=exc.message)
                self._send(400, page, "text/html; charset=utf-8")
            except (UnicodeDecodeError, ValueError):
                self._send(400, "bad request\n", "text/plain; charset=utf-8")

        def _read_form(self) -> dict[str, list[str]]:
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" in ctype:
                raise FormError("ожидается форма application/x-www-form-urlencoded")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("bad content-length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise FormError("слишком большой запрос")
            raw = self.rfile.read(length) if length else b""
            text = raw.decode("utf-8")
            return parse_qs(text, keep_blank_values=True)

        def _redirect(self, location: str) -> None:
            body = b""
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, body: str, content_type: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

    return Handler


def _first(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key)
    if not values:
        return ""
    return values[0]


def _parse_address(raw: str) -> int:
    text = raw.strip()
    if not text.isdigit():
        raise FormError("адрес: нужно целое от 1 до 100")
    address = int(text)
    if not 1 <= address <= 100:
        raise FormError("адрес: нужно целое от 1 до 100")
    return address


def _clean_text(raw: str, limit: int, label: str, *, allow_empty: bool) -> str:
    value = raw.strip()
    if not value and allow_empty:
        return ""
    if not value:
        raise FormError(f"{label}: пустое значение")
    if len(value) > limit:
        raise FormError(f"{label}: длиннее {limit} символов")
    if any(ord(ch) < 32 for ch in value):
        raise FormError(f"{label}: недопустимые символы")
    return value


def _parse_metric_slots(form: dict[str, list[str]]) -> list[MetricInfo]:
    names = form.get("metric_name", [])
    types = form.get("metric_type", [])
    units = form.get("metric_unit", [])
    ids = form.get("metric_id", [])
    metrics: list[MetricInfo] = []
    seen: set[int] = set()
    count = max(len(names), len(types), len(units), len(ids))
    for index in range(count):
        name_raw = names[index] if index < len(names) else ""
        if name_raw.strip() == "":
            continue
        name = _clean_text(name_raw, NAME_LIMIT, "имя метрики", allow_empty=False)
        id_raw = ids[index] if index < len(ids) else ""
        metric_id = _parse_metric_id(id_raw, fallback=index)
        if metric_id in seen:
            raise FormError(f"metric_id {metric_id} повторяется")
        seen.add(metric_id)
        type_raw = (types[index] if index < len(types) else "").strip()
        kind = _TYPE_BY_LABEL.get(type_raw)
        if kind is None:
            raise FormError(f"метрика {name}: неизвестный тип")
        unit = _clean_text(
            units[index] if index < len(units) else "",
            UNIT_LIMIT,
            f"единица метрики {name}",
            allow_empty=True,
        )
        metrics.append(
            MetricInfo(metric_id=metric_id, type=kind, unit=unit, name=name)
        )
    return metrics


def _parse_metric_id(raw: str, *, fallback: int) -> int:
    text = raw.strip()
    if not text:
        value = fallback
    elif not text.isdigit():
        raise FormError("metric_id: нужно целое от 0 до 255")
    else:
        value = int(text)
    if not 0 <= value <= 255:
        raise FormError("metric_id: нужно целое от 0 до 255")
    return value


def _metric_kind(metric: MetricCatalogRow) -> MetricType:
    try:
        return MetricType(metric.type)
    except ValueError as exc:
        raise FormError(f"метрика {metric.name}: неизвестный тип в базе") from exc


def _parse_reading(name: str, kind: MetricType, raw: str) -> int | float | bool:
    text = raw.strip()
    if kind == MetricType.BOOL:
        if text not in ("0", "1"):
            raise FormError(f"метрика {name}: нужно 0 или 1")
        return text == "1"
    if kind == MetricType.UINT16:
        if not text.isdigit() or not 0 <= int(text) <= 65535:
            raise FormError(f"метрика {name}: целое от 0 до 65535")
        return int(text)
    if kind == MetricType.INT32:
        if not _is_int_token(text):
            raise FormError(f"метрика {name}: целое от -2147483648 до 2147483647")
        value = int(text)
        if not -2147483648 <= value <= 2147483647:
            raise FormError(f"метрика {name}: целое от -2147483648 до 2147483647")
        return value
    if kind == MetricType.FLOAT:
        try:
            value = float(text)
        except ValueError as exc:
            raise FormError(f"метрика {name}: нужно конечное число") from exc
        if not math.isfinite(value):
            raise FormError(f"метрика {name}: нужно конечное число")
        return value
    raise FormError(f"метрика {name}: неизвестный тип")


def _is_int_token(text: str) -> bool:
    if text.startswith("-"):
        return text[1:].isdigit() and text != "-"
    return text.isdigit()


def _parse_timestamp(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    if "T" in text or "-" in text:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return time.mktime(time.strptime(text, fmt))
            except ValueError:
                continue
        raise FormError("метка времени: ожидается ГГГГ-ММ-ДДTЧЧ:ММ")
    try:
        stamp = float(text)
    except ValueError as exc:
        raise FormError("метка времени: ожидается ГГГГ-ММ-ДДTЧЧ:ММ") from exc
    if not math.isfinite(stamp):
        raise FormError("метка времени: некорректное значение")
    return stamp


def _format_value(sample: SampleRow) -> str:
    if sample.value_text:
        return sample.value_text
    if sample.value_real is None:
        return ""
    if sample.type == int(MetricType.FLOAT):
        return f"{sample.value_real:.6g}"
    if sample.value_real == int(sample.value_real):
        return str(int(sample.value_real))
    return str(sample.value_real)


def _format_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return str(ts)


def _source_label(quality: int) -> str:
    if quality == VIRTUAL_QUALITY:
        return "виртуальное"
    if quality == 0:
        return "опрос"
    return str(quality)


def _device_row(device: DeviceRow, metrics: list[MetricCatalogRow]) -> str:
    seen = _format_ts(device.last_seen) if device.last_seen else "—"
    metric_text = ", ".join(
        f"{m.name} #{m.metric_id} ({_LABEL_BY_TYPE.get(m.type, m.type)}"
        + (f", {m.unit}" if m.unit else "")
        + ")"
        for m in metrics
    )
    online = "да" if device.online else "нет"
    return (
        "<tr>"
        f"<td>{device.address}</td>"
        f"<td>{_esc(device.name)}</td>"
        f"<td>{device.fw_major}.{device.fw_minor}</td>"
        f"<td>{device.poll_interval_ms}</td>"
        f"<td>{online}</td>"
        f"<td>{_esc(seen)}</td>"
        f"<td>{_esc(metric_text)}</td>"
        "</tr>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
body { font-family: sans-serif; max-width: 60rem; margin: 2rem auto; padding: 0 1rem; color: #1c1c1c; }
h1 { margin-bottom: 0.4rem; }
.lede { color: #333; }
code { font-family: ui-monospace, monospace; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }
th, td { border-bottom: 1px solid #ddd; text-align: left; padding: 0.35rem 0.45rem; vertical-align: top; }
form { margin: 0 0 1.4rem; padding: 0.8rem 0; border-top: 1px solid #eee; }
label { display: block; margin: 0.45rem 0; }
input, select, button { font: inherit; padding: 0.25rem 0.4rem; }
button { margin-top: 0.6rem; }
.metric-head, .metric-row { display: grid; grid-template-columns: 4.5rem 1fr 8rem 8rem; gap: 0.4rem; margin: 0.25rem 0; }
.ok { background: #e7f6e7; padding: 0.5rem 0.7rem; }
.error { background: #fde8e8; padding: 0.5rem 0.7rem; }
"""
