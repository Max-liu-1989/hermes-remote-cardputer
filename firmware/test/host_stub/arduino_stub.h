// 仅用于主机语法检查的桩头文件（不是固件的一部分，也不验证 API 语义）。
// 目的：在没有 ESP32 工具链的机器上，至少保证 main.cpp 自己写得能编译。
// 真机编译必须用 PlatformIO + 真实库（见 firmware/README.md）。
#pragma once

#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// ── Arduino 基础 ────────────────────────────────────────────────────────────
class String {
 public:
  String() {}
  String(const char* s) : s_(s ? s : "") {}
  String(const std::string& s) : s_(s) {}
  String(int v) : s_(std::to_string(v)) {}
  String(unsigned v) : s_(std::to_string(v)) {}
  String(long v) : s_(std::to_string(v)) {}
  String(unsigned long v) : s_(std::to_string(v)) {}

  const char* c_str() const { return s_.c_str(); }
  unsigned length() const { return (unsigned)s_.size(); }
  int toInt() const { return atoi(s_.c_str()); }
  bool startsWith(const char* p) const { return s_.rfind(p, 0) == 0; }
  void trim() {}
  String substring(unsigned a) const { return String(s_.substr(a)); }
  int indexOf(char c, unsigned from = 0) const { return (int)s_.find(c, from); }
  String substring(unsigned a, unsigned b) const { return String(s_.substr(a, b - a)); }

  String& operator+=(char c) { s_ += c; return *this; }
  String& operator+=(const String& o) { s_ += o.s_; return *this; }
  bool operator==(const char* o) const { return s_ == o; }
  bool operator==(const String& o) const { return s_ == o.s_; }

  friend String operator+(const String& a, const String& b) { return String(a.s_ + b.s_); }
  friend String operator+(const char* a, const String& b) { return String(std::string(a) + b.s_); }
  friend String operator+(const String& a, const char* b) { return String(a.s_ + b); }
  friend String operator+(const String& a, int b) { return String(a.s_ + std::to_string(b)); }

 private:
  std::string s_;
};

struct SerialStub {
  void begin(unsigned long) {}
  void println(const String&) {}
  void println(const char*) {}
  void print(const char*) {}
  int printf(const char*, ...) { return 0; }
  int available() { return 0; }
  int read() { return -1; }
};
static SerialStub Serial;

inline unsigned long millis() { return 0; }
inline void delay(unsigned long) {}

struct ESPStub {
  void restart() {}
};
static ESPStub ESP;

// ── WiFi / UDP ──────────────────────────────────────────────────────────────
enum { WL_CONNECTED = 3, WL_IDLE_STATUS = 0 };
enum { WIFI_STA = 1, WIFI_AP = 2 };

struct IPAddressStub {
  String toString() const { return String("0.0.0.0"); }
};

struct WiFiStub {
  void mode(int) {}
  void begin(const char*, const char*) {}
  int status() { return WL_CONNECTED; }
  void softAP(const char*) {}
  String macAddress() { return String("AA:BB:CC:DD:EE:FF"); }
  int RSSI() { return -50; }
};
static WiFiStub WiFi;

struct WiFiUDP {
  bool begin(uint16_t) { return true; }
  int parsePacket() { return 0; }
  int read(uint8_t*, size_t) { return 0; }
  IPAddressStub remoteIP() { return IPAddressStub(); }
};


// ── WebServer（AP 配网） ────────────────────────────────────────────────────
enum { HTTP_GET = 1, HTTP_POST = 2 };

struct WebServer {
  explicit WebServer(int) {}
  template <typename F>
  void on(const char*, int, F) {}
  void send(int, const char*, const char*) {}
  String arg(const char*) { return String(""); }
  void begin() {}
  void handleClient() {}
};

// ── WebSocketsClient ────────────────────────────────────────────────────────
typedef enum {
  WStype_ERROR,
  WStype_DISCONNECTED,
  WStype_CONNECTED,
  WStype_TEXT,
  WStype_BIN,
  WStype_FRAGMENT_TEXT_START,
  WStype_FRAGMENT_BIN_START,
  WStype_FRAGMENT,
  WStype_FRAGMENT_FIN,
  WStype_PING,
  WStype_PONG,
} WStype_t;

struct WebSocketsClient {
  void begin(const char*, uint16_t, const String&) {}
  void onEvent(void (*)(WStype_t, uint8_t*, size_t)) {}
  void setReconnectInterval(unsigned long) {}
  void enableHeartbeat(uint32_t, uint32_t, uint8_t) {}
  void loop() {}
  bool sendTXT(const String&) { return true; }
  bool sendBIN(uint8_t*, size_t) { return true; }
};

// ── Preferences ─────────────────────────────────────────────────────────────
struct Preferences {
  bool begin(const char*, bool) { return true; }
  String getString(const char*, const char*) { return String(""); }
  uint16_t getUShort(const char*, uint16_t) { return 0; }
  size_t putString(const char*, const String&) { return 0; }
  size_t putUShort(const char*, uint16_t) { return 0; }
  void end() {}
};

// ── M5Cardputer / M5Unified ─────────────────────────────────────────────────
#define TFT_BLACK 0x0000
#define TFT_WHITE 0xFFFF
#define TFT_DARKGREY 0x7BEF

struct DisplayStub {
  void setRotation(int) {}
  void fillScreen(uint16_t) {}
  void setTextColor(uint16_t, uint16_t = 0) {}
  void setTextSize(int) {}
  void setCursor(int, int) {}
  void print(const char*) {}
  void startWrite() {}
  void endWrite() {}
  void drawFastHLine(int, int, int, uint16_t) {}
  void fillRect(int, int, int, int, uint16_t) {}
};

struct KeysStateStub {
  std::vector<char> word;
  bool enter = false;
  bool del = false;
  bool space = false;
  bool tab = false;
  bool fn = false;
};

struct KeyboardStub {
  bool isChange() { return false; }
  bool isPressed() { return false; }
  bool isKeyPressed(char) { return false; }
  KeysStateStub keysState() { return KeysStateStub(); }
};

struct ButtonStub {
  bool isPressed() { return false; }
  bool wasPressed() { return false; }
};

struct MicStub {
  bool begin() { return true; }
  void end() {}
  bool record(int16_t*, size_t, uint32_t) { return true; }
  bool isRecording() { return false; }
};

struct PowerStub {
  void begin() {}
  int getBatteryLevel() { return 100; }
};

struct M5Stub {
  struct Config {
    void begin() {}
  };
  Config config() { return Config(); }
  MicStub Mic;
  PowerStub Power;
};

struct M5CardputerStub {
  DisplayStub Display;
  KeyboardStub Keyboard;
  ButtonStub BtnA;
  ButtonStub BtnB;
  template <typename C>
  void begin(C&, bool) {}
  void update() {}
};

static M5Stub M5;
static M5CardputerStub M5Cardputer;
