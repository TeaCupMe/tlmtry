"""Small window for debugging Tlmtry sensors on an RS-485 COM port."""

from __future__ import annotations

import sys
import time
import tkinter as tk
from tkinter import ttk

from .debug_session import (
    BusSettings,
    DebugSession,
    DeviceSnapshot,
    SessionEvent,
    list_serial_ports,
)

BAUD_RATES = ("9600", "19200", "38400", "57600", "115200")
COLUMNS = (
    ("address", "Адрес", 64, False),
    ("dev_id", "ID", 56, False),
    ("mtype", "Тип", 72, False),
    ("unit", "Ед.", 52, False),
    ("value", "Значение", 180, True),
    ("interval", "Интервал", 88, False),
    ("status", "Статус", 110, False),
)


def _font(size: int, *, bold: bool = False) -> tuple[str, int] | tuple[str, int, str]:
    family = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
    if bold:
        return (family, size, "bold")
    return (family, size)


class DebugApp:
    def __init__(self, root: tk.Tk | None = None) -> None:
        self.root = root or tk.Tk()
        self.root.title("Tlmtry — отладка датчиков")
        self.root.geometry("1080x700")
        self.root.minsize(920, 540)
        self.session = DebugSession()
        self.session.start()
        self._connected = False
        self._busy = False
        self._closing = False
        self._ignore_poll = False
        self._port_labels: dict[str, str] = {}

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.sim_var = tk.BooleanVar(value=False)
        self.addr_min_var = tk.StringVar(value="1")
        self.addr_max_var = tk.StringVar(value="100")
        self.timeout_var = tk.StringVar(value="200")
        self.probe_var = tk.StringVar(value="50")
        self.poll_var = tk.BooleanVar(value=False)
        self.poll_ms_var = tk.StringVar(value="500")
        self.status_var = tk.StringVar(value="Выберите COM-порт и подключитесь")

        self._build()
        self._refresh_ports()
        self.poll_var.trace_add("write", lambda *_: self._on_poll_toggle())
        self.sim_var.trace_add("write", lambda *_: self._on_sim_toggle())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._drain_events)

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.session.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _build(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        bg = "#eef1f4"
        self.root.configure(background=bg)
        style.configure(".", background=bg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, font=_font(10))
        style.configure("Hint.TLabel", background=bg, foreground="#5c6770", font=_font(9))
        style.configure("Status.TLabel", background=bg, font=_font(10, bold=True))
        style.configure("TLabelframe", background=bg)
        style.configure("TLabelframe.Label", background=bg, font=_font(10, bold=True))
        style.configure("TButton", font=_font(10), padding=(10, 4))
        style.configure("TCheckbutton", background=bg, font=_font(10))
        style.configure("Treeview", font=_font(10), rowheight=28)
        style.configure("Treeview.Heading", font=_font(10, bold=True))
        style.configure("TSpinbox", arrowsize=12)

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="Отладка датчиков", font=_font(16, bold=True)).pack(anchor="w")
        ttk.Label(
            header,
            text="COM-порт, discover и чтение метрик. Раскройте строку устройства, чтобы увидеть показатели.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        link = ttk.LabelFrame(outer, text="Подключение", padding=10)
        link.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        port_row = ttk.Frame(link)
        port_row.pack(fill="x")
        ttk.Label(port_row, text="Порт").pack(side="left")
        self.port_combo = ttk.Combobox(port_row, textvariable=self.port_var)
        self.port_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.refresh_btn = ttk.Button(port_row, text="Обновить", command=self._refresh_ports)
        self.refresh_btn.pack(side="left")
        ttk.Label(port_row, text="Скорость").pack(side="left", padx=(12, 6))
        self.baud_combo = ttk.Combobox(
            port_row, textvariable=self.baud_var, values=BAUD_RATES, width=10
        )
        self.baud_combo.pack(side="left")
        self.sim_check = ttk.Checkbutton(port_row, text="Симулятор", variable=self.sim_var)
        self.sim_check.pack(side="left", padx=(12, 0))

        scan_row = ttk.Frame(link)
        scan_row.pack(fill="x", pady=(8, 0))
        ttk.Label(scan_row, text="Адреса").pack(side="left")
        self.addr_min = ttk.Spinbox(
            scan_row, from_=1, to=100, textvariable=self.addr_min_var, width=5
        )
        self.addr_min.pack(side="left", padx=(6, 0))
        ttk.Label(scan_row, text="–").pack(side="left", padx=4)
        self.addr_max = ttk.Spinbox(
            scan_row, from_=1, to=100, textvariable=self.addr_max_var, width=5
        )
        self.addr_max.pack(side="left")
        ttk.Label(scan_row, text="Ответ, мс").pack(side="left", padx=(16, 6))
        self.timeout_spin = ttk.Spinbox(
            scan_row, from_=20, to=5000, increment=10, textvariable=self.timeout_var, width=7
        )
        self.timeout_spin.pack(side="left")
        ttk.Label(scan_row, text="Проба, мс").pack(side="left", padx=(16, 6))
        self.probe_spin = ttk.Spinbox(
            scan_row, from_=10, to=2000, increment=10, textvariable=self.probe_var, width=7
        )
        self.probe_spin.pack(side="left")

        actions = ttk.Frame(link)
        actions.pack(fill="x", pady=(10, 0))
        self.connect_btn = ttk.Button(actions, text="Подключить", command=self._on_connect)
        self.connect_btn.pack(side="left")
        self.discover_btn = ttk.Button(actions, text="Discover", command=self._on_discover)
        self.discover_btn.pack(side="left", padx=(8, 0))
        self.read_btn = ttk.Button(actions, text="Читать", command=self._on_read)
        self.read_btn.pack(side="left", padx=(8, 0))
        self.poll_check = ttk.Checkbutton(
            actions, text="Непрерывный опрос", variable=self.poll_var
        )
        self.poll_check.pack(side="left", padx=(16, 6))
        ttk.Label(actions, text="каждые").pack(side="left")
        self.poll_spin = ttk.Spinbox(
            actions,
            from_=100,
            to=60000,
            increment=100,
            textvariable=self.poll_ms_var,
            width=7,
            command=self._on_poll_interval,
        )
        self.poll_spin.pack(side="left", padx=4)
        self.poll_spin.bind("<FocusOut>", lambda _event: self._on_poll_interval())
        self.poll_spin.bind("<Return>", lambda _event: self._on_poll_interval())
        ttk.Label(actions, text="мс").pack(side="left")

        ttk.Label(
            link,
            text="Пустые адреса опрашиваются по очереди. Сузьте диапазон, если сканирование затягивается. Симулятор отвечает только на broadcast.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.grid(row=2, column=0, sticky="nsew")

        table_wrap = ttk.Frame(panes)
        table_bar = ttk.Frame(table_wrap)
        table_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(table_bar, text="Устройства", font=_font(10, bold=True)).pack(side="left")
        ttk.Button(table_bar, text="Свернуть все", command=self._collapse_all).pack(side="right")
        ttk.Button(table_bar, text="Развернуть все", command=self._expand_all).pack(
            side="right", padx=(0, 6)
        )

        tree_frame = ttk.Frame(table_wrap)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=tuple(col[0] for col in COLUMNS),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Устройство / метрика", anchor="w")
        self.tree.column("#0", width=200, minwidth=140, stretch=True, anchor="w")
        for key, title, width, stretch in COLUMNS:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, minwidth=60, stretch=stretch, anchor="w")
        self.tree.tag_configure("device", background="#ffffff")
        self.tree.tag_configure("online", foreground="#146c43")
        self.tree.tag_configure("offline", foreground="#9b2c2c")
        self.tree.tag_configure("metric", background="#f7f8fa")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self._xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=self._on_tree_xscroll)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self._xscroll.grid(row=1, column=0, sticky="ew")
        self._xscroll.grid_remove()
        self.tree.bind("<Double-1>", self._toggle_row)

        log_wrap = ttk.Frame(panes, padding=(0, 8, 0, 0))
        ttk.Label(log_wrap, text="Журнал", font=_font(10, bold=True)).pack(anchor="w", pady=(0, 4))
        log_frame = ttk.Frame(log_wrap)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_frame,
            height=6,
            wrap="word",
            font=("DejaVu Sans Mono", 9) if sys.platform != "win32" else ("Consolas", 9),
            relief="flat",
            background="#fbfbfc",
            foreground="#243038",
            padx=8,
            pady=6,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        panes.add(table_wrap, weight=4)
        panes.add(log_wrap, weight=1)

        status = ttk.Frame(outer)
        status.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        self._sync_buttons()
        self._append_log("Готово. Укажите порт или включите симулятор.")

    def _refresh_ports(self) -> None:
        current = self._selected_port()
        ports = list_serial_ports()
        self._port_labels = {label: device for device, label in ports}
        labels = list(self._port_labels)
        self.port_combo.configure(values=labels)
        if current:
            for label, device in self._port_labels.items():
                if device == current:
                    self.port_var.set(label)
                    return
            self.port_var.set(current)
            return
        if labels:
            self.port_var.set(labels[0])

    def _selected_port(self) -> str:
        label = self.port_var.get().strip()
        return self._port_labels.get(label, label)

    def _read_settings(self, *, silent: bool = False) -> BusSettings | None:
        try:
            baud = int(self.baud_var.get().strip())
            address_min = int(self.addr_min_var.get().strip())
            address_max = int(self.addr_max_var.get().strip())
            response_ms = int(self.timeout_var.get().strip())
            probe_ms = int(self.probe_var.get().strip())
            poll_ms = int(self.poll_ms_var.get().strip())
        except ValueError:
            if not silent:
                self.status_var.set("Числовые поля должны быть целыми")
            return None
        if not (1 <= address_min <= address_max <= 100):
            if not silent:
                self.status_var.set("Адреса: от 1 до 100, минимум не больше максимума")
            return None
        if baud <= 0 or response_ms < 20 or probe_ms < 10 or poll_ms < 100:
            if not silent:
                self.status_var.set("Проверьте скорость, таймауты и период опроса")
            return None
        sim = bool(self.sim_var.get())
        port = self._selected_port()
        if not sim and not port:
            if not silent:
                self.status_var.set("Выберите COM-порт или включите симулятор")
            return None
        return BusSettings(
            port=port,
            baudrate=baud,
            sim=sim,
            address_min=address_min,
            address_max=address_max,
            response_timeout_ms=response_ms,
            probe_timeout_ms=probe_ms,
            poll_interval_ms=poll_ms,
            unicast_fill=True,
        )

    def _on_sim_toggle(self) -> None:
        state = "disabled" if self.sim_var.get() else "normal"
        self.port_combo.configure(state=state)
        self.baud_combo.configure(state=state)
        self.refresh_btn.configure(state=state)

    def _on_connect(self) -> None:
        if self._connected:
            self.session.disconnect()
            return
        settings = self._read_settings()
        if settings is None:
            return
        self.status_var.set("Подключение…")
        self.session.connect(settings)

    def _on_discover(self) -> None:
        settings = self._read_settings()
        if settings is None:
            return
        self.session.discover(settings)

    def _on_read(self) -> None:
        self.session.read_once()

    def _on_poll_toggle(self) -> None:
        if self._ignore_poll:
            return
        settings = self._read_settings(silent=True)
        interval = settings.poll_interval_ms if settings is not None else 500
        self.session.set_polling(bool(self.poll_var.get()), interval)

    def _on_poll_interval(self) -> None:
        if self._ignore_poll or not self.poll_var.get():
            return
        self._on_poll_toggle()

    def _on_tree_xscroll(self, first: str, last: str) -> None:
        self._xscroll.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._xscroll.grid_remove()
        else:
            self._xscroll.grid()

    def _expand_all(self) -> None:
        for iid in self.tree.get_children(""):
            self.tree.item(iid, open=True)

    def _collapse_all(self) -> None:
        for iid in self.tree.get_children(""):
            self.tree.item(iid, open=False)

    def _toggle_row(self, event: tk.Event) -> None:
        element = self.tree.identify_element(event.x, event.y)
        if "indicator" in element:
            return
        row = self.tree.identify_row(event.y)
        if not row or self.tree.parent(row):
            return
        self.tree.item(row, open=not bool(self.tree.item(row, "open")))

    def _sync_buttons(self) -> None:
        connected = self._connected
        busy = self._busy
        self.connect_btn.configure(text="Отключить" if connected else "Подключить")
        can_talk = "normal" if connected and not busy else "disabled"
        self.discover_btn.configure(state=can_talk)
        self.read_btn.configure(state=can_talk)
        self.poll_check.configure(state=can_talk)

    def _set_poll_flag(self, enabled: bool) -> None:
        self._ignore_poll = True
        self.poll_var.set(enabled)
        self._ignore_poll = False

    def _append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {text}\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 200:
            self.log.delete("1.0", f"{line_count - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_events(self) -> None:
        if self._closing:
            return
        drained = 0
        while drained < 200:
            event = self.session.try_get_event()
            if event is None:
                break
            self._apply_event(event)
            drained += 1
        try:
            self.root.after(80, self._drain_events)
        except tk.TclError:
            pass

    def _apply_event(self, event: SessionEvent) -> None:
        if event.kind == "status":
            self.status_var.set(event.text)
        elif event.kind == "log":
            self._append_log(event.text)
        elif event.kind == "connected":
            self._connected = event.flag
            if not event.flag:
                self._set_poll_flag(False)
            self._sync_buttons()
        elif event.kind == "busy":
            self._busy = event.flag
            try:
                self.root.configure(cursor="watch" if event.flag else "")
            except tk.TclError:
                pass
            self._sync_buttons()
        elif event.kind == "polling":
            if self.poll_var.get() != event.flag:
                self._set_poll_flag(event.flag)
        elif event.kind == "devices":
            self._render_devices(event.devices)

    def _render_devices(self, devices: tuple[DeviceSnapshot, ...]) -> None:
        seen: set[str] = set()
        for index, device in enumerate(devices):
            iid = f"dev-{device.address}"
            seen.add(iid)
            values = (
                str(device.address),
                str(device.device_id),
                device.firmware,
                "",
                device.preview,
                f"{device.poll_interval_ms} мс",
                device.status,
            )
            if device.link == "offline":
                tags = ("offline",)
            elif device.link == "online":
                tags = ("online",)
            else:
                tags = ("device",)
            if self.tree.exists(iid):
                self.tree.item(iid, text=device.name, values=values, tags=tags)
                self.tree.move(iid, "", index)
            else:
                self.tree.insert(
                    "",
                    index,
                    iid=iid,
                    text=device.name,
                    values=values,
                    tags=tags,
                    open=False,
                )
            metric_ids: set[str] = set()
            for metric in device.metrics:
                mid = f"{iid}-m-{metric.metric_id}"
                metric_ids.add(mid)
                metric_values = (
                    "",
                    str(metric.metric_id),
                    metric.type_name,
                    metric.unit,
                    metric.value,
                    "",
                    "",
                )
                if self.tree.exists(mid):
                    self.tree.item(mid, text=metric.name, values=metric_values, tags=("metric",))
                else:
                    self.tree.insert(
                        iid,
                        "end",
                        iid=mid,
                        text=metric.name,
                        values=metric_values,
                        tags=("metric",),
                    )
            for child in self.tree.get_children(iid):
                if child not in metric_ids:
                    self.tree.delete(child)
        for iid in list(self.tree.get_children("")):
            if iid not in seen:
                self.tree.delete(iid)


def main() -> None:
    try:
        import tkinter as _tk  # noqa: F401
    except ImportError:
        print(
            "Нужен tkinter. В Debian/Ubuntu: sudo apt install python3-tk",
            file=sys.stderr,
        )
        raise SystemExit(1)
    DebugApp().run()


if __name__ == "__main__":
    main()
