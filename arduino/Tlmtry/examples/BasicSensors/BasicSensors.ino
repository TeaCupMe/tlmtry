/*
  BasicSensors — minimal Tlmtry device with two simulated metrics.

  Wiring (MAX485):
    DI -> TX of RS-485 UART
    RO -> RX of RS-485 UART
    DE+RE -> DE_PIN
    A/B -> bus

  Set DEVICE_ADDRESS uniquely on each node (1..100).
*/

#include <Tlmtry.h>

// Adjust for your board. Uno SoftwareSerial example uses pins 10/11.
#if defined(ARDUINO_AVR_UNO) || defined(ARDUINO_AVR_NANO)
#include <SoftwareSerial.h>
SoftwareSerial rs485(10, 11);  // RX, TX
#define BUS rs485
#else
#define BUS Serial1
#endif

static const uint8_t DE_PIN = 2;
static const uint8_t DEVICE_ADDRESS = 1;

TlmtryDevice device(BUS, DE_PIN, DEVICE_ADDRESS);

static float readTemperatureC() {
  // Replace with real sensor read.
  return 20.0f + (millis() % 1000) / 100.0f;
}

static float readHumidityPct() {
  return 40.0f + (millis() % 500) / 50.0f;
}

void setup() {
  BUS.begin(115200);
  device.begin();
  device.setName("lab-node-1");
  device.setFirmwareVersion(1, 0);
  device.setPollIntervalMs(2000);
  device.addMetric("temp_c", TlmtryMetricType::Float, "C", readTemperatureC);
  device.addMetric("humidity", TlmtryMetricType::Float, "%", readHumidityPct);
}

void loop() {
  device.poll();
}
