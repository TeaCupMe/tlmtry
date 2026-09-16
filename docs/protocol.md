# Tlmtry RS-485 Protocol

Binary half-duplex protocol for up to 100 Arduino sensor nodes on one RS-485 bus.

## Frame layout

All multi-byte integers are **little-endian**.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 1 | `SOF` | Always `0xAA` |
| 1 | 1 | `VER` | Protocol version (`0x01`) |
| 2 | 1 | `ADDR` | Device address `1..100`; `0` = broadcast |
| 3 | 1 | `CMD` | Command / response code |
| 4 | 2 | `LEN` | Payload length in bytes |
| 6 | LEN | `PAYLOAD` | Command-specific |
| 6+LEN | 2 | `CRC16` | CRC-16/Modbus over bytes `[VER..PAYLOAD]` (SOF excluded) |

Maximum payload: 256 bytes. Maximum frame: 262 bytes.

## Commands

| Code | Name | Direction | Description |
|------|------|-----------|-------------|
| `0x01` | `DISCOVER` | PC → bus | Request device manifest (`ADDR=0` broadcast or specific) |
| `0x81` | `DISCOVER_ACK` | Device → PC | Manifest reply |
| `0x02` | `READ` | PC → device | Read all metric values |
| `0x82` | `READ_ACK` | Device → PC | Metric values |
| `0x03` | `PING` | PC → device | Liveness check |
| `0x83` | `PONG` | Device → PC | Empty payload |
| `0x7F` | `NACK` | Device → PC | Error: payload `[error_code u8]` |

### NACK error codes

| Code | Meaning |
|------|---------|
| 1 | Unknown command |
| 2 | Bad payload |
| 3 | Busy |
| 4 | Internal error |

## Metric types

| Value | Type | Wire size |
|-------|------|-----------|
| 0 | `i32` | 4 |
| 1 | `f32` | 4 (IEEE-754) |
| 2 | `bool` | 1 (`0`/`1`) |
| 3 | `u16` | 2 |

## `DISCOVER_ACK` payload

```
device_id        u16
fw_major         u8
fw_minor         u8
name_len         u8
name             bytes[name_len]
poll_interval_ms u32
metric_count     u8
repeat metric_count times:
  metric_id      u8
  type           u8
  unit_len       u8
  unit           bytes[unit_len]
  name_len       u8
  name           bytes[name_len]
```

## `READ_ACK` payload

```
metric_count     u8
repeat metric_count times:
  metric_id      u8
  type           u8
  value          (size depends on type)
```

## Timing

- After sending a request, the PC waits up to `response_timeout_ms` (default 200 ms) for a reply.
- Devices must leave the bus idle (DE low / receive mode) when not transmitting.
- Only one outstanding request on the bus at a time.
- Recommended baud rates: 9600, 19200, 115200 (8N1).

## Discovery scan

1. PC optionally sends `DISCOVER` with `ADDR=0` (broadcast). Devices that support it may reply with staggered delays (`addr * 5 ms`) to reduce collisions.
2. PC then unicast-scans addresses `1..100` for any that did not reply.
3. Manifests are stored; each device is polled at its advertised (or config-overridden) interval.
