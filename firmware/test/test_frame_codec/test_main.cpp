// 主机侧测试：不需要 ESP32，也不需要 PlatformIO。
//   g++ -std=c++17 -I include test/test_frame_codec/test_main.cpp -o build/frame_test
//   ./build/frame_test
#include <cassert>
#include <cstdio>
#include <cstring>

#include "frame_codec.h"
#include "vectors.h"

static int g_failures = 0;

#define CHECK(cond)                                                   \
  do {                                                                \
    if (!(cond)) {                                                    \
      std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);     \
      ++g_failures;                                                   \
    }                                                                 \
  } while (0)

static void pattern_bitmap(uint8_t* out) {
  for (int i = 0; i < hr::RAW_LEN; ++i) {
    const int row = i / 30;
    if (row % 7 == 0) {
      out[i] = 0xFF;
    } else if ((i % 30) < 10) {
      out[i] = 0xAA;
    } else {
      out[i] = 0x00;
    }
  }
}

int main() {
  // 1) 编码结果必须与 Python 生成的向量逐字节一致
  uint8_t bitmap[hr::RAW_LEN];
  pattern_bitmap(bitmap);
  static uint8_t encoded[hr::RLE_MAX];
  const size_t elen = hr::rle_encode(bitmap, hr::RAW_LEN, encoded, sizeof(encoded));
  CHECK(elen == kVectorPayloadLen);
  CHECK(std::memcmp(encoded, kVectorPayload, elen) == 0);

  // 2) 解码回来必须与原位图一致（按 span 重画到缓冲）
  static uint8_t redrawn[hr::RAW_LEN];
  std::memset(redrawn, 0, sizeof(redrawn));
  const bool ok = hr::decode_rle_spans(kVectorPayload, kVectorPayloadLen,
                                       [](int x, int y, int w, bool black) {
                                         if (!black) return;
                                         for (int i = 0; i < w; ++i) {
                                           const int px = x + i;
                                           const int idx = y * (hr::SCREEN_W / 8) + px / 8;
                                           redrawn[idx] |= (uint8_t)(0x80 >> (px % 8));
                                         }
                                       });
  CHECK(ok);
  CHECK(std::memcmp(redrawn, bitmap, hr::RAW_LEN) == 0);

  // 3) 帧头解析（拿整帧去解析，payload 长度必须对得上）
  static uint8_t frame[hr::HEADER_LEN + hr::RLE_MAX];
  const size_t flen = hr::build_screen_frame(bitmap, hr::RAW_LEN, 0x1234, frame, sizeof(frame));
  CHECK(flen == kVectorFrameLen);
  CHECK(std::memcmp(frame, kVectorHeader, 9) == 0);
  hr::ScreenHeader h{};
  CHECK(hr::parse_header(frame, flen, &h));
  CHECK(h.type == hr::TYPE_SCREEN);
  CHECK(h.seq == 0x1234);
  CHECK(h.width == 240 && h.height == 135);
  CHECK(h.encoding == hr::ENC_RLE1);
  CHECK(h.payload_len == kVectorPayloadLen);

  // 4) 坏帧必须被拒绝
  static uint8_t bad[hr::HEADER_LEN + hr::RLE_MAX];
  std::memcpy(bad, frame, flen);
  bad[0] = 0x09;  // 未知类型
  CHECK(!hr::parse_header(bad, flen, &h));
  std::memcpy(bad, frame, flen);
  bad[3] = 128;  // 宽度不对
  CHECK(!hr::parse_header(bad, flen, &h));
  std::memcpy(bad, frame, flen);
  bad[5] = 7;  // 未知编码
  CHECK(!hr::parse_header(bad, flen, &h));
  CHECK(!hr::parse_header(frame, 8, &h));  // 头不够长
  CHECK(!hr::parse_header(frame, 9, &h));  // 头说有 payload 但帧里没有

  // 5) RLE 坏数据（0 长度、越界）不能通过
  const uint8_t zero_run[] = {0x00, 0xFF};
  CHECK(!hr::decode_rle_spans(zero_run, sizeof(zero_run), [](int, int, int, bool) {}));
  const uint8_t overrun[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                             0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  CHECK(!hr::decode_rle_spans(overrun, sizeof(overrun), [](int, int, int, bool) {}));

  // 6) 整帧构造在 3) 已校验（flen / 头部字节一致）

  // 7) 语音分片头
  uint8_t pcm[4] = {1, 2, 3, 4};
  uint8_t vc[16];
  const size_t vclen = hr::build_voice_chunk(pcm, sizeof(pcm), vc, sizeof(vc));
  CHECK(vclen == 7);
  CHECK(vc[0] == hr::TYPE_VOICE);
  CHECK(vc[1] == 0x00 && vc[2] == 0x04);
  CHECK(std::memcmp(vc + 3, pcm, 4) == 0);

  if (g_failures == 0) {
    std::printf("frame_codec: all checks passed (payload=%u frame=%u)\n",
                static_cast<unsigned>(kVectorPayloadLen), static_cast<unsigned>(kVectorFrameLen));
    return 0;
  }
  std::printf("frame_codec: %d failure(s)\n", g_failures);
  return 1;
}
