"""tlmtry-collector CLI."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .config import load_config
from .replicate import backup_ready, replicate
from .scheduler import CollectorScheduler, SchedulerConfig
from .serial_bus import Bus, SerialTransport
from .storage import Storage


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _open_bus(cfg, *, sim: bool = False) -> Bus:
    if sim:
        from .sim import QueueTransport, demo_devices

        transport = QueueTransport(demo_devices())
    else:
        transport = SerialTransport(port=cfg.port, baudrate=cfg.baudrate)
    return Bus(
        transport,
        response_timeout_s=cfg.response_timeout_ms / 1000.0,
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.database:
        cfg.database = args.database
    if args.sim:
        # Simulator only has a few addresses; skip slow empty unicast sweep.
        if args.config is None:
            cfg.address_max = 5
        cfg.use_broadcast = True
    _setup_logging(cfg.log_level)
    storage = Storage(cfg.database)
    bus = _open_bus(cfg, sim=bool(args.sim))
    sched = CollectorScheduler(
        bus,
        storage,
        SchedulerConfig(
            rediscover_every_s=cfg.rediscover_every_s,
            address_min=cfg.address_min,
            address_max=cfg.address_max,
            use_broadcast=cfg.use_broadcast,
            unicast_fill=not args.sim,
            interval_overrides=cfg.interval_overrides,
        ),
    )

    stop = threading.Event()

    def _handle_signal(signum, frame) -> None:  # noqa: ANN001
        logging.getLogger(__name__).info("signal %s received, shutting down", signum)
        stop.set()
        sched.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    repl_thread: threading.Thread | None = None
    if cfg.backup_path and cfg.replicate_every_s:

        def _repl_loop() -> None:
            log = logging.getLogger("tlmtry.replicate")
            while not stop.wait(cfg.replicate_every_s):
                dest = cfg.backup_path
                try:
                    # SQLite USB mounts: skip when parent path is absent.
                    if not backup_ready(dest):
                        log.debug(
                            "backup parent missing, skip: %s",
                            Path(dest).parent,
                        )
                        continue
                    replicate(storage, dest)
                except Exception:
                    log.exception("background replication failed")

        repl_thread = threading.Thread(target=_repl_loop, name="replicate", daemon=True)
        repl_thread.start()

    try:
        sched.run_forever()
    finally:
        bus.close()
        storage.close()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _setup_logging(cfg.log_level)
    if args.port:
        cfg.port = args.port
    if args.max_address:
        cfg.address_max = args.max_address
    if args.database:
        cfg.database = args.database
    if args.sim and args.max_address is None and args.config is None:
        cfg.address_max = 5
    storage = Storage(cfg.database)
    bus = _open_bus(cfg, sim=bool(args.sim))
    try:
        manifests = bus.discover_scan(
            address_min=cfg.address_min,
            address_max=cfg.address_max,
            use_broadcast=cfg.use_broadcast,
            unicast_fill=not args.sim,
        )
        for m in manifests:
            override = cfg.interval_overrides.get(m.address)
            storage.upsert_manifest(m, poll_interval_ms=override)
            print(
                f"addr={m.address} id={m.device_id} name={m.name!r} "
                f"fw={m.fw_major}.{m.fw_minor} interval_ms={m.poll_interval_ms} "
                f"metrics={[x.name for x in m.metrics]}"
            )
        print(f"found {len(manifests)} device(s)")
    finally:
        bus.close()
        storage.close()
    return 0


def cmd_replicate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _setup_logging(cfg.log_level)
    primary = Storage(args.database or cfg.database)
    dest = args.to or cfg.backup_path
    if not dest:
        print(
            "backup target required: --to or config backup_path "
            "(SQLite path or postgres:// URL)",
            file=sys.stderr,
        )
        return 2
    try:
        result = replicate(primary, dest)
        print(result)
    finally:
        primary.close()
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    storage = Storage(args.database or cfg.database)
    try:
        devices = storage.list_devices()
        print(f"database: {storage.location} ({storage.backend})")
        print(f"devices: {len(devices)}")
        print(f"samples: {storage.sample_count()}")
        print(f"replication watermark: {storage.get_replication_watermark()}")
        for d in devices:
            seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.last_seen)) if d.last_seen else "-"
            print(
                f"  addr={d.address} name={d.name!r} online={d.online} "
                f"interval_ms={d.poll_interval_ms} last_seen={seen} err={d.last_error!r}"
            )
    finally:
        storage.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tlmtry-collector", description="RS-485 telemetry collector")
    p.add_argument("-c", "--config", help="Path to YAML config")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run collector loop (foreground; use systemd for service)")
    run.add_argument("--sim", action="store_true", help="Use in-process fake RS-485 devices")
    run.add_argument(
        "--database",
        help="Override primary DB: SQLite path or postgres:// / postgresql:// URL",
    )
    run.set_defaults(func=cmd_run)

    disc = sub.add_parser("discover", help="Scan bus and print/store manifests")
    disc.add_argument("--port", help="Override serial port")
    disc.add_argument("--max-address", type=int, default=None)
    disc.add_argument(
        "--database",
        help="Override primary DB: SQLite path or postgres:// / postgresql:// URL",
    )
    disc.add_argument("--sim", action="store_true", help="Use in-process fake RS-485 devices")
    disc.set_defaults(func=cmd_discover)

    rep = sub.add_parser(
        "replicate",
        help="Copy new samples to backup SQLite file or PostgreSQL URL",
    )
    rep.add_argument("--to", help="Backup SQLite path or postgres:// / postgresql:// URL")
    rep.add_argument(
        "--database",
        help="Primary DB: SQLite path or postgres:// / postgresql:// URL",
    )
    rep.set_defaults(func=cmd_replicate)

    gui = sub.add_parser("gui", help="Open the sensor debug window")
    gui.set_defaults(func=cmd_gui)

    st = sub.add_parser("status", help="Show devices and sample counts")
    st.add_argument(
        "--database",
        help="Primary DB: SQLite path or postgres:// / postgresql:// URL",
    )
    st.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
