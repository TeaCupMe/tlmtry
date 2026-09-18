# Hardware notes (RS-485)

## Topology

- One USB–RS485 adapter on the PC (master).
- Up to 100 Arduino nodes with MAX485 / SP3485 (or similar) transceivers.
- Twisted pair A/B; common GND recommended for short lab runs.
- 120 Ω termination at both ends of the bus when length or noise requires it.

## Arduino transceiver wiring (typical MAX485)

| MAX485 | Arduino |
|--------|---------|
| DI | TX (e.g. `Serial1` TX) |
| RO | RX (e.g. `Serial1` RX) |
| DE + RE (tied) | Digital pin (library `de_pin`) — HIGH = transmit |
| VCC / GND | 5V / GND |
| A / B | Bus A / B |

Boards with one hardware UART (e.g. Uno) can use SoftwareSerial for the bus, or USB Serial for debug and hardware UART for RS-485 on boards that have both (Mega, Leonardo, many SAMD/ESP).

## Addressing

Set each device address to a unique value in `1..100` (jumpers, EEPROM, or compile-time constant in the sketch).

## PC serial port

- Linux: `/dev/ttyUSB0`, `/dev/ttyACM0`, or a stable udev symlink.
- Windows: `COM3`, `COM4`, …
- Baud must match firmware (default in examples: **115200** 8N1).
