# 腾讯云部署记录

部署日期：2026-07-23

## 服务地址

- HTTP API：`http://82.157.96.225/`
- 健康检查：`http://82.157.96.225/health`
- OpenAPI 文档：`http://82.157.96.225/docs`
- 实时 WebSocket：`ws://82.157.96.225/v1/streams/detect`

公网客户端示例：

```bash
python -m fatigue_fastapi_service.client \
  --url ws://82.157.96.225/v1/streams/detect \
  --source 0
```

## 服务器布局

- SSH 用户：`ubuntu`
- 程序目录：`/opt/robotdog-fatigue-api`
- Python 环境：`/opt/robotdog-fatigue-api/.venv`
- systemd 单元：`robotdog-fatigue-api.service`
- 环境配置：`/etc/robotdog-fatigue-api.env`
- Nginx 站点：`/etc/nginx/sites-available/robotdog-fatigue-api`
- Uvicorn：仅监听 `127.0.0.1:8000`
- Nginx：监听公网 `80` 并代理 HTTP/WebSocket

服务器为 2 核 CPU、2 GB RAM、无 GPU。已添加并持久化 4 GB
`/swapfile`，服务配置为 CPU 推理和 2 个计算线程。

## 运维命令

```bash
sudo systemctl status robotdog-fatigue-api
sudo systemctl restart robotdog-fatigue-api
sudo journalctl -u robotdog-fatigue-api -f

sudo nginx -t
sudo systemctl reload nginx
```

服务和 Nginx 都已启用开机自启。

## 已验证内容

- Python 依赖通过 `pip check`；
- YOLOv8-Face 和 MediaPipe 模型成功加载及预热；
- 公网 `/health` 返回 `status=ok`；
- 公网 WebSocket 可接收人脸 JPEG、返回逐帧 JSON 和标注 JPEG；
- 测试样本返回一张人脸、`track_id=1`、landmarks 和校准状态；
- 224×224 测试样本在该 CPU 实例上的单帧处理时间约为 243 ms；
- 部署未占用或修改原有的 `3010` 端口服务。

## 安全说明

当前仅使用公网 HTTP/WS，没有 TLS 和 API 身份验证。不要通过它传输敏感视频。
正式对外使用前，应绑定域名、签发 HTTPS 证书，并增加 API token 或其他访问
控制。SSH 登录密码不记录在仓库中；由于密码曾通过聊天传递，建议部署完成后
轮换密码或改用 SSH key。
