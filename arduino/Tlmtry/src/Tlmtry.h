#ifndef TLMTRY_H
#define TLMTRY_H

#include <Arduino.h>
#include <Stream.h>

namespace tlmtry {

static const uint8_t PROTOCOL_VERSION = 0x01;
static const uint8_t SOF = 0xAA;
static const uint8_t ADDR_BROADCAST = 0;

static const uint8_t CMD_DISCOVER = 0x01;
static const uint8_t CMD_DISCOVER_ACK = 0x81;
static const uint8_t CMD_READ = 0x02;
static const uint8_t CMD_READ_ACK = 0x82;
static const uint8_t CMD_PING = 0x03;
static const uint8_t CMD_PONG = 0x83;
static const uint8_t CMD_NACK = 0x7F;

static const uint8_t ERR_UNKNOWN_CMD = 1;
static const uint8_t ERR_BAD_PAYLOAD = 2;
static const uint8_t ERR_BUSY = 3;
static const uint8_t ERR_INTERNAL = 4;

static const uint8_t MAX_METRICS = 16;
static const uint8_t MAX_NAME_LEN = 32;
static const uint8_t MAX_UNIT_LEN = 8;
static const uint16_t MAX_PAYLOAD = 256;
static const uint16_t MAX_FRAME = 262;

enum class MetricType : uint8_t {
  Int32 = 0,
  Float = 1,
  Bool = 2,
  UInt16 = 3,
};

using MetricReader = float (*)();

struct Metric {
  uint8_t id;
  MetricType type;
  char name[MAX_NAME_LEN + 1];
  char unit[MAX_UNIT_LEN + 1];
  MetricReader reader;
};

uint16_t crc16_modbus(const uint8_t *data, size_t len);

class Device {
 public:
  Device(Stream &serial, uint8_t de_pin, uint8_t address);

  void begin();
  void setName(const char *name);
  void setFirmwareVersion(uint8_t major, uint8_t minor);
  void setPollIntervalMs(uint32_t interval_ms);
  void setDeviceId(uint16_t device_id);

  bool addMetric(const char *name, MetricType type, const char *unit,
                 MetricReader reader);

  /** Call frequently from loop(); handles RX and replies. */
  void poll();

  uint8_t address() const { return address_; }
  uint32_t pollIntervalMs() const { return poll_interval_ms_; }

 private:
  void setTransmit(bool tx);
  void sendFrame(uint8_t cmd, const uint8_t *payload, uint16_t len);
  void handleFrame(uint8_t addr, uint8_t cmd, const uint8_t *payload,
                   uint16_t len);
  void replyDiscover();
  void replyRead();
  void replyPong();
  void replyNack(uint8_t error_code);
  bool readByte(uint8_t &out);

  Stream &serial_;
  uint8_t de_pin_;
  uint8_t address_;
  uint16_t device_id_;
  uint8_t fw_major_;
  uint8_t fw_minor_;
  uint32_t poll_interval_ms_;
  char name_[MAX_NAME_LEN + 1];

  Metric metrics_[MAX_METRICS];
  uint8_t metric_count_;

  enum class RxState : uint8_t {
    WaitSof,
    WaitVer,
    WaitAddr,
    WaitCmd,
    WaitLenLo,
    WaitLenHi,
    WaitPayload,
    WaitCrcLo,
    WaitCrcHi,
  };

  RxState rx_state_;
  uint8_t rx_ver_;
  uint8_t rx_addr_;
  uint8_t rx_cmd_;
  uint16_t rx_len_;
  uint16_t rx_idx_;
  uint8_t rx_payload_[MAX_PAYLOAD];
  uint8_t rx_crc_lo_;
  uint32_t rx_last_ms_;
};

}  // namespace tlmtry

using TlmtryDevice = tlmtry::Device;
using TlmtryMetricType = tlmtry::MetricType;

#endif
