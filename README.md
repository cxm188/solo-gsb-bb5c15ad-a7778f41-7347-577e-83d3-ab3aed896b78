# Range 回源规划与重组服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：

- `POST /plan`：规范化闭区间 / suffix 范围，可选接收已缓存块（`cached_blocks`）。
  先按输入顺序计算缓存的可用覆盖，再对未覆盖片段按最大间隙合并（绝不跨过缓存
  覆盖）、按单次回源上限拆块，返回回源块以及每个原范围在回源块 / 缓存块中的
  切片映射——一个范围可以由多个来源连续拼成。
- `POST /assemble`：回显 `/plan` 的 `blocks`、`mappings`（以及 `cached_blocks`），
  并分别按块提供 Base64 回源内容与缓存内容；服务严格校验来源 id、边界、缺块
  与重复块后，依据切片映射按原 ranges 顺序重组，返回每段范围的 Base64 内容与长度。
- `POST /secure-request`：`/plan` 与 `/assemble` 的统一签名入口。请求携带
  `operation`（`plan` 或 `assemble`）、`payload`、`key_id`、`timestamp`、
  `nonce`、`signature`；服务对固定规范化的 JSON 用环境配置的 HMAC-SHA256
  密钥验签并限制时间偏差，同一 `key_id` + `nonce` 在有效窗口内只能成功一次
  （防重放）。验签通过后执行对应操作并原样返回 `/plan` 或 `/assemble` 的响应。

## Docker 一键启动

需要 Docker 20.10+（支持 Compose v2）。在仓库根目录执行：

```
docker compose up --build -d
```

首次会构建镜像（Python 3.12 slim）并在后台启动；查看日志与停止：

```
docker compose logs -f
docker compose down
```

不带 `-d` 可在前台运行。健康检查就绪后，服务监听
http://127.0.0.1:8080 （接口文档：http://127.0.0.1:8080/docs ）。

不使用 Compose 时也可以直接用 Docker：

```
docker build -t range-planner:latest .
docker run --rm -p 127.0.0.1:8080:8080 range-planner:latest
```

## 配置

`/plan` 与 `/assemble` 无需任何环境变量或配置文件，且保持完全公开
（`/secure-request` 的引入不改变它们的行为）。

如需更改对外端口，修改 `docker-compose.yml` 中的端口映射
`127.0.0.1:8080:8080`（容器内固定监听 8080）。

`/secure-request` 通过以下环境变量配置（未配置密钥时该接口不可用，
调用返回 `503 keys_not_configured`；`docker-compose.yml` 已内置一个
**仅供演示** 的 `demo` 密钥，生产环境务必替换）：

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SECURE_REQUEST_KEYS` | 使用 `/secure-request` 时必填 | 空 | 密钥表 JSON 对象：`{"<key_id>": "<标准 Base64 密钥>"}`，可配置多个 key_id；密钥值为标准 Base64（含 `=` 填充），解码后的原始字节用于 HMAC-SHA256 |
| `SECURE_REQUEST_TIMESTAMP_TOLERANCE` | 否 | `300` | 请求 `timestamp` 与服务端时钟允许的最大偏差（秒）；也是 nonce 的有效窗口长度 |

生成一个新的随机 Base64 密钥：

```
python3 -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
```

例如配置两个调用方身份：

```
SECURE_REQUEST_KEYS='{"edge-a": "<base64-key-a>", "edge-b": "<base64-key-b>"}'
```

> nonce 防重放记录保存在服务进程内存中。多 worker（如多副本 / 多 uvicorn
> worker）部署时记录不共享，需要将示例中的存储替换为 Redis 等共享存储。

## 接口

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

### `POST /plan`

请求示例（不带缓存，行为与旧版一致）：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128
}
```

响应中的 `blocks` 为合并后的回源块（含可直接使用的 `range_header`），
`mappings` 为每个原范围（按 `index` 对应输入下标）在块中的切片映射。

#### 缓存感知规划（可选 `cached_blocks`）

如果调用方本地已缓存了对象的某些字节，可以通过 `cached_blocks` 声明，
每项为稳定 id（字符串或整数，请求内唯一）加闭区间：

```json
{
  "object_length": 256,
  "ranges": [
    {"type": "interval", "start": 0, "end": 99},
    {"type": "suffix", "last_n": 16}
  ],
  "max_gap": 8,
  "fetch_limit": 128,
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

规划规则：

1. **可用覆盖**：多个缓存块相互重叠时按输入顺序优先——先出现的缓存块占据
   重叠部分，后出现的只获得尚未被占据的剩余区间；
2. **未覆盖片段合并**：各范围扣除缓存覆盖后得到未覆盖片段，间隙
   `<= max_gap` 的相邻片段合并为一个回源段，但合并**不得跨过缓存覆盖**
   （间隙中含有缓存字节时绝不合并，避免重复回源）；
3. **按上限拆块**：合并后的回源段按 `fetch_limit` 拆成若干回源块，每块
   不超过上限。因此提供 `cached_blocks` 后，单个范围允许超过
   `fetch_limit`（会被拆到多个回源块），不再报 `unsatisfiable`；
4. **切片来源**：`mappings[].slices[]` 用 `source` 标明来源——`"fetch"`
   （携带 `block_index`）或 `"cache"`（携带 `cache_id`）。一个范围的多个
   切片按对象偏移顺序连续覆盖整个范围，可以横跨缓存块与多个回源块。

上面的请求会得到 3 个回源块 `[0,39]`、`[60,99]`、`[240,255]`，其中范围 0
的切片依次为 `fetch(块0)` → `cache(cdn-a)` → `fetch(块1)`：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "slices": [
        {"source": "fetch", "block_index": 0, "cache_id": null,
         "block_start": 0, "block_end": 39, "offset_in_block": 0, "length": 40},
        {"source": "cache", "block_index": null, "cache_id": "cdn-a",
         "block_start": 40, "block_end": 59, "offset_in_block": 0, "length": 20},
        {"source": "fetch", "block_index": 1, "cache_id": null,
         "block_start": 60, "block_end": 99, "offset_in_block": 0, "length": 40}
      ]
    },
    {
      "index": 1,
      "type": "suffix",
      "start": 240,
      "end": 255,
      "slices": [
        {"source": "fetch", "block_index": 2, "cache_id": null,
         "block_start": 240, "block_end": 255, "offset_in_block": 0, "length": 16}
      ]
    }
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ]
}
```

响应中的 `cached_blocks` 只回显本次规划**实际引用到**的缓存块（未命中任何
范围的缓存块不会出现）。若所有范围都命中缓存，`blocks` 为空列表。

### `POST /assemble`

请求体把 `/plan` 的返回原样回显，并在 `contents` 中为**每个**回源块提供其
完整回源内容（标准 Base64，含 `=` 填充）；若规划引用了缓存块，还需回显
`cached_blocks` 并在 `cache_contents` 中为**每个**缓存块提供完整缓存内容：

```json
{
  "object_length": 256,
  "blocks": [
    {"index": 0, "start": 0, "end": 39, "length": 40, "range_header": "bytes=0-39"},
    {"index": 1, "start": 60, "end": 99, "length": 40, "range_header": "bytes=60-99"},
    {"index": 2, "start": 240, "end": 255, "length": 16, "range_header": "bytes=240-255"}
  ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJw=="},
    {"block_index": 1, "data_b64": "PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiYw=="},
    {"block_index": 2, "data_b64": "8PHy8/T19vf4+fr7/P3+/w=="}
  ],
  "cached_blocks": [
    {"cache_id": "cdn-a", "start": 40, "end": 59}
  ],
  "cache_contents": [
    {"cache_id": "cdn-a", "data_b64": "KCkqKywtLi8wMTIzNDU2Nzg5Ojs="}
  ]
}
```

校验规则（任一不满足都返回 `422`）：

1. `blocks[].index` 唯一、区间合法、`length == end - start + 1` 且不越界；
2. `contents[].block_index` 唯一，且与 `blocks` 一一对应——缺块、重复提供、
   提供多余块都会被拒绝；
3. `cached_blocks[].cache_id` 唯一、区间合法且不越界；`cache_contents[].cache_id`
   唯一，且与 `cached_blocks` 一一对应——缺块、重复提供、提供未声明的 id
   都会被拒绝；
4. `data_b64` 必须是合法的标准 Base64，解码字节数必须等于该来源块的声明长度；
5. `mappings[].index` 必须恰好覆盖 `0..N-1`（对应原 ranges 顺序，不可重复或缺失）；
6. 每个切片的来源必须合法：`source=fetch` 携带 `block_index` 且引用存在的
   回源块，`source=cache` 携带 `cache_id` 且引用已声明的缓存块；切片记录的
   `block_start/block_end` 与来源块一致，`offset_in_block + length` 不得超出
   来源块长度；
7. 切片必须从范围起点开始连续拼接（可横跨多个来源），且总长度等于
   `end - start + 1`。

全部命中缓存时 `blocks` 与 `contents` 传空列表即可。成功响应按原 ranges
顺序（`mappings[].index`）返回每段内容：

```json
{
  "object_length": 256,
  "ranges": [
    {
      "index": 0,
      "type": "interval",
      "start": 0,
      "end": 99,
      "length": 100,
      "data_b64": "..."
    }
  ]
}
```

典型调用流程：先请求 `/plan`，按 `blocks[].range_header` 发起实际回源，
把每块响应体做 Base64 编码；命中缓存的部分直接从本地缓存读取对应
`cached_blocks` 的字节并做 Base64 编码；最后连同 `blocks`、`mappings`、
`cached_blocks` 一起提交给 `/assemble`，即可取回与原请求 ranges 一一对应
的字节内容。

### `POST /secure-request`

`/plan` 与 `/assemble` 的统一 HMAC-SHA256 签名入口。请求体：

```json
{
  "operation": "plan",
  "payload": {
    "object_length": 256,
    "ranges": [
      {"type": "interval", "start": 0, "end": 99},
      {"type": "suffix", "last_n": 16}
    ],
    "max_gap": 8,
    "fetch_limit": 128
  },
  "key_id": "demo",
  "timestamp": 1758585600,
  "nonce": "5f8a0c1e7b6d4f2a9e0c3d1b8a7f6e54",
  "signature": "<小写十六进制 HMAC-SHA256 摘要>"
}
```

- `operation`：`"plan"` 或 `"assemble"`，决定 `payload` 按哪个接口校验与执行；
- `payload`：原样对应 `/plan` 或 `/assemble` 的请求体；
- `key_id`：标识使用哪个预置密钥（非空字符串）；
- `timestamp`：当前 Unix 秒（整数），与服务端偏差不得超过
  `SECURE_REQUEST_TIMESTAMP_TOLERANCE`（默认 300 秒）；
- `nonce`：每个请求唯一的随机字符串（非空，最长 128 字符），同一
  `key_id` + `nonce` 在有效窗口（`timestamp + 容限`）内只能成功一次；
- `signature`：按下方规则计算的十六进制 HMAC-SHA256 摘要（推荐小写；
  服务端比较前会统一转小写，故大写亦可）。

#### 签名规则

待签名内容是把 `key_id`、`timestamp`、`nonce`、`payload` 四个字段组成的
对象做**固定 JSON 规范化**后的 UTF-8 字节：

1. 对象的 key 按 Unicode 码位字典序排列；
2. 紧凑分隔符：`,` 与 `:` 后均无空格；
3. 非 ASCII 字符直接输出（不转义成 `\uXXXX`）；
4. `timestamp` 必须是整数秒（不要传浮点或字符串，避免跨语言表示差异）。

然后用 `key_id` 对应的密钥**原始字节**（Base64 解码后）做 HMAC-SHA256，
输出小写十六进制。Python 示例：

```python
import hashlib, hmac, json

def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")

signing_doc = canonical_json({
    "key_id": key_id,
    "timestamp": timestamp,
    "nonce": nonce,
    "payload": payload,
})
signature = hmac.new(secret_bytes, signing_doc, hashlib.sha256).hexdigest()
```

#### 响应与重试语义

- 验签通过：HTTP 200，响应体与直接调用 `/plan` 或 `/assemble` **完全一致**；
- `payload` 自身的字段/语义错误：仍按对应接口返回 HTTP 422（结构同下文章节）。
  注意此时 nonce 已在验签通过后被占用——重试任何请求都应生成新的 nonce；
- 信封/鉴权错误：HTTP 401；nonce 重放：HTTP 409；服务端未配置密钥：HTTP 503。
  详见下方“安全入口错误码”。

仓库内置可直接运行的示例客户端（仅依赖 Python 标准库，默认使用 compose
中的演示密钥）：

```
python3 examples/secure_request_client.py plan
python3 examples/secure_request_client.py assemble

# 重放演示：固定 nonce 与 timestamp 连发两次，第二次返回 409 nonce_replayed
NONCE=fixed-nonce TS=$(date +%s) python3 examples/secure_request_client.py plan
NONCE=fixed-nonce TS=<上一步使用的秒数> python3 examples/secure_request_client.py plan
```

## 错误响应

所有请求/语义错误统一返回 HTTP `422`：

- 字段类型或约束错误：`{"error": "invalid_request", "detail": [...]}`；
- `/plan` 范围不可满足：`{"error": "unsatisfiable", "message": ..., "range_index": ...}`；
- `/assemble` 组装错误：`{"error": "<code>", "detail": [{"message": ..., "loc": ...}]}`，
  其中 `loc` 指向出错的字段路径（如 `contents[2].data_b64`、
  `mappings[0].slices[1]`），并按需附带 `block_index` / `cache_id` /
  `range_index` / `slice_index`。主要错误码：

  | error                       | 含义                               |
  | --------------------------- | ---------------------------------- |
  | `missing_block`             | 缺少回源块内容                     |
  | `duplicate_block_content`   | 同一回源块内容被重复提供           |
  | `unexpected_block`          | 提供了 blocks 中不存在的回源块     |
  | `duplicate_block`           | blocks 中块编号重复                |
  | `missing_cache`             | 缺少缓存块内容                     |
  | `duplicate_cache_content`   | 同一缓存块内容被重复提供           |
  | `unexpected_cache`          | 提供了 cached_blocks 中未声明的 id |
  | `duplicate_cache`           | cached_blocks 中缓存 id 重复       |
  | `invalid_cache`             | 缓存块区间非法                     |
  | `cache_out_of_bounds`       | 缓存块超出对象长度                 |
  | `cache_length_mismatch`     | 缓存内容解码字节数与声明长度不符   |
  | `unknown_cache_reference`   | 切片引用了未声明的缓存块           |
  | `invalid_base64`            | Base64 非法或填充错误              |
  | `block_length_mismatch`     | 解码字节数与声明长度不符           |
  | `invalid_slice`             | 切片来源字段组合非法或偏移非法     |
  | `slice_out_of_bounds`       | 切片超出来源块边界                 |
  | `assembly_length_mismatch`  | 切片总长与范围声明长度不符         |
  | `slice_misaligned`          | 切片不能连续覆盖该范围             |
  | `unknown_block_reference`   | 切片引用了不存在的回源块           |
  | `slice_block_mismatch`      | 切片记录的区间与来源块声明不一致   |
  | `block_out_of_bounds`       | 回源块超出对象长度                 |
  | `duplicate_mapping_index`   | mappings 中下标重复                |
  | `invalid_mapping_index`     | mappings 下标未覆盖 0..N-1         |

### 安全入口错误码（`/secure-request`）

鉴权层错误与业务错误分开返回，响应体形如
`{"error": "<code>", "message": ..., ...}`：

| HTTP | error                  | 含义与定位字段 |
| ---- | ---------------------- | --- |
| 401  | `invalid_envelope`     | 信封无法解析或字段缺失/类型错误（非 JSON、`operation` 非 plan/assemble、`timestamp` 非整数秒、`nonce`/`signature` 为空或超长、缺少 `payload`）；可定位时附带 `field` |
| 401  | `unknown_key_id`       | 服务端密钥表中不存在该 `key_id`（附带 `key_id`） |
| 401  | `timestamp_expired`    | 时间戳超出容限；`message` 给出实际偏差秒数，响应附带 `server_time` 便于对时 |
| 401  | `invalid_signature`    | HMAC-SHA256 签名不匹配（请检查规范化规则、密钥与摘要大小写） |
| 409  | `nonce_replayed`       | 同一 `key_id` + `nonce` 在有效窗口内重复使用（附带 `key_id`） |
| 503  | `keys_not_configured`  | 服务端未配置 `SECURE_REQUEST_KEYS`（或配置非法），安全入口不可用 |

校验顺序为：信封格式 → 服务端密钥配置 → `key_id` → 时间戳 → 签名 →
nonce 重放；即签名错误的请求不会占用 nonce，而验签通过后的重放一律
返回 409（即使两次请求的 `payload` 不同）。

## 本地开发（不使用 Docker）

```
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

本地调试 `/secure-request` 时可直接注入环境变量，例如：

```
SECURE_REQUEST_KEYS='{"demo": "ZGVtby1zZWNyZXQta2V5LWRvLW5vdC11c2UtaW4tcHJvZC0zMkIh"}' \
  uvicorn app.main:app --reload --port 8080
```
