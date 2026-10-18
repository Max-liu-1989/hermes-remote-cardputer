// 跨语言对拍：读桥生成的一帧，用固件同一份解码器解出逐像素索引
// 用法：./xcheck frame.bin out.idx
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "frame_codec.h"

int main(int argc, char** argv) {
  if (argc < 3) return 2;
  FILE* f = fopen(argv[1], "rb");
  if (!f) return 3;
  std::vector<uint8_t> buf(200000);
  size_t n = fread(buf.data(), 1, buf.size(), f);
  fclose(f);

  hr::ScreenHeader h{};
  if (!hr::parse_header(buf.data(), n, &h)) {
    fprintf(stderr, "bad header\n");
    return 4;
  }
  std::vector<uint8_t> idx(hr::SCREEN_W * hr::SCREEN_H, 0);
  bool ok = false;
  if (h.encoding == hr::ENC_RLE4) {
    ok = hr::decode_rle4_spans(buf.data() + hr::HEADER_LEN, h.payload_len,
                               [&idx](int x, int y, int w, int c) {
                                 for (int i = 0; i < w; ++i) {
                                   idx[y * hr::SCREEN_W + x + i] = static_cast<uint8_t>(c);
                                 }
                               });
  } else {
    fprintf(stderr, "not rle4 (enc=%u)\n", h.encoding);
    return 5;
  }
  FILE* o = fopen(argv[2], "wb");
  fwrite(idx.data(), 1, idx.size(), o);
  fclose(o);
  printf("ok=%d enc=%u payload=%u pixels=%zu\n", ok ? 1 : 0, h.encoding, h.payload_len,
         idx.size());
  return ok ? 0 : 6;
}
