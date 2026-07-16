#!/usr/bin/env python3
"""
本地 athena / api stub — 零外网,让设备"感知自己已连接"

替代原厂固件里指向厂商服务器的 ATHENA_HOST / API_HOST:
  ATHENA_HOST='ws://127.0.0.1:8899'   API_HOST='http://127.0.0.1:8899'

做两件事:
  1. HTTP  POST /v2/pilotauth/     → 返回一个稳定的 dongle_id,注册即完成
  2. WS    /ws/v2/<dongle_id>      → 接受 athenad 连接并周期性发 PING

athenad 收到 PING 就会写 LastAthenaPingTime,UI 据此显示"已连接"。
因为连接是真的建立成功的,athenad 不会进入重连退避循环,也就不会刷日志。

仅依赖标准库。
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

HOST = "127.0.0.1"
PORT = int(os.getenv("LOCAL_STUB_PORT", "8899"))
PING_INTERVAL = 30.0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _stable_dongle_id() -> str:
  """由设备序列号派生出稳定的 dongle_id,重启后不变。"""
  params = Params()
  existing = params.get("DongleId")
  if existing:
    return existing
  try:
    from openpilot.system.hardware import HARDWARE
    seed = HARDWARE.get_serial() or "localstub"
  except Exception:
    seed = "localstub"
  return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _ws_frame(opcode: int, payload: bytes = b"") -> bytes:
  """服务端 → 客户端的帧不做掩码(RFC 6455)。"""
  header = bytes([0x80 | opcode])
  n = len(payload)
  if n < 126:
    header += bytes([n])
  elif n < (1 << 16):
    header += bytes([126]) + struct.pack(">H", n)
  else:
    header += bytes([127]) + struct.pack(">Q", n)
  return header + payload


def _read_exact(conn: socket.socket, n: int) -> bytes:
  buf = b""
  while len(buf) < n:
    chunk = conn.recv(n - len(buf))
    if not chunk:
      raise ConnectionError("closed")
    buf += chunk
  return buf


def _read_frame(conn: socket.socket):
  b1, b2 = _read_exact(conn, 2)
  opcode = b1 & 0x0F
  masked = b2 & 0x80
  n = b2 & 0x7F
  if n == 126:
    n = struct.unpack(">H", _read_exact(conn, 2))[0]
  elif n == 127:
    n = struct.unpack(">Q", _read_exact(conn, 8))[0]
  mask = _read_exact(conn, 4) if masked else None
  data = _read_exact(conn, n) if n else b""
  if mask:
    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
  return opcode, data


def _http_response(status: str, body: bytes, ctype: str = "application/json") -> bytes:
  return (
    f"HTTP/1.1 {status}\r\n"
    f"Content-Type: {ctype}\r\n"
    f"Content-Length: {len(body)}\r\n"
    f"Connection: close\r\n\r\n"
  ).encode() + body


def _serve_websocket(conn: socket.socket, key: str, dongle_id: str) -> None:
  accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
  conn.sendall((
    "HTTP/1.1 101 Switching Protocols\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
  ).encode())
  cloudlog.info(f"local_stub: athenad connected ({dongle_id})")

  stop = threading.Event()

  def pinger():
    # athenad 收到 PING 就写 LastAthenaPingTime → UI 显示已连接
    while not stop.is_set():
      try:
        conn.sendall(_ws_frame(0x9))
      except OSError:
        break
      stop.wait(PING_INTERVAL)

  t = threading.Thread(target=pinger, daemon=True)
  t.start()
  try:
    while True:
      opcode, data = _read_frame(conn)
      if opcode == 0x8:            # close
        break
      elif opcode == 0x9:          # ping → pong
        conn.sendall(_ws_frame(0xA, data))
      # 任何业务消息一律忽略:本 stub 不下发指令
  except (OSError, ConnectionError):
    pass
  finally:
    stop.set()
    cloudlog.info("local_stub: athenad disconnected")


def _handle(conn: socket.socket, dongle_id: str) -> None:
  conn.settimeout(120)
  try:
    raw = conn.recv(8192).decode("utf-8", errors="replace")
    if not raw:
      return
    lines = raw.split("\r\n")
    request = lines[0]
    headers = {}
    for line in lines[1:]:
      if ": " in line:
        k, v = line.split(": ", 1)
        headers[k.lower()] = v

    if headers.get("upgrade", "").lower() == "websocket":
      _serve_websocket(conn, headers.get("sec-websocket-key", ""), dongle_id)
      return

    if "pilotauth" in request:
      conn.sendall(_http_response("200 OK", json.dumps({"dongle_id": dongle_id}).encode()))
    else:
      conn.sendall(_http_response("200 OK", b"{}"))
  except Exception:
    pass
  finally:
    try:
      conn.close()
    except OSError:
      pass


def main() -> None:
  dongle_id = _stable_dongle_id()
  # 预先落盘:registration.register() 见到 DongleId 就直接返回,连 HTTP 都不会发
  Params().put("DongleId", dongle_id)
  cloudlog.info(f"local_stub: serving on {HOST}:{PORT} as {dongle_id}")

  srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  srv.bind((HOST, PORT))
  srv.listen(8)
  while True:
    try:
      conn, _ = srv.accept()
      threading.Thread(target=_handle, args=(conn, dongle_id), daemon=True).start()
    except Exception:
      time.sleep(1)


if __name__ == "__main__":
  main()
