# 实时疲劳检测 FastAPI 服务

这个目录把项目根目录的 `realtime_camera_fatigue.py` 封装成一个有状态的
FastAPI WebSocket 服务。客户端持续发送 JPEG/PNG 视频帧，服务端逐帧返回
多人的实时检测结果，包括：

- 人脸框、置信度和稳定的 `track_id`；
- `EAR`、`MAR`、`PERCLOS`、闭眼/哈欠时长；
- 头部姿态、视线偏移、低头和点头；
- `CALIBRATING`、`ALERT`、`INATTENTIVE` 或 `DROWSY` 状态；
- 疲劳分数、触发原因和个人中性姿态校准进度。

一个 WebSocket 连接就是一个独立时序会话。连接断开后，其 ID、PERCLOS
窗口和个人校准状态都会被释放。

## 安装与启动

从项目根目录执行：

```bash
python -m pip install -r fatigue_fastapi_service/requirements.txt

python -m uvicorn fatigue_fastapi_service.app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1
```

Linux CPU 服务器建议先安装 CPU 版 PyTorch，避免下载不需要的 CUDA 运行库：

```bash
python -m pip install \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch>=2.7,<3'
python -m pip install -r fatigue_fastapi_service/requirements.txt
```

模型在服务启动时加载，服务准备好后可检查：

```bash
curl http://127.0.0.1:8000/health
```

默认会在启动阶段完成一次模型预热，所以首个实时请求不会承担 YOLO 的首次
推理初始化开销。资源紧张或只想检查配置时，可设置
`FATIGUE_WARMUP=false`。

交互式 HTTP 文档位于 `http://127.0.0.1:8000/docs`。WebSocket 协议不会
完整显示在 OpenAPI 页面中，具体格式见下文。

> 必须使用 `--workers 1`。推理模型在一个进程中只允许一条活动流，避免
> 不同视频共享跟踪状态或同时访问 MediaPipe 推理器。需要多路视频时，可
> 运行多个服务进程/容器并为每个实例分配独立端口或 GPU。

## 快速验证

另开一个终端，从项目根目录运行内置客户端：

```bash
python -m fatigue_fastapi_service.client --source 0
```

本地视频、RTSP/HTTP 流也可作为客户端输入：

```bash
python -m fatigue_fastapi_service.client \
  --source /path/to/video.mp4

python -m fatigue_fastapi_service.client \
  --source 'rtsp://user:password@camera.example/stream'
```

客户端默认请求服务返回带框的 JPEG，并打开预览窗口。只输出逐帧 JSON：

```bash
python -m fatigue_fastapi_service.client \
  --source 0 \
  --no-annotated \
  --no-display
```

客户端默认直连并忽略 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境变量，
避免直连内网或公网 IP 时被本机 SOCKS 代理拦截。确实需要代理时可指定：

```bash
python -m fatigue_fastapi_service.client \
  --url ws://example.com/v1/streams/detect \
  --source 0 \
  --proxy auto
```

也可以用 `--proxy http://127.0.0.1:7890` 显式指定代理。SOCKS 代理还需要
安装 `python-socks`。

## WebSocket 协议

连接地址：

```text
ws://127.0.0.1:8000/v1/streams/detect?mirror=false&annotated=false
```

查询参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `mirror` | `false` | 推理前水平翻转输入帧 |
| `annotated` | `false` | 每个 JSON 结果后再返回一条标注 JPEG 二进制消息 |

连接成功后，服务首先发送 `ready` JSON。之后客户端每发送一条 JPEG 或 PNG
二进制消息，服务端就返回一条结果：

```json
{
  "type": "result",
  "sequence": 42,
  "timestamp": 3.581,
  "frame": {"width": 1280, "height": 720},
  "summary": {
    "face_count": 1,
    "drowsy_count": 0,
    "inattentive_count": 0
  },
  "people": [
    {
      "track_id": 1,
      "status": "ALERT",
      "bbox": {
        "x1": 420,
        "y1": 130,
        "x2": 710,
        "y2": 490,
        "confidence": 0.94
      },
      "landmarks_detected": true,
      "calibrating": false,
      "calibration_progress": 1.0,
      "state": {
        "ear": 0.29,
        "mar": 0.12,
        "perclos": 0.03,
        "fatigue_score": 3.2,
        "drowsy": false,
        "inattentive": false,
        "reasons": []
      }
    }
  ],
  "processing_ms": 58.4,
  "annotated_frame_follows": false
}
```

`state` 实际还包含原脚本 `FatigueState.to_dict()` 输出的全部时序指标。若
`annotated=true`，JSON 后的下一条 WebSocket 消息一定是对应的标注 JPEG。
这种一问一答形成自然背压，不会让慢速推理积压无限数量的旧帧。

支持两种文本控制消息：

```json
{"type": "ping"}
{"type": "reset"}
```

`reset` 会清空当前连接的全部 ID、疲劳历史和个人姿态校准，并把下一帧的
`sequence` 重新置为 0。

## 配置

环境变量示例见 `.env.example`。常用配置：

```bash
FATIGUE_DEVICE=mps \
FATIGUE_CALIBRATION_FRAMES=30 \
FATIGUE_THRESHOLD_CONFIG="$PWD/02_yolov8face_mediapipe/uta_rldd_threshold_results.json" \
python -m uvicorn fatigue_fastapi_service.app.main:app \
  --host 0.0.0.0 --port 8000 --workers 1
```

设备可设为 `auto`、`cpu`、`mps`、`0` 或 `cuda:0`。默认模型路径仍是：

```text
02_yolov8face_mediapipe/models/yolov8n-face-lindevs.pt
02_yolov8face_mediapipe/models/face_landmarker.task
```

## 测试

API 测试使用假的推理引擎，因此不会加载模型：

```bash
python -m pytest -q fatigue_fastapi_service/tests
```

这仍是研究原型，不能作为医疗诊断或唯一的安全报警来源。新 `track_id` 的
前 20 个有效人脸帧用于中性头部姿态/视线校准；校准期间应自然坐直并看向
摄像头。

## Ubuntu systemd + Nginx 部署文件

`deploy/` 中包含一套面向 `/opt/robotdog-fatigue-api` 的部署配置：

- `robotdog-fatigue-api.service`：单 worker systemd 服务；
- `robotdog-fatigue-api.env`：CPU、预热、帧大小和线程数配置；
- `nginx-robotdog-fatigue-api.conf`：HTTP 与 WebSocket 反向代理。

安装到服务器后可用：

```bash
sudo systemctl status robotdog-fatigue-api
sudo journalctl -u robotdog-fatigue-api -f
sudo nginx -t
```
