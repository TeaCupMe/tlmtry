"""Background RS-485 session for the sensor debug window.

All bus I/O happens on one worker thread. The GUI (or a test) posts commands
and reads immutable snapshots back from the event queue.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import DeviceManifest, MetricType
from .serial_bus import Bus, SerialTransport

TYPE_LABELS = {
    MetricType.INT32: "i32",
    MetricType.FLOAT: "f32",
    MetricType.BOOL: "bool",
    MetricType.UINT16: "u16",
}


class ScanCancelled(Exception):
    """Raised from the discover progress callback when the scan should stop."""


def format_metric_value(metric_type: MetricType, value: Any) -> str:
    if metric_type == MetricType.BOOL:
        return "true" if value else "false"
    if metric_type == MetricType.FLOAT:
        return f"{float(value):.6g}"
    return str(int(value))


def list_serial_ports() -> list[tuple[str, str]]:
    """Return ``(device, label)`` for ports pyserial can see."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    found: list[tuple[str, str]] = []
    for port in sorted(list_ports.comports(), key=lambda item: item.device):
        description = (port.description or "").strip()
        if description and description.lower() not in {"n/a", "unknown"}:
            label = f"{port.device} — {description}"
        else:
            label = port.device
        found.append((port.device, label))
    return found


@dataclass(frozen=True)
class BusSettings:
    port: str = ""
    baudrate: int = 115200
    sim: bool = False
    address_min: int = 1
    address_max: int = 100
    response_timeout_ms: int = 200
    probe_timeout_ms: int = 50
    poll_interval_ms: int = 500
    unicast_fill: bool = True


@dataclass(frozen=True)
class MetricSnapshot:
    metric_id: int
    name: str
    type_name: str
    unit: str
    value: str


@dataclass(frozen=True)
class DeviceSnapshot:
    address: int
    device_id: int
    name: str
    firmware: str
    poll_interval_ms: int
    link: str
    status: str
    preview: str
    metrics: tuple[MetricSnapshot, ...]


@dataclass(frozen=True)
class SessionEvent:
    kind: str
    text: str = ""
    flag: bool = False
    devices: tuple[DeviceSnapshot, ...] = ()


@dataclass
class _DeviceState:
    manifest: DeviceManifest
    values: dict[int, Any] = field(default_factory=dict)
    link: str = "found"
    updated_at: float | None = None


def _clamp_settings(settings: BusSettings) -> BusSettings:
    address_min = min(100, max(1, int(settings.address_min)))
    address_max = min(100, max(address_min, int(settings.address_max)))
    return BusSettings(
        port=settings.port.strip(),
        baudrate=max(1, int(settings.baudrate)),
        sim=bool(settings.sim),
        address_min=address_min,
        address_max=address_max,
        response_timeout_ms=max(20, int(settings.response_timeout_ms)),
        probe_timeout_ms=max(10, int(settings.probe_timeout_ms)),
        poll_interval_ms=max(100, int(settings.poll_interval_ms)),
        unicast_fill=bool(settings.unicast_fill),
    )


class DebugSession:
    def __init__(self) -> None:
        self._inbox: queue.Queue[tuple] = queue.Queue()
        self._events: queue.Queue[SessionEvent] = queue.Queue()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._bus: Bus | None = None
        self._devices: dict[int, _DeviceState] = {}
        self._settings = BusSettings()
        self._polling = False
        self._poll_interval_s = 0.5
        self._last_poll = 0.0
        self._busy = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tlmtry-debug", daemon=True)
        self._thread.start()

    def connect(self, settings: BusSettings) -> None:
        self._cancel.clear()
        self._inbox.put(("connect", _clamp_settings(settings)))

    def disconnect(self) -> None:
        self._cancel.set()
        self._inbox.put(("disconnect",))

    def discover(self, settings: BusSettings) -> None:
        self._cancel.clear()
        self._inbox.put(("discover", _clamp_settings(settings)))

    def read_once(self) -> None:
        self._inbox.put(("read",))

    def set_polling(self, enabled: bool, interval_ms: int) -> None:
        self._inbox.put(("polling", bool(enabled), max(100, int(interval_ms))))

    def try_get_event(self) -> SessionEvent | None:
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._cancel.set()
        self._stop.set()
        self._inbox.put(("stop",))
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=8)

    def wait_for(self, kind: str, timeout: float = 5.0) -> SessionEvent:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                event = self._events.get(timeout=min(0.1, max(0.0, remaining)))
            except queue.Empty:
                continue
            seen.append(event.kind)
            if event.kind == kind:
                return event
        raise TimeoutError(f"no {kind!r} event, saw {seen}")

    def _emit(self, event: SessionEvent) -> None:
        self._events.put(event)

    def _loop(self) -> None:
        while not self._stop.is_set():
            timeout = 0.05
            if self._polling and self._bus is not None and self._devices and not self._busy:
                remaining = self._poll_interval_s - (time.monotonic() - self._last_poll)
                if remaining > 0:
                    timeout = min(0.05, remaining)
                else:
                    timeout = 0.0
            try:
                command = self._inbox.get(timeout=timeout if timeout > 0 else 0.05)
            except queue.Empty:
                command = None
            if command is not None:
                try:
                    if not self._dispatch(command):
                        break
                except Exception as exc:
                    self._busy = False
                    self._emit(SessionEvent(kind="busy", flag=False))
                    self._emit(SessionEvent(kind="log", text=f"Сбой сессии: {exc}"))
                    self._emit(SessionEvent(kind="status", text=f"Сбой сессии: {exc}"))
            if (
                self._polling
                and not self._busy
                and self._bus is not None
                and self._devices
                and not self._cancel.is_set()
                and time.monotonic() - self._last_poll >= self._poll_interval_s
            ):
                try:
                    self._read_all(announce=False)
                except Exception as exc:
                    self._emit(SessionEvent(kind="log", text=f"Ошибка опроса: {exc}"))
                self._last_poll = time.monotonic()
        self._close_bus()

    def _dispatch(self, command: tuple) -> bool:
        kind = command[0]
        if kind == "stop":
            return False
        if kind == "connect":
            self._handle_connect(command[1])
        elif kind == "disconnect":
            self._handle_disconnect()
        elif kind == "discover":
            self._handle_discover(command[1])
        elif kind == "read":
            self._read_all(announce=True)
        elif kind == "polling":
            self._handle_polling(command[1], command[2])
        return not self._stop.is_set()

    def _handle_connect(self, settings: BusSettings) -> None:
        self._close_bus()
        self._devices.clear()
        self._publish()
        self._settings = settings
        self._polling = False
        try:
            if settings.sim:
                from .sim import QueueTransport, demo_devices

                transport = QueueTransport(demo_devices())
            else:
                if not settings.port:
                    raise ValueError("не задан COM-порт")
                transport = SerialTransport(
                    port=settings.port,
                    baudrate=settings.baudrate,
                    timeout=0.05,
                )
            self._bus = Bus(
                transport,
                response_timeout_s=settings.response_timeout_ms / 1000.0,
                probe_timeout_s=settings.probe_timeout_ms / 1000.0,
            )
        except Exception as exc:
            self._bus = None
            self._emit(SessionEvent(kind="log", text=f"Не удалось открыть порт: {exc}"))
            self._emit(SessionEvent(kind="status", text=f"Ошибка порта: {exc}"))
            self._emit(SessionEvent(kind="connected", flag=False))
            return
        if settings.sim:
            where = "симулятор"
        else:
            where = f"{settings.port} @ {settings.baudrate}"
        self._emit(SessionEvent(kind="log", text=f"Подключено: {where}"))
        self._emit(SessionEvent(kind="status", text=f"Подключено: {where}"))
        self._emit(SessionEvent(kind="connected", flag=True))

    def _handle_disconnect(self) -> None:
        self._polling = False
        self._close_bus()
        self._devices.clear()
        self._publish()
        self._cancel.clear()
        self._emit(SessionEvent(kind="log", text="Порт закрыт"))
        self._emit(SessionEvent(kind="status", text="Отключено"))
        self._emit(SessionEvent(kind="connected", flag=False))
        self._emit(SessionEvent(kind="polling", flag=False))

    def _handle_discover(self, settings: BusSettings) -> None:
        if self._bus is None:
            self._emit(SessionEvent(kind="status", text="Сначала подключитесь к порту"))
            return
        if self._cancel.is_set() or self._stop.is_set():
            return
        self._settings = settings
        self._bus.response_timeout_s = settings.response_timeout_ms / 1000.0
        self._bus.probe_timeout_s = settings.probe_timeout_ms / 1000.0
        unicast = settings.unicast_fill and not settings.sim
        self._busy = True
        self._emit(SessionEvent(kind="busy", flag=True))
        self._emit(
            SessionEvent(
                kind="status",
                text=f"Сканирование адресов {settings.address_min}–{settings.address_max}…",
            )
        )
        try:
            manifests = self._bus.discover_scan(
                address_min=settings.address_min,
                address_max=settings.address_max,
                use_broadcast=True,
                unicast_fill=unicast,
                on_progress=self._on_progress,
            )
        except ScanCancelled:
            self._emit(SessionEvent(kind="status", text="Сканирование прервано"))
            return
        except Exception as exc:
            self._emit(SessionEvent(kind="log", text=f"Discover: {exc}"))
            self._emit(SessionEvent(kind="status", text=f"Ошибка discover: {exc}"))
            return
        finally:
            self._busy = False
            self._emit(SessionEvent(kind="busy", flag=False))
        found: dict[int, _DeviceState] = {}
        for manifest in manifests:
            previous = self._devices.get(manifest.address)
            if previous is None:
                found[manifest.address] = _DeviceState(manifest=manifest)
            else:
                previous.manifest = manifest
                found[manifest.address] = previous
        self._devices = found
        self._publish()
        if manifests:
            summary = ", ".join(f"{item.address}:{item.name}" for item in manifests)
        else:
            summary = "пусто"
        mode = "broadcast" if not unicast else "broadcast + unicast"
        self._emit(
            SessionEvent(
                kind="log",
                text=f"Найдено {len(manifests)} ({mode}): {summary}",
            )
        )
        self._emit(
            SessionEvent(kind="status", text=f"Найдено устройств: {len(manifests)}")
        )

    def _on_progress(self, token: str) -> None:
        if self._stop.is_set() or self._cancel.is_set():
            raise ScanCancelled()
        if token == "broadcast":
            text = "DISCOVER: широковещательный запрос…"
        elif token.startswith("unicast:"):
            _, addr, index, total = token.split(":")
            text = f"DISCOVER: адрес {addr} ({index}/{total})…"
        else:
            text = token
        self._emit(SessionEvent(kind="status", text=text))

    def _handle_polling(self, enabled: bool, interval_ms: int) -> None:
        self._poll_interval_s = max(0.1, interval_ms / 1000.0)
        if enabled and self._bus is None:
            self._polling = False
            self._emit(SessionEvent(kind="status", text="Сначала подключитесь к порту"))
            self._emit(SessionEvent(kind="polling", flag=False))
            return
        if enabled and not self._devices:
            self._polling = False
            self._emit(SessionEvent(kind="status", text="Нет устройств — сначала Discover"))
            self._emit(SessionEvent(kind="polling", flag=False))
            return
        self._polling = enabled
        self._last_poll = 0.0
        if enabled:
            text = f"Опрос каждые {interval_ms} мс"
        else:
            text = "Опрос выключен"
        self._emit(SessionEvent(kind="status", text=text))
        self._emit(SessionEvent(kind="polling", flag=enabled))

    def _read_all(self, *, announce: bool) -> None:
        if self._bus is None:
            if announce:
                self._emit(SessionEvent(kind="status", text="Сначала подключитесь к порту"))
            return
        if not self._devices:
            if announce:
                self._emit(SessionEvent(kind="status", text="Нет устройств — сначала Discover"))
            return
        failures = 0
        for address in list(self._devices):
            if self._cancel.is_set() or self._stop.is_set():
                break
            state = self._devices[address]
            try:
                samples = self._bus.read_metrics(address)
            except Exception as exc:
                samples = None
                detail = str(exc)
            else:
                detail = ""
            if samples is None:
                failures += 1
                if state.link != "offline":
                    extra = f": {detail}" if detail else ""
                    self._emit(
                        SessionEvent(
                            kind="log",
                            text=f"addr {address}: нет ответа{extra}",
                        )
                    )
                state.link = "offline"
                continue
            state.link = "online"
            state.updated_at = time.time()
            state.values = {
                sample.metric_id: (sample.type, sample.value) for sample in samples
            }
        self._publish()
        if announce or failures:
            if failures:
                text = f"Нет ответа от {failures} из {len(self._devices)}"
            else:
                text = f"Прочитано устройств: {len(self._devices)}"
            self._emit(SessionEvent(kind="status", text=text))

    def _publish(self) -> None:
        self._emit(SessionEvent(kind="devices", devices=tuple(self._snapshots())))

    def _snapshots(self) -> list[DeviceSnapshot]:
        snapshots: list[DeviceSnapshot] = []
        for address in sorted(self._devices):
            state = self._devices[address]
            manifest = state.manifest
            metrics: list[MetricSnapshot] = []
            for info in manifest.metrics:
                stored = state.values.get(info.metric_id)
                if stored is None:
                    shown = ""
                else:
                    metric_type, raw = stored
                    shown = format_metric_value(metric_type, raw)
                metrics.append(
                    MetricSnapshot(
                        metric_id=info.metric_id,
                        name=info.name,
                        type_name=TYPE_LABELS.get(info.type, str(int(info.type))),
                        unit=info.unit,
                        value=shown,
                    )
                )
            preview_parts: list[str] = []
            for metric in metrics:
                if metric.value:
                    unit = f" {metric.unit}" if metric.unit else ""
                    preview_parts.append(f"{metric.name}={metric.value}{unit}")
                else:
                    preview_parts.append(metric.name)
            if state.link == "online":
                status = "на связи"
            elif state.link == "offline":
                status = "нет ответа"
            else:
                status = "найден"
            snapshots.append(
                DeviceSnapshot(
                    address=address,
                    device_id=manifest.device_id,
                    name=manifest.name or f"device-{address}",
                    firmware=f"fw {manifest.fw_major}.{manifest.fw_minor}",
                    poll_interval_ms=manifest.poll_interval_ms,
                    link=state.link,
                    status=status,
                    preview=", ".join(preview_parts),
                    metrics=tuple(metrics),
                )
            )
        return snapshots

    def _close_bus(self) -> None:
        self._polling = False
        bus = self._bus
        self._bus = None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
