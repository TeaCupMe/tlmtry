#include "Tlmtry.h"

#include <string.h>

namespace tlmtry {

uint16_t crc16_modbus(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; ++b) {
      if (crc & 1) {
        crc = (crc >> 1) ^ 0xA001;
      } else {
        crc >>= 1;
      }
    }
  }
  return crc;
}

Device::Device(Stream &serial, uint8_t de_pin, uint8_t address)
    : serial_(serial),
      de_pin_(de_pin),
      address_(address),
      device_id_(address),
      fw_major_(1),
      fw_minor_(0),
      poll_interval_ms_(5000),
      metric_count_(0),
      rx_state_(RxState::WaitSof),
      rx_ver_(0),
      rx_addr_(0),
      rx_cmd_(0),
      rx_len_(0),
      rx_idx_(0),
      rx_crc_lo_(0),
      rx_last_ms_(0) {
  name_[0] = '\0';
  memset(metrics_, 0, sizeof(metrics_));
  memset(rx_payload_, 0, sizeof(rx_payload_));
}

void Device::begin() {
  pinMode(de_pin_, OUTPUT);
  setTransmit(false);
  strncpy(name_, "device", MAX_NAME_LEN);
  name_[MAX_NAME_LEN] = '\0';
}

void Device::setName(const char *name) {
  if (!name) {
    return;
  }
  strncpy(name_, name, MAX_NAME_LEN);
  name_[MAX_NAME_LEN] = '\0';
}

void Device::setFirmwareVersion(uint8_t major, uint8_t minor) {
  fw_major_ = major;
  fw_minor_ = minor;
}

void Device::setPollIntervalMs(uint32_t interval_ms) {
  if (interval_ms < 100) {
    interval_ms = 100;
  }
  poll_interval_ms_ = interval_ms;
}

void Device::setDeviceId(uint16_t device_id) { device_id_ = device_id; }

bool Device::addMetric(const char *name, MetricType type, const char *unit,
                       MetricReader reader) {
  if (metric_count_ >= MAX_METRICS || !name || !reader) {
    return false;
  }
  Metric &m = metrics_[metric_count_];
  m.id = metric_count_;
  m.type = type;
  m.reader = reader;
  strncpy(m.name, name, MAX_NAME_LEN);
  m.name[MAX_NAME_LEN] = '\0';
  if (unit) {
    strncpy(m.unit, unit, MAX_UNIT_LEN);
    m.unit[MAX_UNIT_LEN] = '\0';
  } else {
    m.unit[0] = '\0';
  }
  ++metric_count_;
  return true;
}

void Device::setTransmit(bool tx) {
  digitalWrite(de_pin_, tx ? HIGH : LOW);
  if (tx) {
    delayMicroseconds(50);
  } else {
    delayMicroseconds(50);
  }
}

void Device::sendFrame(uint8_t cmd, const uint8_t *payload, uint16_t len) {
  if (len > MAX_PAYLOAD) {
    return;
  }

  uint8_t header[5];
  header[0] = PROTOCOL_VERSION;
  header[1] = address_;
  header[2] = cmd;
  header[3] = static_cast<uint8_t>(len & 0xFF);
  header[4] = static_cast<uint8_t>((len >> 8) & 0xFF);

  uint16_t crc = 0xFFFF;
  auto feed = [&crc](uint8_t b) {
    crc ^= b;
    for (uint8_t i = 0; i < 8; ++i) {
      if (crc & 1) {
        crc = (crc >> 1) ^ 0xA001;
      } else {
        crc >>= 1;
      }
    }
  };
  for (uint8_t i = 0; i < 5; ++i) {
    feed(header[i]);
  }
  for (uint16_t i = 0; i < len; ++i) {
    feed(payload[i]);
  }

  setTransmit(true);
  serial_.write(SOF);
  serial_.write(header, 5);
  if (len > 0) {
    serial_.write(payload, len);
  }
  serial_.write(static_cast<uint8_t>(crc & 0xFF));
  serial_.write(static_cast<uint8_t>((crc >> 8) & 0xFF));
  serial_.flush();
  setTransmit(false);
}

void Device::replyNack(uint8_t error_code) {
  uint8_t payload[1] = {error_code};
  sendFrame(CMD_NACK, payload, 1);
}

void Device::replyPong() { sendFrame(CMD_PONG, nullptr, 0); }

void Device::replyDiscover() {
  uint8_t payload[MAX_PAYLOAD];
  uint16_t idx = 0;

  auto put_u8 = [&](uint8_t v) {
    if (idx < MAX_PAYLOAD) {
      payload[idx++] = v;
    }
  };
  auto put_u16 = [&](uint16_t v) {
    put_u8(static_cast<uint8_t>(v & 0xFF));
    put_u8(static_cast<uint8_t>((v >> 8) & 0xFF));
  };
  auto put_u32 = [&](uint32_t v) {
    put_u8(static_cast<uint8_t>(v & 0xFF));
    put_u8(static_cast<uint8_t>((v >> 8) & 0xFF));
    put_u8(static_cast<uint8_t>((v >> 16) & 0xFF));
    put_u8(static_cast<uint8_t>((v >> 24) & 0xFF));
  };
  auto put_str = [&](const char *s) {
    uint8_t n = static_cast<uint8_t>(strlen(s));
    put_u8(n);
    for (uint8_t i = 0; i < n && idx < MAX_PAYLOAD; ++i) {
      payload[idx++] = static_cast<uint8_t>(s[i]);
    }
  };

  put_u16(device_id_);
  put_u8(fw_major_);
  put_u8(fw_minor_);
  put_str(name_);
  put_u32(poll_interval_ms_);
  put_u8(metric_count_);

  for (uint8_t i = 0; i < metric_count_; ++i) {
    const Metric &m = metrics_[i];
    put_u8(m.id);
    put_u8(static_cast<uint8_t>(m.type));
    put_str(m.unit);
    put_str(m.name);
  }

  // Stagger broadcast replies to reduce collisions.
  delay(static_cast<unsigned long>(address_) * 5UL);
  sendFrame(CMD_DISCOVER_ACK, payload, idx);
}

void Device::replyRead() {
  uint8_t payload[MAX_PAYLOAD];
  uint16_t idx = 0;

  payload[idx++] = metric_count_;
  for (uint8_t i = 0; i < metric_count_; ++i) {
    const Metric &m = metrics_[i];
    if (idx + 6 > MAX_PAYLOAD) {
      break;
    }
    payload[idx++] = m.id;
    payload[idx++] = static_cast<uint8_t>(m.type);
    float raw = m.reader();
    switch (m.type) {
      case MetricType::Int32: {
        int32_t v = static_cast<int32_t>(raw);
        memcpy(&payload[idx], &v, 4);
        idx += 4;
        break;
      }
      case MetricType::Float: {
        memcpy(&payload[idx], &raw, 4);
        idx += 4;
        break;
      }
      case MetricType::Bool: {
        payload[idx++] = raw != 0.0f ? 1 : 0;
        break;
      }
      case MetricType::UInt16: {
        uint16_t v = static_cast<uint16_t>(raw);
        payload[idx++] = static_cast<uint8_t>(v & 0xFF);
        payload[idx++] = static_cast<uint8_t>((v >> 8) & 0xFF);
        break;
      }
    }
  }
  sendFrame(CMD_READ_ACK, payload, idx);
}

void Device::handleFrame(uint8_t addr, uint8_t cmd, const uint8_t *payload,
                         uint16_t len) {
  (void)payload;
  (void)len;
  const bool for_me = (addr == address_) || (addr == ADDR_BROADCAST);
  if (!for_me) {
    return;
  }

  switch (cmd) {
    case CMD_DISCOVER:
      replyDiscover();
      break;
    case CMD_READ:
      if (addr == ADDR_BROADCAST) {
        replyNack(ERR_BAD_PAYLOAD);
      } else {
        replyRead();
      }
      break;
    case CMD_PING:
      if (addr == ADDR_BROADCAST) {
        replyNack(ERR_BAD_PAYLOAD);
      } else {
        replyPong();
      }
      break;
    default:
      if (addr == address_) {
        replyNack(ERR_UNKNOWN_CMD);
      }
      break;
  }
}

bool Device::readByte(uint8_t &out) {
  if (serial_.available() <= 0) {
    return false;
  }
  int v = serial_.read();
  if (v < 0) {
    return false;
  }
  out = static_cast<uint8_t>(v);
  return true;
}

void Device::poll() {
  uint8_t b;
  const uint32_t now = millis();
  if (rx_state_ != RxState::WaitSof && (now - rx_last_ms_) > 50) {
    rx_state_ = RxState::WaitSof;
  }

  while (readByte(b)) {
    rx_last_ms_ = now;
    switch (rx_state_) {
      case RxState::WaitSof:
        if (b == SOF) {
          rx_state_ = RxState::WaitVer;
        }
        break;
      case RxState::WaitVer:
        rx_ver_ = b;
        rx_state_ = (rx_ver_ == PROTOCOL_VERSION) ? RxState::WaitAddr
                                                  : RxState::WaitSof;
        break;
      case RxState::WaitAddr:
        rx_addr_ = b;
        rx_state_ = RxState::WaitCmd;
        break;
      case RxState::WaitCmd:
        rx_cmd_ = b;
        rx_state_ = RxState::WaitLenLo;
        break;
      case RxState::WaitLenLo:
        rx_len_ = b;
        rx_state_ = RxState::WaitLenHi;
        break;
      case RxState::WaitLenHi:
        rx_len_ |= static_cast<uint16_t>(b) << 8;
        rx_idx_ = 0;
        if (rx_len_ > MAX_PAYLOAD) {
          rx_state_ = RxState::WaitSof;
        } else if (rx_len_ == 0) {
          rx_state_ = RxState::WaitCrcLo;
        } else {
          rx_state_ = RxState::WaitPayload;
        }
        break;
      case RxState::WaitPayload:
        rx_payload_[rx_idx_++] = b;
        if (rx_idx_ >= rx_len_) {
          rx_state_ = RxState::WaitCrcLo;
        }
        break;
      case RxState::WaitCrcLo:
        rx_crc_lo_ = b;
        rx_state_ = RxState::WaitCrcHi;
        break;
      case RxState::WaitCrcHi: {
        const uint16_t got = rx_crc_lo_ | (static_cast<uint16_t>(b) << 8);
        uint8_t tmp[5 + MAX_PAYLOAD];
        tmp[0] = rx_ver_;
        tmp[1] = rx_addr_;
        tmp[2] = rx_cmd_;
        tmp[3] = static_cast<uint8_t>(rx_len_ & 0xFF);
        tmp[4] = static_cast<uint8_t>((rx_len_ >> 8) & 0xFF);
        if (rx_len_ > 0) {
          memcpy(tmp + 5, rx_payload_, rx_len_);
        }
        const uint16_t expect = crc16_modbus(tmp, 5 + rx_len_);
        if (expect == got) {
          handleFrame(rx_addr_, rx_cmd_, rx_payload_, rx_len_);
        }
        rx_state_ = RxState::WaitSof;
        break;
      }
    }
  }
}

}  // namespace tlmtry
