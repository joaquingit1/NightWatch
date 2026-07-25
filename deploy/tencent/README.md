# 腾讯云问卷部署

本目录保存 `http://82.157.96.225/form` 使用的服务配置，不保存服务器密码。

- `nightwatch-form-web.service`：在本机 `3020` 端口运行问卷网页。
- `nightwatch-form-api.service`：在本机 `8020` 端口运行问卷数据接口，SQLite
  数据保存到 `/var/lib/nightwatch-form/nightwatch.db`。
- `robotdog-fatigue-api.nginx.conf`：只将 `/form`、`/_next`、公开 schema
  和提交接口转发给问卷服务；问卷列表、待护送队列和状态修改接口不对公网
  暴露，其余路径继续交给服务器原有的 `robotdog-fatigue-api`。

服务器目录采用发布版本加 `current` 软链接：

```text
/opt/nightwatch-form/
├── current -> releases/<version>
├── releases/
└── venv/
```

更新前先构建新发布目录；构建与接口检查通过后再切换 `current`，随后重启两个
systemd 服务。Nginx 配置替换前必须运行 `nginx -t`。
