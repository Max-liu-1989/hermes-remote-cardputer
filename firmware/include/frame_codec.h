// 帧编解码：桥（Python）与板子（C++）之间唯一的契约。
//
// 这个头文件不依赖 Arduino / ESP-IDF，所以可以在主机上直接编译测试：
//     g++ -std=c++17 -I firmware/include firmware/test/test_frame_codec/test_main.cpp -o /tmp/t && /tmp/t
//
// 帧头 9 字节（大端）：type(1) seq(2) w(1) h(1) enc(1) len(2) flags(1)
// 位图：MSB-first，行优先，240*135/8 = 4050 字节；RLE：(count, value) 对，run 跨行连续。
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace hr {

constexpr int SCREEN_W = 240;
constexpr int SCREEN_H = 135;
constexpr int HEADER_LEN = 9;
constexpr int RAW_LEN = (SCREEN_W * SCREEN_H) / 8;  // 4050
constexpr int RLE_MAX = 2 * RAW_LEN;                // 8100
constexpr uint8_t TYPE_SCREEN = 0x01;
constexpr uint8_t TYPE_VOICE = 0x02;
constexpr uint8_t ENC_RLE1 = 0;
constexpr uint8_t ENC_RAW = 1;
constexpr uint8_t ENC_RLE4 = 2;                     // 4 位调色板：每字节两个像素
constexpr int RAW_LEN4 = (SCREEN_W * SCREEN_H) / 2;  // 16200
constexpr int RLE4_MAX = 2 * RAW_LEN4;
constexpr int PALETTE_SIZE = 16;

// 调色板必须与 hermes_remote/protocol.py:PALETTE 一一对应（RGB565）
constexpr uint16_t PALETTE565[PALETTE_SIZE] = {
    0x0000,  // 0 黑
    0xFFFF,  // 1 白
    0xF800,  // 2 红
    0xFD20,  // 3 橙（警告）
    0x07E0,  // 4 绿
    0x8410,  // 5 中灰
    0x4208,  // 6 深灰
    0x04FF,  // 7 蓝
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
};
constexpr int VOICE_HEADER_LEN = 3;

struct ScreenHeader {
  uint8_t type = 0;
  uint16_t seq = 0;
  uint8_t width = 0;
  uint8_t height = 0;
  uint8_t encoding = 0;
  uint16_t payload_len = 0;
  uint8_t flags = 0;
};

inline uint16_t rd16(const uint8_t* p) {
  return static_cast<uint16_t>((static_cast<uint16_t>(p[0]) << 8) | p[1]);
}

inline void wr16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v >> 8);
  p[1] = static_cast<uint8_t>(v & 0xFF);
}

// 解析并校验帧头；不合法就返回 false（宁可什么都不画，也不画花屏）
inline bool parse_header(const uint8_t* data, size_t len, ScreenHeader* out) {
  if (data == nullptr || out == nullptr || len < HEADER_LEN) return false;
  ScreenHeader h{};
  h.type = data[0];
  h.seq = rd16(data + 1);
  h.width = data[3];
  h.height = data[4];
  h.encoding = data[5];
  h.payload_len = rd16(data + 6);
  h.flags = data[8];
  if (h.type != TYPE_SCREEN) return false;
  if (h.width != SCREEN_W || h.height != SCREEN_H) return false;
  if (h.encoding != ENC_RLE1 && h.encoding != ENC_RAW && h.encoding != ENC_RLE4) return false;
  if (len < static_cast<size_t>(HEADER_LEN) + h.payload_len) return false;
  if (h.encoding == ENC_RAW && h.payload_len != RAW_LEN) return false;
  *out = h;
  return true;
}

// 把 RLE 解成一条条横向像素段交给回调：span(x, y, w, black)。
// 不分配整帧缓冲：没有 PSRAM 的机器上，8100 字节的 payload 已经是最大开销。
//
// 注意：RLE 的 run 是“连续 count 个相同字节”，不是一个纯色块——0xAA 这种
// 字节内部黑白相间。所以每个字节要按位展开（0x00 / 0xFF 才是纯色段）。
// 屏宽 240 = 30 字节，字节永远不跨行，展开时可以只按行内偏移算。
template <typename F>
inline bool decode_rle_spans(const uint8_t* payload, size_t len, F&& span) {
  if (payload == nullptr || (len % 2) != 0) return false;
  int byte_index = 0;
  const int total_bytes = RAW_LEN;
  for (size_t i = 0; i < len; i += 2) {
    const int count = payload[i];
    const uint8_t value = payload[i + 1];
    if (count == 0) return false;
    if (byte_index + count > total_bytes) return false;
    for (int b = 0; b < count; ++b) {
      const int idx = byte_index + b;
      const int y = idx / (SCREEN_W / 8);
      const int x0 = (idx % (SCREEN_W / 8)) * 8;
      int bit = 0;
      while (bit < 8) {
        const bool black = (value >> (7 - bit)) & 1;
        int run = 1;
        while (bit + run < 8 && (((value >> (7 - (bit + run))) & 1) != 0) == black) ++run;
        if (black) span(x0 + bit, y, run, true);
        bit += run;
      }
    }
    byte_index += count;
  }
  return byte_index == total_bytes;
}

// RAW 帧：直接逐行回调
template <typename F>
inline bool decode_raw_spans(const uint8_t* payload, size_t len, F&& span) {
  if (payload == nullptr || len != RAW_LEN) return false;
  for (int y = 0; y < SCREEN_H; ++y) {
    const uint8_t* row = payload + y * (SCREEN_W / 8);
    int x = 0;
    while (x < SCREEN_W) {
      const bool black = (row[x / 8] >> (7 - (x % 8))) & 1;
      int run = 1;
      while (x + run < SCREEN_W &&
             (((row[(x + run) / 8] >> (7 - ((x + run) % 8))) & 1) == static_cast<int>(black))) {
        ++run;
      }
      span(x, y, run, black);
      x += run;
    }
  }
  return true;
}

// RLE4：payload 是 (count, byte) 对；byte 里两个 4 位调色板索引（高四位=左像素，低四位=右像素）。
// 每字节 2 像素、120 字节一行，字节不跨行。回调按行输出连续同色段：span(x, y, w, color_index)。
template <typename F>
inline bool decode_rle4_spans(const uint8_t* payload, size_t len, F&& span) {
  if (payload == nullptr || (len % 2) != 0) return false;
  constexpr int BYTES_PER_ROW4 = SCREEN_W / 2;  // 120
  int byte_index = 0;
  int pend_y = -1, pend_x = 0, pend_w = 0, pend_c = -1;
  auto flush = [&]() {
    if (pend_w > 0) {
      span(pend_x, pend_y, pend_w, pend_c);
      pend_w = 0;
    }
  };
  for (size_t i = 0; i < len; i += 2) {
    const int count = payload[i];
    const uint8_t v = payload[i + 1];
    if (count == 0) return false;
    if (byte_index + count > RAW_LEN4) return false;
    for (int b = 0; b < count; ++b) {
      const int idx = byte_index + b;
      const int y = idx / BYTES_PER_ROW4;
      const int x0 = (idx % BYTES_PER_ROW4) * 2;
      const int colors[2] = {(v >> 4) & 0x0F, v & 0x0F};
      for (int k = 0; k < 2; ++k) {
        const int x = x0 + k;
        const int c = colors[k];
        if (pend_c == c && pend_y == y && pend_x + pend_w == x) {
          ++pend_w;
        } else {
          flush();
          pend_y = y;
          pend_x = x;
          pend_w = 1;
          pend_c = c;
        }
      }
    }
    byte_index += count;
  }
  flush();
  return byte_index == RAW_LEN4;
}

// 编码（移植/自测用；固件只解码）
inline size_t rle_encode(const uint8_t* bitmap, size_t len, uint8_t* out, size_t cap) {
  if (bitmap == nullptr || out == nullptr || len == 0) return 0;
  size_t o = 0;
  uint8_t cur = bitmap[0];
  int run = 1;
  for (size_t i = 1; i < len; ++i) {
    if (bitmap[i] == cur && run < 255) {
      ++run;
      continue;
    }
    if (o + 2 > cap) return 0;
    out[o++] = static_cast<uint8_t>(run);
    out[o++] = cur;
    cur = bitmap[i];
    run = 1;
  }
  if (o + 2 > cap) return 0;
  out[o++] = static_cast<uint8_t>(run);
  out[o++] = cur;
  return o;
}

inline size_t build_screen_frame(const uint8_t* bitmap, size_t len, uint16_t seq, uint8_t* out,
                                 size_t cap) {
  if (bitmap == nullptr || out == nullptr || len != RAW_LEN) return 0;
  uint8_t payload[RLE_MAX];
  const size_t plen = rle_encode(bitmap, len, payload, sizeof(payload));
  if (plen == 0 || cap < HEADER_LEN + plen) return 0;
  out[0] = TYPE_SCREEN;
  wr16(out + 1, seq);
  out[3] = SCREEN_W;
  out[4] = SCREEN_H;
  out[5] = ENC_RLE1;
  wr16(out + 6, static_cast<uint16_t>(plen));
  out[8] = 0x01;  // full refresh
  std::memcpy(out + HEADER_LEN, payload, plen);
  return HEADER_LEN + plen;
}

inline size_t build_screen_frame4(const uint8_t* packed, size_t len, uint16_t seq, uint8_t* out,
                                  size_t cap) {
  if (packed == nullptr || out == nullptr || len != RAW_LEN4) return 0;
  static uint8_t payload[RLE4_MAX];
  const size_t plen = rle_encode(packed, len, payload, sizeof(payload));
  if (plen == 0 || cap < HEADER_LEN + plen) return 0;
  out[0] = TYPE_SCREEN;
  wr16(out + 1, seq);
  out[3] = SCREEN_W;
  out[4] = SCREEN_H;
  out[5] = ENC_RLE4;
  wr16(out + 6, static_cast<uint16_t>(plen));
  out[8] = 0x01;  // full refresh
  std::memcpy(out + HEADER_LEN, payload, plen);
  return HEADER_LEN + plen;
}

// 语音分片：3 字节头（type + uint16 len）+ PCM
inline size_t build_voice_chunk(const uint8_t* pcm, size_t len, uint8_t* out, size_t cap) {
  if (pcm == nullptr || len == 0 || len > 0xFFFF || cap < VOICE_HEADER_LEN + len) return 0;
  out[0] = TYPE_VOICE;
  wr16(out + 1, static_cast<uint16_t>(len));
  std::memcpy(out + VOICE_HEADER_LEN, pcm, len);
  return VOICE_HEADER_LEN + len;
}

}  // namespace hr
