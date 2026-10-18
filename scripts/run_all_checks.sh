#!/usr/bin/env bash
# 一条命令跑完所有能在这台机器上跑的验证。
#   bash scripts/run_all_checks.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY="${PYTHON:-python3}"
fail=0
step() { echo; echo "── $* ──"; }

step "单元测试 + 端到端测试（假后端 + 模拟板子）"
"$PY" -m unittest discover -s tests -p 'test_*.py' || fail=1

step "跨语言帧向量（Python 编码 → C++ 解码）"
"$PY" scripts/gen_vectors.py || fail=1
mkdir -p build
g++ -std=c++17 -Wall -Wextra -I firmware/include -I firmware/test/test_frame_codec \
    firmware/test/test_frame_codec/test_main.cpp -o build/frame_test || fail=1
./build/frame_test || fail=1

step "固件语法检查（桩头文件；不等于真机编译）"
g++ -std=gnu++17 -fsyntax-only -Wall -Wextra \
    -I firmware/test/host_stub -I firmware/include firmware/src/main.cpp || fail=1

step "导出四种屏（PNG + RLE），供人工看版式"
"$PY" -m hermes_remote.cli frames --out ./screens || fail=1

echo
if [[ $fail -eq 0 ]]; then
  echo "全部通过"
else
  echo "有失败项，见上面输出"
fi
exit $fail
