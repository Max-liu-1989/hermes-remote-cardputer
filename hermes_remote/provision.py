"""串口配网：一条命令把 WiFi 名字、密码、设备 token 写进板子的 NVS。

板上固件在启动后 8 秒内监听 USB 串口，接受四行文本::

    PING
    SSID <name>
    PASS <password>
    HOST <bridge-ip> <port>
    TOKEN <32 hex>
    COMMIT

密码不落盘、不进日志、不回显；写完板子回 ``OK``，重启即连。
换网重新跑一次，不用重刷固件。
"""

from __future__ import annotations

import sys
import time


def _require_pyserial():
    try:
        import serial  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise SystemExit("需要 pyserial：pip install pyserial（或 pip install 'hermes-remote[serial]'）") from exc
    return serial


def _pick_port(preferred: str) -> str:
    """板子复位后 /dev 节点可能重建；优先用原端口，端口没了就找唯一一个 usbmodem。"""
    import glob
    import os

    if preferred and os.path.exists(preferred):
        return preferred
    cands = sorted(glob.glob("/dev/cu.usbmodem*"))
    return cands[0] if cands else preferred


def provision_serial(port: str, *, ssid: str, password: str, token: str,
                     bridge_host: str = "", bridge_port: int = 47823,
                     timeout: float = 12.0, baud: int = 115200,
                     wait_ready_s: float = 20.0, attempts: int = 3) -> bool:
    """把 SSID/密码/token 写进 NVS。

    板子只在**开机后 8 秒**里打印 READY 并收配置。固件已经在跑时，只能靠复位把它带回窗口：
    ESP32-S3 的 USB CDC 认 DTR/RTS 的复位组合，但复位会让设备重新枚举（原 fd 报
    "Device not configured"），所以每轮都要重开端口、重找设备名。
    """
    serial = _require_pyserial()
    if len(token) < 32:
        raise ValueError("device token 太短（至少 32 hex 字符）")
    lines = ["PING", f"SSID {ssid}", f"PASS {password}",
             f"HOST {bridge_host or ''} {bridge_port}", f"TOKEN {token}", "COMMIT"]

    def _serial(p: str):
        return serial.Serial(p, baud, timeout=1.0, write_timeout=2.0)

    def _reset(p: str) -> None:
        with _serial(p) as ser:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.12)
            ser.rts = False

    got_ready = False
    ser = None
    for attempt in range(1, attempts + 1):
        try:
            _reset(_pick_port(port))
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"打不开串口 {port}：{exc}") from exc
        time.sleep(0.8)
        port_now = _pick_port(port)
        if not got_ready:
            print(f"第 {attempt} 次：等固件 READY（{port_now}，最多 {wait_ready_s:.0f} 秒）…")
        try:
            ser = _serial(port_now)
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"复位后打不开串口 {port_now}：{exc}") from exc
            continue
        ser.reset_input_buffer()
        deadline = time.time() + (wait_ready_s if attempt == attempts else 8.0)
        banner = b""
        while time.time() < deadline:
            try:
                chunk = ser.read(256)
            except serial.SerialException:
                break  # 设备又枚举了一次：这一轮放弃，下一轮重来
            if chunk:
                banner += chunk
                if b"READY" in banner:
                    got_ready = True
                    break
        if got_ready:
            break
        ser.close()
        ser = None

    if ser is None or not got_ready:
        raise TimeoutError(
            "没等到 READY：把板子电源开关关一下再打开，然后立刻重跑这条命令")

    with ser:
        got = b""
        try:
            for line in lines:
                ser.write((line + "\n").encode("utf-8"))
                ser.flush()
                time.sleep(0.05)
            deadline = time.time() + timeout
            got = b""
            while time.time() < deadline:
                try:
                    chunk = ser.read(256)
                except serial.SerialException:
                    break  # 写完 COMMIT 后板子重启，端口会消失，属正常
                if chunk:
                    got += chunk
                    if b"OK" in got:
                        return True
                    if b"ERR" in got:
                        raise RuntimeError(got.decode("utf-8", "replace").strip())
        except serial.SerialException:
            pass
    if b"OK" in got:
        return True
    raise TimeoutError("板子没有回 OK：确认它是 hermes-remote 固件，且没在别的程序里占着串口")


if __name__ == "__main__":  # 手工试：python -m hermes_remote.provision /dev/cu.usbmodem1101
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m hermes_remote.provision <serial-port>")
    print("用 `hermes-remote provision` 更方便。")
