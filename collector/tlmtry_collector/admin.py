"""Local admin page for virtual devices, manual readings, and schedules.

Virtual samples are stored in the same tables as bus polls, with quality = 1.
A schedule writes samples on its own while this process is running: linear
transition between keyframes, then uniform noise of the given amplitude.
The page does not talk to the RS-485 bus.
"""

from __future__ import annotations

import html
import logging
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .protocol import DeviceManifest, MetricInfo, MetricType, SampleValue
from .schedule import Keyframe, VirtualSchedule, emit_due, interpolate
from .storage import DeviceRow, MetricCatalogRow, SampleRow, Storage

log = logging.getLogger(__name__)

VIRTUAL_QUALITY = 1
METRIC_SLOTS = 8
KEYFRAME_SLOTS = 8
MAX_BODY_BYTES = 65_536
NAME_LIMIT = 64
UNIT_LIMIT = 16
SCHEDULE_POLL_S = 0.5
_NUMERIC_TYPES = {MetricType.INT32, MetricType.FLOAT, MetricType.UINT16}
_VIEWS = ("sensors", "readings", "schedules")

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

    def save_schedule(self, form: dict[str, list[str]]) -> None:
        address = _parse_address(_first(form, "address"))
        metric = _require_numeric_metric(self.storage, address, _first(form, "metric_id"))
        step_s = _parse_step(_first(form, "step_s"))
        noise = _parse_noise(_first(form, "noise"))
        keyframes = _parse_keyframes(form)
        self.storage.replace_schedule(
            VirtualSchedule(
                address=address,
                metric_id=metric.metric_id,
                step_s=step_s,
                noise=noise,
                enabled=True,
                last_emit_ts=None,
                keyframes=keyframes,
            )
        )

    def stop_schedule(self, form: dict[str, list[str]]) -> None:
        address = _parse_address(_first(form, "address"))
        metric_id = _parse_metric_id(_first(form, "metric_id"), fallback=-1)
        self.storage.delete_schedule(address, metric_id)

    def emit_due(self, now: float | None = None) -> int:
        return emit_due(
            self.storage,
            now=time.time() if now is None else now,
            quality=VIRTUAL_QUALITY,
        )

    def render(self, *, notice: str = "", error: str = "", view: str = "sensors") -> str:
        devices = self.storage.list_devices()
        metrics = self.storage.list_metrics()
        samples = self.storage.list_recent_samples(limit=30)
        by_address: dict[int, list[MetricCatalogRow]] = {}
        for metric in metrics:
            by_address.setdefault(metric.address, []).append(metric)
        for rows in by_address.values():
            rows.sort(key=lambda m: m.metric_id)
        names = {(m.address, m.metric_id): m for m in metrics}
        schedules = self.storage.list_schedules()
        if view not in _VIEWS:
            view = "sensors"

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
            f'<body class="view-{view}">',
            '<div class="app">',
            "<aside class=\"side\">",
            '<div class="brand"><span class="mark">T</span><div><strong>tlmtry</strong>'
            "<span>админ-панель</span></div></div>",
            "<nav>",
            _nav_link("sensors", view, "Добавление виртуальных датчиков"),
            _nav_link("readings", view, "Добавление разовых показаний"),
            _nav_link(
                "schedules",
                view,
                "Добавление расписаний",
                count=len(schedules),
            ),
            "</nav>",
            '<p class="side-db">База '
            f"<code>{_esc(self.storage.location)}</code> "
            f"<span>{_esc(self.storage.backend)}</span></p>",
            "</aside>",
            "<main>",
        ]
        if notice:
            parts.append(f'<p class="banner ok" role="status">{_esc(notice)}</p>')
        if error:
            parts.append(f'<p class="banner error" role="alert">{_esc(error)}</p>')

        parts.append('<section class="panel panel-sensors">')
        parts.append(
            '<header class="panel-head"><h1>Виртуальные датчики</h1>'
            '<p class="hint">Адрес 1..100. Пустые строки метрик пропускаются. '
            "Поле id — это <code>metric_id</code> (0..255). "
            "Не занимайте адрес живого узла: следующий DISCOVER перезапишет манифест.</p>"
            "</header>"
        )
        parts.append('<section class="card" id="devices"><h2>Уже в базе</h2>')
        if not devices:
            parts.append('<p class="empty">Пока нет ни одного устройства.</p>')
        else:
            parts.append(
                '<div class="table-wrap"><table><thead><tr>'
                "<th>Адрес</th><th>Имя</th><th>Прошивка</th><th>Интервал, мс</th>"
                "<th>На связи</th><th>Последний раз</th><th>Метрики</th>"
                "</tr></thead><tbody>"
            )
            for device in devices:
                parts.append(_device_row(device, by_address.get(device.address, [])))
            parts.append("</tbody></table></div>")
        parts.append("</section>")

        parts.append('<section class="card" id="device-form"><h2>Новый датчик</h2>')
        parts.append('<form method="post" action="/devices" accept-charset="utf-8">')
        parts.append(
            '<div class="pair">'
            '<label>Адрес <input name="address" required inputmode="numeric"></label>'
            '<label>Имя <input name="name" required maxlength="64"></label>'
            "</div>"
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
        parts.append('<button class="primary" type="submit">Записать устройство</button></form>')
        parts.append("</section></section>")

        parts.append('<section class="panel panel-readings">')
        parts.append(
            '<header class="panel-head"><h1>Разовые показания</h1>'
            '<p class="hint">Строка пишется в ту же базу, что и опрос шины, '
            "с <code>quality = 1</code>. Пустое время значит «сейчас».</p></header>"
        )
        parts.append('<section class="card" id="readings"><h2>Доложить показание</h2>')
        if not devices:
            parts.append('<p class="empty">Сначала запишите устройство.</p>')
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
                '<button class="primary" type="submit">Записать показание</button>'
                "</form>"
            )
        parts.append("</section>")
        parts.append('<section class="card" id="samples"><h2>Последние показания</h2>')
        if not samples:
            parts.append('<p class="empty">Показаний ещё нет.</p>')
        else:
            parts.append(
                '<div class="table-wrap"><table><thead><tr>'
                "<th>id</th><th>Адрес</th><th>metric_id</th><th>Метрика</th><th>Значение</th>"
                "<th>Время</th><th>Источник</th>"
                "</tr></thead><tbody>"
            )
            for sample in samples:
                metric = names.get((sample.address, sample.metric_id))
                metric_label = metric.name if metric else "—"
                source = _source_label(sample.quality)
                source_class = "virtual" if sample.quality == VIRTUAL_QUALITY else "bus"
                parts.append(
                    "<tr>"
                    f"<td>{sample.id}</td>"
                    f"<td>{sample.address}</td>"
                    f"<td>{sample.metric_id}</td>"
                    f"<td>{_esc(metric_label)}</td>"
                    f"<td class=\"num\">{_esc(_format_value(sample))}</td>"
                    f"<td>{_esc(_format_ts(sample.ts))}</td>"
                    f"<td><span class=\"pill {source_class}\">{_esc(source)}</span></td>"
                    "</tr>"
                )
            parts.append("</tbody></table></div>")
        parts.append("</section></section>")
        parts.append('<section class="panel panel-schedules">')
        parts.append(
            '<header class="panel-head"><h1>Расписания</h1>'
            '<p class="hint">Пока панель запущена, показание пишется с заданным шагом. '
            "Между точками значение переходит линейно. До первой и после последней "
            "точки оно держится. Шум — случайная добавка в пределах ±. "
            "Время — локальное время машины, где запущена панель.</p></header>"
        )
        parts.extend(_schedule_sections(devices, by_address, schedules, names))
        parts.append("</section></main></div></body></html>")
        return "\n".join(parts)


def make_server(storage: Storage, host: str, port: int) -> ThreadingHTTPServer:
    app = AdminApp(storage)
    server = ThreadingHTTPServer((host, port), _handler_class(app))
    server.admin_app = app  # type: ignore[attr-defined]
    return server


def serve_admin(storage: Storage, host: str, port: int) -> None:
    server = make_server(storage, host, port)
    app: AdminApp = server.admin_app  # type: ignore[attr-defined]
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(SCHEDULE_POLL_S):
            try:
                with app.lock:
                    app.emit_due()
            except Exception:
                log.exception("virtual schedule tick failed")

    worker = threading.Thread(target=_loop, name="virtual-schedule", daemon=True)
    worker.start()
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
        stop.set()
        worker.join(timeout=2)
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
                    view=_first(query, "view"),
                )
            self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                form = self._read_form()
                with app.lock:
                    if parsed.path == "/devices":
                        app.save_device(form)
                        self._redirect(_done("sensors", "Устройство записано"))
                        return
                    if parsed.path == "/readings":
                        app.save_readings(form)
                        self._redirect(_done("readings", "Показание записано"))
                        return
                    if parsed.path == "/schedules":
                        app.save_schedule(form)
                        self._redirect(_done("schedules", "Расписание записано"))
                        return
                    if parsed.path == "/schedules/stop":
                        app.stop_schedule(form)
                        self._redirect(_done("schedules", "Расписание остановлено"))
                        return
                self._send(404, "not found\n", "text/plain; charset=utf-8")
            except FormError as exc:
                with app.lock:
                    page = app.render(error=exc.message, view=_view_for_path(parsed.path))
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


def _require_numeric_metric(storage: Storage, address: int, raw_id: str) -> MetricCatalogRow:
    metric_id = _parse_metric_id(raw_id, fallback=-1)
    devices = {d.address for d in storage.list_devices()}
    if address not in devices:
        raise FormError("устройство с таким адресом не зарегистрировано")
    for metric in storage.list_metrics():
        if metric.address == address and metric.metric_id == metric_id:
            try:
                kind = MetricType(metric.type)
            except ValueError as exc:
                raise FormError("метрика: неизвестный тип") from exc
            if kind not in _NUMERIC_TYPES:
                raise FormError("расписание доступно для числовых метрик")
            return metric
    raise FormError("метрика с таким id не зарегистрирована")


def _parse_step(raw: str) -> float:
    text = raw.strip().replace(",", ".")
    try:
        step = float(text)
    except ValueError as exc:
        raise FormError("шаг: нужно число секунд больше 0") from exc
    if not math.isfinite(step) or step <= 0:
        raise FormError("шаг: нужно число секунд больше 0")
    if step > 86400 * 30:
        raise FormError("шаг: не больше 30 суток")
    return step


def _parse_noise(raw: str) -> float:
    text = raw.strip().replace(",", ".")
    if not text:
        return 0.0
    try:
        noise = float(text)
    except ValueError as exc:
        raise FormError("шум: нужно неотрицательное число") from exc
    if not math.isfinite(noise) or noise < 0:
        raise FormError("шум: нужно неотрицательное число")
    return noise


def _parse_keyframes(form: dict[str, list[str]]) -> list[Keyframe]:
    stamps = form.get("kf_ts", [])
    values = form.get("kf_value", [])
    count = max(len(stamps), len(values))
    points: list[Keyframe] = []
    seen: set[float] = set()
    for index in range(count):
        ts_raw = stamps[index] if index < len(stamps) else ""
        value_raw = values[index] if index < len(values) else ""
        if ts_raw.strip() == "" and value_raw.strip() == "":
            continue
        if ts_raw.strip() == "" or value_raw.strip() == "":
            raise FormError("точка расписания: нужны и время, и значение")
        stamp = _parse_timestamp(ts_raw)
        if stamp is None:
            raise FormError("точка расписания: нужно время")
        text = value_raw.strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError as exc:
            raise FormError("точка расписания: значение должно быть числом") from exc
        if not math.isfinite(value):
            raise FormError("точка расписания: значение должно быть конечным числом")
        if stamp in seen:
            raise FormError("точки расписания: одинаковое время")
        seen.add(stamp)
        points.append(Keyframe(ts=stamp, value=value))
    if not points:
        raise FormError("нужна хотя бы одна точка расписания")
    points.sort(key=lambda item: item.ts)
    return points


def _nav_link(view: str, current: str, label: str, count: int | None = None) -> str:
    current_attr = ' aria-current="page"' if view == current else ""
    badge = f'<span class="badge">{count}</span>' if count else ""
    return (
        f'<a class="{"active" if view == current else ""}" '
        f'href="/?view={view}"{current_attr}><span>{_esc(label)}</span>{badge}</a>'
    )


def _done(view: str, message: str) -> str:
    return f"/?view={view}&ok={quote(message)}"


def _view_for_path(path: str) -> str:
    if path == "/readings":
        return "readings"
    if path in ("/schedules", "/schedules/stop"):
        return "schedules"
    return "sensors"


def _schedule_sections(
    devices: list[DeviceRow],
    by_address: dict[int, list[MetricCatalogRow]],
    schedules: list[VirtualSchedule],
    names: dict[tuple[int, int], MetricCatalogRow],
) -> list[str]:
    parts = ['<section class="card" id="schedule">', "<h2>Активные расписания</h2>"]
    if schedules:
        parts.append(
            '<div class="table-wrap"><table><thead><tr>'
            "<th>Адрес</th><th>Метрика</th><th>Шаг, с</th><th>Шум ±</th>"
            "<th>Сейчас без шума</th><th>Точки</th><th></th>"
            "</tr></thead><tbody>"
        )
        now = time.time()
        for schedule in schedules:
            metric = names.get((schedule.address, schedule.metric_id))
            label = metric.name if metric else str(schedule.metric_id)
            current = ""
            if schedule.keyframes:
                current = f"{interpolate(schedule.keyframes, now):.6g}"
            point_text = ", ".join(
                f"{_format_ts(point.ts)}→{point.value:.6g}" for point in schedule.keyframes
            )
            parts.append(
                "<tr>"
                f"<td>{schedule.address}</td>"
                f"<td>{_esc(label)} #{schedule.metric_id}</td>"
                f"<td>{_esc(f'{schedule.step_s:.6g}')}</td>"
                f"<td>{_esc(f'{schedule.noise:.6g}')}</td>"
                f"<td class=\"num\">{_esc(current)}</td>"
                f"<td>{_esc(point_text)}</td>"
                "<td>"
                '<form class="inline" method="post" action="/schedules/stop" accept-charset="utf-8">'
                f'<input type="hidden" name="address" value="{schedule.address}">'
                f'<input type="hidden" name="metric_id" value="{schedule.metric_id}">'
                '<button class="quiet" type="submit">Остановить</button>'
                "</form></td>"
                "</tr>"
            )
        parts.append("</tbody></table></div>")
    else:
        parts.append('<p class="empty">Активных расписаний нет.</p>')
    parts.append("</section>")
    parts.append('<section class="card"><h2>Новое расписание</h2>')

    numeric_found = False
    for device in devices:
        for metric in by_address.get(device.address, []):
            try:
                kind = MetricType(metric.type)
            except ValueError:
                continue
            if kind not in _NUMERIC_TYPES:
                continue
            numeric_found = True
            label = _LABEL_BY_TYPE.get(metric.type, str(metric.type))
            unit = f", {_esc(metric.unit)}" if metric.unit else ""
            parts.append('<form method="post" action="/schedules" accept-charset="utf-8">')
            parts.append(
                f"<h3>{_esc(device.address)} · {_esc(device.name)} · "
                f"{_esc(metric.name)} (id {_esc(metric.metric_id)}, {_esc(label)}{unit})</h3>"
                f'<input type="hidden" name="address" value="{device.address}">'
                f'<input type="hidden" name="metric_id" value="{metric.metric_id}">'
            )
            parts.append(
                '<div class="pair">'
                '<label>Шаг, секунды <input name="step_s" required inputmode="decimal"></label>'
                '<label>Шум ± <input name="noise" value="0" inputmode="decimal"></label>'
                "</div>"
            )
            parts.append('<div class="kf-head"><span>Время</span><span>Значение</span></div>')
            for _ in range(KEYFRAME_SLOTS):
                parts.append(
                    '<div class="kf-row">'
                    '<input type="datetime-local" name="kf_ts" step="1">'
                    '<input name="kf_value" inputmode="decimal">'
                    "</div>"
                )
            parts.append(
                '<button class="primary" type="submit">Записать расписание</button></form>'
            )
    if devices and not numeric_found:
        parts.append('<p class="empty">Числовых метрик нет — расписание не к чему привязать.</p>')
    elif not devices:
        parts.append('<p class="empty">Сначала запишите устройство.</p>')
    parts.append("</section>")
    return parts


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
    online_class = "on" if device.online else "off"
    return (
        "<tr>"
        f"<td>{device.address}</td>"
        f"<td>{_esc(device.name)}</td>"
        f"<td>{device.fw_major}.{device.fw_minor}</td>"
        f"<td>{device.poll_interval_ms}</td>"
        f'<td><span class="pill {online_class}">{online}</span></td>'
        f"<td>{_esc(seen)}</td>"
        f"<td>{_esc(metric_text)}</td>"
        "</tr>"
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
:root {
  --bg: #efeae2;
  --ink: #1e2a26;
  --muted: #5e6a64;
  --line: #e4dbd0;
  --card: #fffdf9;
  --accent: #1c6b5a;
  --side: #1c2824;
  --side-ink: #e8f0eb;
  --side-dim: #b7c4bb;
  --ok-bg: #e6f6ec;
  --ok-ink: #1a6840;
  --err-bg: #fdecea;
  --err-ink: #8d2f2f;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font-family: "Segoe UI", "Avenir Next", system-ui, sans-serif;
  line-height: 1.45;
}
.app { display: flex; min-height: 100vh; }
.side {
  width: 17.5rem;
  flex: none;
  background: var(--side);
  color: var(--side-ink);
  padding: 1.4rem 1rem 1.2rem;
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
}
.brand { display: flex; gap: 0.75rem; align-items: center; padding: 0 0.4rem 1.2rem; }
.mark {
  width: 2.2rem; height: 2.2rem; border-radius: 0.7rem;
  display: grid; place-items: center;
  background: #2f6f5c; color: white; font-weight: 700;
}
.brand strong { display: block; font-size: 1.05rem; letter-spacing: 0.01em; }
.brand span { color: var(--side-dim); font-size: 0.82rem; }
.side nav { display: flex; flex-direction: column; gap: 0.35rem; }
.side a {
  color: var(--side-ink);
  text-decoration: none;
  padding: 0.72rem 0.8rem;
  border-radius: 0.7rem;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.6rem;
}
.side a > span:first-child { flex: 1; }
.side a:hover { background: rgba(255, 255, 255, 0.06); }
.side a.active { background: #31584b; }
.badge {
  min-width: 1.4rem; text-align: center;
  background: #e7f3ee; color: #1c6b5a;
  border-radius: 999px; font-size: 0.75rem; padding: 0.05rem 0.4rem;
}
.side-db { margin-top: auto; color: #d5e0d8; font-size: 0.82rem; padding: 1rem 0.45rem 0.2rem; }
.side-db code { color: #f7fbf8; word-break: break-all; }
.side-db span { display: inline-block; margin-top: 0.25rem; }
main { flex: 1; padding: 1.6rem 1.8rem 2.5rem; max-width: 68rem; }
.panel { display: none; }
body.view-sensors .panel-sensors,
body.view-readings .panel-readings,
body.view-schedules .panel-schedules { display: block; }
.panel-head { margin: 0 0 1rem; }
.panel-head h1 { margin: 0 0 0.35rem; font-size: 1.7rem; font-weight: 650; }
.hint { margin: 0; color: var(--muted); max-width: 42rem; }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 1rem 1.15rem 1.15rem;
  margin: 0 0 1rem;
  box-shadow: 0 10px 28px rgba(48, 36, 22, 0.05);
}
.card h2 { margin: 0 0 0.8rem; font-size: 1.05rem; }
.card h3 { margin: 0.2rem 0 0.6rem; font-size: 1rem; }
.banner { border-radius: 10px; padding: 0.7rem 0.9rem; margin: 0 0 1rem; }
.banner.ok { background: var(--ok-bg); color: var(--ok-ink); }
.banner.error { background: var(--err-bg); color: var(--err-ink); }
.empty { color: var(--muted); margin: 0.2rem 0 0.4rem; }
code { font-family: ui-monospace, "Cascadia Code", monospace; font-size: 0.92em; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.55rem 0.6rem; vertical-align: top; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 0.78rem; letter-spacing: 0.03em; text-transform: uppercase; }
tr:last-child td { border-bottom: 0; }
td.num { font-variant-numeric: tabular-nums; }
form { margin: 0; }
form + form { margin-top: 1.1rem; padding-top: 1rem; border-top: 1px solid var(--line); }
form.inline { margin: 0; padding: 0; border: 0; }
label { display: block; margin: 0.55rem 0; color: var(--muted); font-size: 0.86rem; }
input, select {
  display: block; width: 100%; margin-top: 0.25rem;
  font: inherit; color: var(--ink); background: white;
  border: 1px solid #ddd4c8; border-radius: 8px; padding: 0.45rem 0.55rem;
}
input:focus, select:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(28, 107, 90, 0.16);
}
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; }
.metric-head, .kf-head {
  display: grid; gap: 0.45rem; margin: 0.8rem 0 0.3rem;
  color: var(--muted); font-size: 0.78rem; letter-spacing: 0.03em; text-transform: uppercase;
}
.metric-head, .metric-row { grid-template-columns: 4.5rem 1fr 8rem 8rem; }
.kf-head, .kf-row { grid-template-columns: 1fr 10rem; }
.metric-row, .kf-row { display: grid; gap: 0.45rem; margin: 0.3rem 0; }
.metric-row input, .metric-row select, .kf-row input { margin-top: 0; }
button {
  font: inherit; cursor: pointer; border-radius: 9px; padding: 0.55rem 0.95rem; margin-top: 0.85rem;
}
button.primary { background: var(--accent); color: white; border: 0; }
button.primary:hover { background: #17594b; }
button.quiet {
  margin-top: 0; background: transparent; color: #8a4034;
  border: 1px solid #ead5ce; padding: 0.35rem 0.65rem;
}
button.quiet:hover { background: #fdf4f2; }
.pill {
  display: inline-block; border-radius: 999px; padding: 0.08rem 0.5rem;
  font-size: 0.78rem; background: #f1ece4; color: var(--muted);
}
.pill.on, .pill.virtual { background: var(--ok-bg); color: var(--ok-ink); }
.pill.off, .pill.bus { background: #f3eee6; color: #6a6258; }
@media (max-width: 860px) {
  .app { display: block; }
  .side { position: static; width: auto; height: auto; }
  .side nav { flex-direction: row; overflow-x: auto; }
  .side a { min-width: 14rem; }
  .side-db { margin-top: 1rem; }
  main { padding: 1rem; }
  .pair, .metric-head, .metric-row { grid-template-columns: 1fr 1fr; }
  .metric-head span:nth-child(n+3) { display: none; }
}
"""
