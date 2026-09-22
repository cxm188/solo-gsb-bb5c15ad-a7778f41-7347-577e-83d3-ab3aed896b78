#!/usr/bin/env python3
"""POST /secure-request 签名调用示例（仅依赖 Python 标准库）。

服务端验签规则（客户端必须完全一致）：
  1. 把 {key_id, timestamp, nonce, payload} 按"固定 JSON 规范化"序列化为字节：
       - object 的 key 按 Unicode 码位字典序排列（sort_keys=True）；
       - 紧凑分隔符，即 "," 与 ":" 后无空格（separators=(",", ":")）；
       - 非 ASCII 字符直接输出（ensure_ascii=False）；
       - timestamp 必须是整数 Unix 秒。
  2. 用 key_id 对应的密钥（原始字节）对规范化字节做 HMAC-SHA256；
  3. 摘要以小写十六进制字符串作为 signature；
  4. 请求体为 {"operation", "payload", "key_id", "timestamp", "nonce",
     "signature"} 组成的 JSON 对象。

用法：
  # 默认演示 plan 操作（默认读取环境变量中的 demo 密钥）
  python3 examples/secure_request_client.py plan

  # 演示 assemble 操作
  python3 examples/secure_request_client.py assemble

  # 自定义服务地址 / key_id / 密钥
  BASE_URL=http://127.0.0.1:8080 \
  KEY_ID=demo \
  # 密钥为原始字节，下方示例密钥 base64 解码后为 32 字节
  KEY_B64=ZGVtby1zZWNyZXQta2V5LWRvLW5vdC11c2UtaW4tcHJvZC0zMkIh \
  python3 examples/secure_request_client.py plan

重放演示（第二次返回 409 nonce_replayed）：
  NONCE=fixed TS=$(date +%s) python3 examples/secure_request_client.py plan
  NONCE=fixed TS=<上一步秒数> python3 examples/secure_request_client.py plan
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
KEY_ID = os.environ.get("KEY_ID", "demo")
KEY_B64 = os.environ.get(
    "KEY_B64",
    "ZGVtby1zZWNyZXQta2V5LWRvLW5vdC11c2UtaW4tcHJvZC0zMkIh",
)


def canonical_json(value: object) -> bytes:
    """与服务端一致的固定 JSON 规范化。"""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sign(payload: object, key_id: str, timestamp: int, nonce: str, secret: bytes) -> str:
    signing_doc = canonical_json(
        {"key_id": key_id, "timestamp": timestamp, "nonce": nonce, "payload": payload}
    )
    return hmac.new(secret, signing_doc, hashlib.sha256).hexdigest()


def secure_request(operation: str, payload: object) -> None:
    secret = base64.b64decode(KEY_B64, validate=True)
    # 默认每次生成新的时间戳与 nonce；可用环境变量固定二者做重放演示：
    #   NONCE=fixed TS=<unix秒> python3 examples/secure_request_client.py plan
    #   NONCE=fixed TS=<同一个值> python3 examples/secure_request_client.py plan
    # 第二次调用将返回 409 nonce_replayed。
    timestamp = int(os.environ.get("TS", str(int(time.time()))))
    nonce = os.environ.get("NONCE", uuid.uuid4().hex)

    body = {
        "operation": operation,
        "payload": payload,
        "key_id": KEY_ID,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": sign(payload, KEY_ID, timestamp, nonce, secret),
    }
    data = json.dumps(body).encode("utf-8")

    print(f"--> POST {BASE_URL}/secure-request  operation={operation}")
    print(f"    key_id={KEY_ID} timestamp={timestamp} nonce={nonce}")
    req = urllib.request.Request(
        f"{BASE_URL}/secure-request",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"<-- {resp.status}")
            print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as exc:
        print(f"<-- {exc.code}")
        print(exc.read().decode("utf-8"))
        sys.exit(1)


PLAN_PAYLOAD = {
    "object_length": 256,
    "ranges": [
        {"type": "interval", "start": 0, "end": 99},
        {"type": "suffix", "last_n": 16},
    ],
    "max_gap": 8,
    "fetch_limit": 128,
    "cached_blocks": [
        {"cache_id": "cdn-a", "start": 40, "end": 59},
    ],
}

# 对应上面 plan 的回源块/缓存块内容（字节 0..99 与 240..255 顺序取值的演示数据）。
ASSEMBLE_PAYLOAD = {
    "object_length": 256,
    "blocks": [
        {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
        {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
        {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"},
    ],
    "mappings": [
        {
            "index": 0,
            "type": "interval",
            "start": 0,
            "end": 99,
            "slices": [
                {"source": "fetch", "block_index": 0, "cache_id": None,
                 "block_start": 0, "block_end": 39, "offset_in_block": 0, "length": 40},
                {"source": "cache", "block_index": None, "cache_id": "cdn-a",
                 "block_start": 40, "block_end": 59, "offset_in_block": 0, "length": 20},
                {"source": "fetch", "block_index": 1, "cache_id": None,
                 "block_start": 60, "block_end": 99, "offset_in_block": 0, "length": 40},
            ],
        },
        {
            "index": 1,
            "type": "suffix",
            "start": 240,
            "end": 255,
            "slices": [
                {"source": "fetch", "block_index": 2, "cache_id": None,
                 "block_start": 240, "block_end": 255, "offset_in_block": 0, "length": 16},
            ],
        },
    ],
    "contents": [
        {"block_index": 0, "data_b64": base64.b64encode(bytes(range(0, 40))).decode()},
        {"block_index": 1, "data_b64": base64.b64encode(bytes(range(60, 100))).decode()},
        {"block_index": 2, "data_b64": base64.b64encode(bytes(range(240, 256))).decode()},
    ],
    "cached_blocks": [
        {"cache_id": "cdn-a", "start": 40, "end": 59},
    ],
    "cache_contents": [
        {"cache_id": "cdn-a", "data_b64": base64.b64encode(bytes(range(40, 60))).decode()},
    ],
}


def main() -> None:
    operation = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if operation == "plan":
        secure_request("plan", PLAN_PAYLOAD)
    elif operation == "assemble":
        secure_request("assemble", ASSEMBLE_PAYLOAD)
    else:
        print(f"未知 operation：{operation!r}（支持 plan / assemble）", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
