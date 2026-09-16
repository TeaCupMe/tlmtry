# Tlmtry Arduino library

Register sensors once; the library answers `DISCOVER`, `READ`, and `PING` on RS-485.

## Install

Copy `arduino/Tlmtry` into your Arduino `libraries` folder, or add this repo path in the IDE / PlatformIO `lib_extra_dirs`.

## Minimal sketch

```cpp
#include <Tlmtry.h>

TlmtryDevice device(Serial1, /*de_pin=*/2, /*address=*/1);

float readTemp() { return 23.5f; }

void setup() {
  Serial1.begin(115200);
  device.begin();
  device.setName("node-1");
  device.setPollIntervalMs(5000);
  device.addMetric("temp_c", TlmtryMetricType::Float, "C", readTemp);
}

void loop() {
  device.poll();
}
```

See `examples/BasicSensors` and `docs/protocol.md`.
