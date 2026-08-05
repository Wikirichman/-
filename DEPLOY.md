# 公网部署说明

## 最便宜路线

先用 Render 免费 Web Service 跑通公网链接。缺点是免费实例会休眠，第一次打开会慢，且海外服务器访问 AkShare 的国内数据源可能偶尔不稳定。

如果主要给国内用户使用，后续建议换腾讯云轻量应用服务器或阿里云轻量应用服务器，国内访问更稳，但通常需要域名备案。

## 需要注册

- GitHub：用于放代码仓库。
- Render：用于免费部署公网 Web Service。
- 域名：可选。没有域名也可以直接用 Render 给的 `*.onrender.com` 地址。

## Render 部署步骤

1. 把本目录 `futures-monitor` 上传到一个 GitHub 仓库。
2. 登录 Render，选择 `New` -> `Blueprint`。
3. 选择刚才的 GitHub 仓库，Render 会读取 `render.yaml`。
4. 设置环境变量：
   - `FUTURES_MONITOR_USERNAME`: 登录账号，例如 `admin`
   - `FUTURES_MONITOR_PASSWORD`: 登录密码，必须自己填强密码
   - `FUTURES_MONITOR_SECRET_KEY`: Render 会自动生成
   - `FUTURES_MONITOR_COOKIE_SECURE`: `1`
5. 部署完成后，打开 Render 给的公网地址。

## 本地生产模式测试

```bash
FUTURES_MONITOR_DEPLOYED=1 \
FUTURES_MONITOR_USERNAME=admin \
FUTURES_MONITOR_PASSWORD='change-me' \
FUTURES_MONITOR_SECRET_KEY='replace-with-long-random-string' \
python -m futures_monitor.web --host 0.0.0.0 --port 8787
```

然后打开 `http://127.0.0.1:8787`。

## 当前登录机制

当前版本是一个共享账号密码，适合早期内测或小团队共用。真正对外开放注册时，建议升级为数据库用户系统，支持单独账号、重置密码、禁用用户和操作审计。
