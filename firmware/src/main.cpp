// Cardputer-Adv 固件：连 WiFi → 找桥 → 显示桥推来的位图 → 回传按键/语音。
//
// 这一端刻意“没有业务逻辑”：不认中文、不排版、不存 Hermes 凭据。
// 屏上的一切由 Mac 画好压成 S 帧；板子只解 RLE 并画点。
//
// 状态机：BOOT → PROVISION(8s 串口窗口) → WIFI → DISCOVER(UDP 47824) → LINK(WS 47823)
// 断线：10 秒没帧就本地显示 RECONNECTING，指数退避重连；不缓存、不补发按键。
#include <Arduino.h>
#include <M5Cardputer.h>
#include <Preferences.h>
#include <WebServer.h>
#include <ArduinoOTA.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>

#include "frame_codec.h"

// ── 配置 ─────────────────────────────────────────────────────────────────────
static constexpr uint16_t DEFAULT_BRIDGE_PORT = 47823;
static constexpr uint16_t DISCOVERY_PORT = 47824;
static constexpr uint32_t FRAME_TIMEOUT_MS = 20000;   // 桥每 5 秒补一帧心跳，20 秒没帧才算真断
static constexpr uint32_t HEARTBEAT_MS = 15000;
static constexpr uint32_t PROVISION_WINDOW_MS = 8000;
static constexpr int MIC_RATE = 16000;
static constexpr int MIC_CHUNK_SAMPLES = 1600;   // 100ms @16k
static constexpr int MIC_MAX_MS = 120000;  // 2 分钟（协议上限同步放宽到 3.84MB）

struct Settings {
  String ssid;
  String pass;
  String host;      // 桥的 IP（广播发现不到时的兜底）
  String token;     // 32 hex，设备与桥共享
  uint16_t port = DEFAULT_BRIDGE_PORT;
  bool valid() const { return ssid.length() && token.length() >= 32; }
};

static Settings g_cfg;
static Preferences g_nvs;
static WebSocketsClient g_ws;
static WiFiUDP g_udp;
static bool g_linked = false;
static uint32_t g_last_frame_ms = 0;

// ── 背光省电：闲置变暗，来帧/按键立刻亮回 ────────────────────────────────────
// 屏幕常亮是这块板子最大的静态功耗之一（Wi-Fi 常开是为了随时收审批，不能关）。
static void send_json(String json);   // 前置声明：note_activity 里要用（定义在下面）

static constexpr uint8_t BL_ON = 180;
static constexpr uint8_t BL_DIM = 0;   // PWM 有 offset=16 的底噪，0 实际约 6% 亮度
static constexpr uint32_t DIM_AFTER_MS = 45000;
static uint32_t g_last_activity_ms = 0;
static bool g_dimmed = false;

static void note_activity() {
  g_last_activity_ms = millis();
  if (g_dimmed) {
    M5Cardputer.Display.setBrightness(BL_ON);
    g_dimmed = false;
    send_json("{\"t\":\"log\",\"m\":\"backlight on\"}");
  }
}
static uint32_t g_last_hb_ms = 0;
static uint16_t g_last_seq = 0;
static bool g_have_seq = false;
//: 上一帧内容的指纹：内容没变就不重画（否则每秒整屏闪一次）
static uint32_t g_last_fp = 0;
static bool g_have_fp = false;
static bool g_reconnecting = false;
static bool g_ws_started = false;
static bool g_ota_started = false;
static uint32_t g_backoff_ms = 500;
static uint32_t g_next_retry_ms = 0;
static String g_state_line = "BOOT";

// 录音
static bool g_recording = false;
static uint32_t g_rec_start_ms = 0;
static int16_t g_pcm[MIC_CHUNK_SAMPLES];
static uint8_t g_voice_buf[hr::VOICE_HEADER_LEN + MIC_CHUNK_SAMPLES * 2];

static void start_recording();
static void pump_recording();
static void finish_recording();

// ── NVS ─────────────────────────────────────────────────────────────────────
static void load_settings() {
  g_nvs.begin("hermes", true);
  g_cfg.ssid = g_nvs.getString("ssid", "");
  g_cfg.pass = g_nvs.getString("pass", "");
  g_cfg.host = g_nvs.getString("host", "");
  g_cfg.token = g_nvs.getString("token", "");
  g_cfg.port = (uint16_t)g_nvs.getUShort("port", DEFAULT_BRIDGE_PORT);
  g_nvs.end();
}

static void save_settings() {
  g_nvs.begin("hermes", false);
  g_nvs.putString("ssid", g_cfg.ssid);
  g_nvs.putString("pass", g_cfg.pass);
  g_nvs.putString("host", g_cfg.host);
  g_nvs.putString("token", g_cfg.token);
  g_nvs.putUShort("port", g_cfg.port);
  g_nvs.end();
}

// ── 屏 ──────────────────────────────────────────────────────────────────────
static void draw_text_line(const char* text, int y, uint16_t color = TFT_BLACK) {
  auto& d = M5Cardputer.Display;
  d.setTextColor(color, TFT_WHITE);
  d.setTextSize(1);
  d.setCursor(4, y);
  d.print(text);
}

static void show_state(const String& line) {
  g_state_line = line;
  auto& d = M5Cardputer.Display;
  d.startWrite();
  d.fillScreen(TFT_WHITE);
  d.drawFastHLine(0, 20, hr::SCREEN_W, TFT_BLACK);
  draw_text_line("HERMES REMOTE", 5);
  draw_text_line(line.c_str(), 34);
  // 这一行只在真的没连上时才写：以前每个状态页都固定印 "bridge offline"，
  // 屏上一出现 RECONNECTING 就顺带显示 offline，看着像两件事其实记错了原因。
  if (!g_linked) {
    draw_text_line("bridge offline", 54, TFT_DARKGREY);
  }
  d.endWrite();
}

// 把一帧画到屏上：只按 RLE 解出横向像素段，不分配整帧缓冲
static uint32_t fingerprint(const uint8_t* data, size_t len) {
  uint32_t h = 2166136261u;
  for (size_t i = 0; i < len; ++i) {
    h ^= data[i];
    h *= 16777619u;
  }
  return h;
}

// 新请求提示音：屏放在桌上、人可能不在旁边，没声音就白等
static void beep_request(const char* why) {
  note_activity();
  Serial.printf("beep(%s) enabled=%d vol=%u\n", why, M5.Speaker.isEnabled() ? 1 : 0,
                (unsigned)M5.Speaker.getVolume());
  M5.Speaker.setVolume(95);
  M5.Speaker.tone(2200, 90);
  delay(120);
  M5.Speaker.tone(3000, 130);
}

// 回复提示音：一声长一点的、音更低，跟"要你批"区分开
static void beep_reply() {
  Serial.println("beep(reply)");
  M5.Speaker.setVolume(90);
  M5.Speaker.tone(1760, 220);
}

static void paint_frame(const uint8_t* data, size_t len) {
  hr::ScreenHeader h{};
  if (!hr::parse_header(data, len, &h)) {
    Serial.printf("bad frame header len=%u\n", (unsigned)len);
    return;
  }
  if (g_have_seq && (int16_t)(h.seq - g_last_seq) <= 0) {
    return;  // 迟到的旧帧，丢掉
  }
  g_last_seq = h.seq;
  g_have_seq = true;

  // 内容指纹相同就只更新序号、不重画：桥每 5 秒补一帧心跳，内容没变；整屏重画会闪。
  const uint32_t fp = fingerprint(data + hr::HEADER_LEN, h.payload_len);
  if (g_have_fp && fp == g_last_fp) {
    return;
  }
  g_last_fp = fp;
  g_have_fp = true;

  auto& d = M5Cardputer.Display;
  const uint8_t* payload = data + hr::HEADER_LEN;
  d.startWrite();
  bool ok = false;
  if (h.encoding == hr::ENC_RLE4) {
    // 调色板帧：每个像素都带颜色，整屏都被覆盖，不用先清屏
    ok = hr::decode_rle4_spans(payload, h.payload_len, [&d](int x, int y, int w, int c) {
      d.fillRect(x, y, w, 1, hr::PALETTE565[c & 0x0F]);
    });
  } else if (h.encoding == hr::ENC_RLE1) {
    if (h.flags & 0x01) {
      d.fillScreen(TFT_WHITE);
    }
    ok = hr::decode_rle_spans(payload, h.payload_len, [&d](int x, int y, int w, bool black) {
      if (black) d.fillRect(x, y, w, 1, TFT_BLACK);
    });
  } else {
    if (h.flags & 0x01) {
      d.fillScreen(TFT_WHITE);
    }
    ok = hr::decode_raw_spans(payload, h.payload_len, [&d](int x, int y, int w, bool black) {
      if (black) d.fillRect(x, y, w, 1, TFT_BLACK);
    });
  }
  d.endWrite();
  if (!ok) {
    show_state("BAD FRAME");
  } else if (h.flags & 0x02) {
    // 只有"值得看"的帧才唤醒：审批/回复/按键回声。周期性状态刷新（信号/电量/时长）
    // 不带这个位，否则每分钟都亮一次，背光永远暗不下去。
    note_activity();
  }
}

// ── 串口配网（hermes-remote provision 的对端） ───────────────────────────────
// 解析一行串口配网协议（SSID/PASS/HOST/TOKEN/COMMIT）
static bool handle_provision_line(String& line, Settings& pending) {
  if (line == "PING") {
    Serial.println("PONG");
    return false;
  }
  if (line.startsWith("SSID ")) {
    pending.ssid = line.substring(5);
    return false;
  }
  if (line.startsWith("PASS ")) {
    pending.pass = line.substring(5);
    return false;
  }
  if (line.startsWith("HOST ")) {
    const int sp = line.indexOf(' ', 5);
    pending.host = sp > 0 ? line.substring(5, sp) : line.substring(5);
    if (sp > 0) pending.port = (uint16_t)line.substring(sp + 1).toInt();
    return false;
  }
  if (line.startsWith("TOKEN ")) {
    pending.token = line.substring(6);
    return false;
  }
  if (line == "COMMIT") {
    if (pending.ssid.length() && pending.token.length() >= 32) {
      g_cfg = pending;
      save_settings();
      Serial.println("OK saved");
      delay(200);
      ESP.restart();
    }
    Serial.println("ERR incomplete");
  }
  return false;
}

// 把串口里已经到达的完整行交给上面那个解析器
static void pump_provision_serial(Settings& pending, String& line) {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      line.trim();
      if (line.length()) handle_provision_line(line, pending);
      line = "";
    } else if (line.length() < 200) {
      line += c;
    }
  }
}

static bool provisioning_window() {
  Serial.println("READY hermes-remote-provision");
  const uint32_t start = millis();
  String line;
  Settings pending = g_cfg;
  while (millis() - start < PROVISION_WINDOW_MS) {
    pump_provision_serial(pending, line);
    delay(10);
  }
  Serial.println("READY timeout");
  return false;
}

// ── AP 兜底配网 ─────────────────────────────────────────────────────────────
// 未配置时：AP 网页 + **串口同时可用**，并且每 5 秒重发一次 READY。
// 这样 Mac 侧不用掐开机那 8 秒窗口，AP 网页也能把 token 一起写上（以前网页写不了 token，
// 保存后仍判无效，于是无限回到 AP）。
static void ap_portal() {
  const String ap = "HermesRemote-" + WiFi.macAddress().substring(12);
  WiFi.mode(WIFI_AP);
  WiFi.softAP(ap.c_str());
  WebServer web(80);
  web.on("/", HTTP_GET, [&web]() {
    web.send(200, "text/html; charset=utf-8",
             "<meta name=viewport content='width=device-width'>"
             "<h3>Hermes 遥控器配网</h3><form method=post action=/save>"
             "WiFi 名 <input name=s><br>密码 <input name=p type=password><br>"
             "桥 IP（可空）<input name=h><br>"
             "设备 token（32 hex，见 Mac 上的 ~/.hermes-remote/device.token）"
             "<input name=t><br><button>保存并重启</button></form>"
             "<p>也可以不连网页，直接用 USB 串口配网（板子一直在等）。</p>");
  });
  web.on("/save", HTTP_POST, [&web]() {
    Settings pending = g_cfg;
    pending.ssid = web.arg("s");
    pending.pass = web.arg("p");
    pending.host = web.arg("h");
    pending.token = web.arg("t");
    if (!pending.ssid.length() || pending.token.length() < 32) {
      web.send(400, "text/plain; charset=utf-8",
               "缺 WiFi 名或 token（至少 32 个十六进制字符）。也可以用 USB 串口配网。");
      return;
    }
    g_cfg = pending;
    save_settings();
    web.send(200, "text/plain", "saved, rebooting");
    delay(500);
    ESP.restart();
  });
  show_state("AP: " + ap);
  Serial.println("READY hermes-remote-provision");
  web.begin();
  String line;
  Settings pending = g_cfg;
  uint32_t last_ready = millis();
  while (true) {
    web.handleClient();
    pump_provision_serial(pending, line);
    if (millis() - last_ready > 5000) {
      last_ready = millis();
      Serial.println("READY hermes-remote-provision");
    }
    delay(5);
  }
}

// ── 发现：UDP 47824 ─────────────────────────────────────────────────────────
static bool discover_bridge(uint32_t timeout_ms) {
  if (!g_udp.begin(DISCOVERY_PORT)) return false;
  const uint32_t start = millis();
  uint8_t buf[512];
  while (millis() - start < timeout_ms) {
    const int sz = g_udp.parsePacket();
    if (sz > 0) {
      const int n = g_udp.read(buf, sizeof(buf) - 1);
      if (n > 0) {
        buf[n] = 0;
        // 只认我们自己的广播：{"t":"hermes-remote","v":1,"port":47823,"name":"..."}
        // 不校验的话，同网段任何人发一个带 "port": 的包就能把板子引到假桥上去，
        // 板子连 WS 时会把 device token 明文带进 URL。
        const bool is_ours = strstr((char*)buf, "\"hermes-remote\"") != nullptr;
        const char* p = is_ours ? strstr((char*)buf, "\"port\":") : nullptr;
        if (p) {
          const int port = atoi(p + 7);
          if (port > 0) {
            g_cfg.host = g_udp.remoteIP().toString();
            g_cfg.port = (uint16_t)port;
            Serial.printf("bridge %s:%d\n", g_cfg.host.c_str(), port);
            return true;
          }
        }
      }
    }
    delay(20);
  }
  return false;
}

// ── WebSocket ───────────────────────────────────────────────────────────────
static void send_json(String json) {
  // 参数按值传：WebSockets 的 sendTXT 收的是 String&（非 const），传 const 引用会编译失败
  if (g_linked) g_ws.sendTXT(json);
}

static void send_key(const char* key) {
  // 按下去先响一声：板子没有震动马达，没声音的话人根本不知道键有没有按到
  M5.Speaker.setVolume(70);
  M5.Speaker.tone(1800, 18);
  send_json(String("{\"t\":\"key\",\"k\":\"") + key + "\"}");
}

static void send_heartbeat() {
  // 注意：不要在这里调 M5.Power.isCharging()/getBatteryVoltage()。
  // Cardputer-Adv 的 PMIC 不在 M5Unified 的支持表里，这两个调用会阻塞，
  // 5 秒后看门狗复位 —— 真机上表现为"每 5 秒 READY 一次"的复位循环。
  String hb = "{\"t\":\"hb\",\"rssi\":" + String(WiFi.RSSI()) +
              ",\"batt\":" + String(M5.Power.getBatteryLevel()) +
              ",\"up\":" + String(millis() / 1000) + ",\"fw\":\"1.0.0\"}";
  send_json(hb);
}

static void on_ws_event(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      g_linked = true;
      g_backoff_ms = 500;
      g_last_frame_ms = millis();
      // 每次连上都是一个新的帧序号空间：桥重启后 seq 会从 0 重新数，
      // 不清掉这里的“已见最大 seq”，新帧会全被判成迟到的旧帧丢掉（表现就是画面卡住 + RECONNECTING）。
      g_have_seq = false;
      g_last_seq = 0;
      g_have_fp = false;   // 重连后第一帧一定要画，别被上一轮的指纹挡住
      g_reconnecting = false;
      // 先认证：桥在收到这条之前不会推任何帧
      send_json(String("{\"t\":\"auth\",\"token\":\"") + g_cfg.token + "\"}");
      send_json("{\"t\":\"hello\",\"fw\":\"1.0.0\"}");
      Serial.println("ws connected");
      break;
    case WStype_DISCONNECTED:
      g_linked = false;
      g_ws_started = false;
      g_next_retry_ms = millis() + g_backoff_ms;
      g_backoff_ms = (g_backoff_ms * 2 > 8000) ? 8000 : g_backoff_ms * 2;
      Serial.println("ws disconnected");
      break;
    case WStype_BIN:
      g_last_frame_ms = millis();
      Serial.printf("bin len=%u\n", (unsigned)length);
      paint_frame(payload, length);
      break;
    case WStype_TEXT: {
      // 桥发来的小指令：{"t":"record","ms":10000} / {"t":"welcome"} / {"t":"voice.text"}
      char cmd[160];
      const size_t n = length < sizeof(cmd) - 1 ? length : sizeof(cmd) - 1;
      memcpy(cmd, payload, n);
      cmd[n] = 0;
      if (strstr(cmd, "\"voice.text\"")) {
        // 屏上已经由桥推来的预览帧显示，这里只留个日志
        Serial.println(cmd);
      } else if (strstr(cmd, "\"notify\"")) {
        Serial.println(cmd);
        if (strstr(cmd, "\"reply\"")) {
          beep_reply();             // 新回复：一声低音
        } else {
          beep_request("notify");   // 新请求：两声高音（要你动手）
        }
      } else if (strstr(cmd, "\"voice.error\"")) {
        Serial.println(cmd);
        show_state("识别失败");    // 语音出错必须让人看见，不能只在日志里
        M5.Speaker.setVolume(90);
        M5.Speaker.tone(700, 120);
        delay(140);
        M5.Speaker.tone(500, 160);
      }
      break;
    }
    default:
      break;
  }
}

static void start_ws() {
  if (g_ws_started) return;
  if (!g_cfg.host.length()) return;
  // token 不放进 URL：连上后第一条消息再发（桥的 /ws 会等这条 auth，超时即断）
  g_ws.begin(g_cfg.host.c_str(), g_cfg.port, "/ws");
  g_ws.onEvent(on_ws_event);
  // 重连只交给库自己做（它是单 socket 的）：两套机制叠加会让板子同时开两条连接，
  // 服务端会以 1002（协议错误）关掉其中一条。
  g_ws.setReconnectInterval(5000);
  // 心跳放宽：ESP32 默认开 modem sleep，回包偶尔晚几秒；3 秒超时会把自己判死，
  // 表现就是“画面好好的 → 突然 RECONNECTING/offline → 几秒后又自己连上”。
  g_ws.enableHeartbeat(20000, 10000, 3);
  g_ws_started = true;
}

// ── 键盘 ────────────────────────────────────────────────────────────────────
static void handle_keyboard() {
  // 注意：这个函数每 10ms 被调一次，不能无条件 note_activity()——那会让闲置计时
  // 永远归零、背光永不熄灭（第一版就是这么翻车的）。只有真的按下才唤醒。
  // 侧键（BtnA）不在键盘矩阵上：Keyboard.isChange() 不会因为它变 True。
  // 所以必须先判 BtnA，再判键盘变化——否则“按住侧键说话”永远进不来。
  if (M5Cardputer.BtnA.isPressed()) {
    note_activity();
    start_recording();
    return;
  }
  if (!M5Cardputer.Keyboard.isChange()) return;
  if (!M5Cardputer.Keyboard.isPressed()) return;
  note_activity();            // 到这里才是真的按了键（暗屏时第一下只是唤醒）
  auto st = M5Cardputer.Keyboard.keysState();
  for (char c : st.word) {
    if (c >= '1' && c <= '5') {
      char k[2] = {c, 0};
      send_key(k);
      return;
    }
  }
  if (st.enter) {
    send_key("enter");
    return;
  }
  if (st.del) {
    send_key("back");
    return;
  }
  if (st.space) {
    send_key("space");
    return;
  }
  if (st.fn) {
    send_key("fn1");
    return;
  }
  // 反引号键当 ESC（Cardputer 键盘没有独立 esc）
  if (M5Cardputer.Keyboard.isKeyPressed('`')) {
    send_key("esc");
    return;
  }
  // 其余可打印字符发给桥：开放式提问可以用键盘打字作答（语音识别错了还有条后路）
  for (char c : st.word) {
    if (c >= 32 && c <= 126) {
      String frame = "{\"t\":\"char\",\"c\":\"";
      if (c == '"' || c == '\\') frame += '\\';
      frame += c;
      frame += "\"}";
      send_json(frame);
      return;
    }
  }
}

// ── 录音：边录边发 ──────────────────────────────────────────────────────────
static void start_recording() {
  if (g_recording) return;
  if (!g_linked) {
    show_state("桥没连上");
    return;
  }
  if (!M5.Mic.begin()) {
    Serial.println("mic begin failed");
    show_state("麦克风打不开");
    return;
  }
  g_recording = true;
  g_rec_start_ms = millis();
  // 立刻给本地反馈：按住没反应＝不知道在录没在录（桥的“录音中”页要等下一帧才到）
  M5.Speaker.setVolume(70);
  M5.Speaker.tone(2400, 40);
  show_state("REC 松开结束");
  send_json(String("{\"t\":\"voice.begin\",\"rate\":") + MIC_RATE + "}");
}

static void pump_recording() {
  if (!g_recording) return;
  const bool too_long = (millis() - g_rec_start_ms) > (uint32_t)MIC_MAX_MS;
  if (too_long || !M5Cardputer.BtnA.isPressed()) {
    finish_recording();
    return;
  }
  // record() 阻塞到录满这 100ms；录满立刻发走，板子上永远只有一小片
  if (!M5.Mic.record(g_pcm, MIC_CHUNK_SAMPLES, MIC_RATE)) {
    return;
  }
  const size_t bytes = MIC_CHUNK_SAMPLES * sizeof(int16_t);
  const size_t n = hr::build_voice_chunk((const uint8_t*)g_pcm, bytes, g_voice_buf, sizeof(g_voice_buf));
  if (n > 0 && g_linked) {
    g_ws.sendBIN(g_voice_buf, n);
  }
}

static void finish_recording() {
  if (!g_recording) return;
  g_recording = false;
  send_json("{\"t\":\"voice.end\"}");
  M5.Mic.end();
  show_state("识别中…");
}

// ── 主流程 ──────────────────────────────────────────────────────────────────
void setup() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, true);
  M5Cardputer.Display.setRotation(1);
  M5Cardputer.Display.fillScreen(TFT_WHITE);
  Serial.begin(115200);
  delay(200);
  // 开机提示音：用来确认喇叭这条路本身是通的（听不到就先查这里，不要怀疑通知逻辑）
  beep_request("boot");

  load_settings();
  provisioning_window();  // 8 秒串口窗口（有 READY 横幅）

  if (!g_cfg.valid()) {
    ap_portal();  // 没有配置就开热点让手机填
    return;
  }

  show_state("WIFI " + g_cfg.ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(g_cfg.ssid.c_str(), g_cfg.pass.c_str());
  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) {
    ap_portal();
    return;
  }
  M5.Power.begin();
  // 关掉 Wi-Fi modem sleep：省电换来的延迟抖动会让 WS 心跳超时、自己把自己判成断线
  WiFi.setSleep(false);
  // OTA：USB CDC 只在启动时枚举（热插拔认不到），所以留一条 Wi-Fi 刷机的路。
  // 密码用设备 token（NVS 里那份），主机侧用同一个值 --auth。
  ArduinoOTA.setHostname("hermes-remote");
  if (g_cfg.token.length() >= 8) {
    ArduinoOTA.setPassword(g_cfg.token.c_str());
  }
  ArduinoOTA.begin();
  g_ota_started = true;
  Serial.printf("ota ready on %s\n", WiFi.localIP().toString().c_str());
  show_state("FIND BRIDGE");
  // 配网时写了 HOST 就是“钉死这个地址”，不再听广播（防止被同网段的假广播带走）
  if (!g_cfg.host.length()) {
    if (!discover_bridge(3000) && !discover_bridge(8000)) {
      Serial.println("bridge not found yet; will keep listening");
    }
  }
  start_ws();
  g_last_hb_ms = millis();
}

void loop() {
  M5Cardputer.update();
  if (g_ota_started) {
    ArduinoOTA.handle();       // Wi-Fi 刷机：以后不用插线（USB CDC 只在启动时枚举）
  }
  if (g_ws_started) {
    g_ws.loop();
  }
  handle_keyboard();
  pump_recording();

  const uint32_t now = millis();
  if (!g_ws_started && g_cfg.host.length()) {
    // 只在第一次起连接；之后的断线重连由库自己按 5 秒间隔做
    start_ws();
  }
  if (g_linked) {
    if (now - g_last_hb_ms > HEARTBEAT_MS) {
      g_last_hb_ms = now;
      send_heartbeat();
    }
    const bool timed_out = now - g_last_frame_ms > FRAME_TIMEOUT_MS;
    if (timed_out && !g_reconnecting) {
      g_reconnecting = true;
      show_state("RECONNECTING");  // 只在进入状态时画一次，不然每 10ms 全屏重绘
    } else if (!timed_out && g_reconnecting) {
      g_reconnecting = false;
    }
    // 半死连接看门狗：socket 还在但一直没帧（服务端卡住 / 路由黑洞）时，
    // 主动断开，让库去重连——否则会一直挂在“已连接但没画面”。
    if (now - g_last_frame_ms > 30000) {
      Serial.println("frame watchdog: forcing reconnect");
      g_ws.disconnect();
    }
  }
  // 闲置到点就压到最低亮度；不关 Wi-Fi、不断连接，审批来了照样收、照样响
  if (!g_dimmed && now - g_last_activity_ms > DIM_AFTER_MS) {
    M5Cardputer.Display.setBrightness(BL_DIM);
    g_dimmed = true;
    send_json("{\"t\":\"log\",\"m\":\"backlight dim\"}");
  }
  delay(10);
}
