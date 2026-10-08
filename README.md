# WechatVideoSniffer

Windows 下基于 mitmproxy 的微信小程序视频请求 URL 捕获 MVP。它只观察并记录经过本地代理的 HTTP/HTTPS 请求，不下载视频、不绕过证书固定（certificate pinning）、DRM 或访问控制。

## 网络链路

```text
微信/测试浏览器 -> 127.0.0.1:8888 -> v2rayN HTTP/SOCKS5 代理 -> Internet
```

程序**不会修改 Windows 系统代理**，也不会关闭或改变 v2rayN。第一阶段请手动配置测试流量，测试结束后手动恢复原设置。

## 1. 安装

建议使用 Python 3.10 或更高版本：

```powershell
cd F:\Dev.Bn\WechatVideoSniffer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 2. 配置 v2rayN 上游

在 v2rayN 中查看当前本地 HTTP 或 SOCKS5 监听端口，然后修改 `config.json`。`10808` 只是示例，不能假定它就是你的端口。

HTTP 示例：

```json
"upstream_proxy": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 10808,
  "type": "http"
}
```

SOCKS5 示例把 `type` 改为 `socks5`，并填写 v2rayN 实际 SOCKS 端口。启动时程序会检查该端口是否可连接；配置错误时会给出可读提示，而不是 traceback。

`ssl_insecure` 默认是 `false`。只有在你明确知道上游服务器证书有问题时才应启用；它与安装 mitmproxy CA 不是一回事。

## 3. 首次启动与 CA 证书

启动：

```powershell
python main.py
```

保持程序运行，将一个测试浏览器的 HTTP 和 HTTPS 代理临时设为 `127.0.0.1:8888`，然后在该浏览器访问：

```text
http://mitm.it
```

选择 Windows，下载并安装 mitmproxy CA 证书到当前用户的“受信任的根证书颁发机构”。只应在自己的测试电脑上安装。测试结束后如果不再使用，可从 Windows 证书管理器删除 mitmproxy 证书。

安装证书后，通过这个代理访问普通 HTTPS 网站。如果网页正常打开，说明 `测试应用 -> WechatVideoSniffer -> v2rayN -> Internet` 链路可用。

> 安装 CA 使 mitmproxy 能解密经过它的 HTTPS 流量。请勿在不受信任的机器上安装，也不要分享 mitmproxy 私钥目录。

## 4. 让流量经过本工具

### 推荐的 MVP 验证顺序

1. 先只给测试浏览器配置 `127.0.0.1:8888`，验证 HTTPS 和一个公开 MP4 URL。
2. 再尝试让 Windows 微信使用同一代理，打开小程序并播放视频。
3. 使用 debug 模式判断微信请求是否进入代理：

```powershell
python main.py --debug
```

debug 模式会输出普通请求：

```text
[HTTP] GET https://example.com/path
```

以及默认被抑制的分片：

```text
[VIDEO SEGMENT] https://example.com/001.ts
```

### 关于 Windows 系统代理和 v2rayN

微信通常没有独立代理设置，是否读取 Windows 系统代理取决于微信版本和具体网络栈。若必须临时修改 Windows 系统代理：

1. 先截图或记下当前代理开关、地址、端口和 PAC 设置（通常由 v2rayN 管理）。
2. 保持 v2rayN 运行；把系统 HTTP/HTTPS 代理临时改为 `127.0.0.1:8888`。
3. 工具的 `upstream_proxy` 仍指向 v2rayN 的本地 HTTP/SOCKS5 端口，因此最终出口行为不变。
4. 测试完立即恢复原系统代理设置；也可在 v2rayN 中重新启用其“设置系统代理”来恢复。

不要同时让 v2rayN 把系统代理指向自身、又期望微信先经过 8888；系统代理只能有一个入口。也不要把 WechatVideoSniffer 的上游设为 8888，否则会形成代理环路。

本程序不会自动修改或恢复系统代理，因此关闭程序、Ctrl+C 或异常退出都不会改变系统设置。手动改过系统代理时，恢复责任仍在用户；这是 MVP 有意采用的安全策略。

## 5. 查看结果

识别规则包括 `.mp4`、`.m3u8`、`.m4s`、`.ts`、`.webm`、`.mov`，也会检查响应 `Content-Type`（如 `video/*` 和 m3u8 MIME 类型）。相同 URL 每次运行只输出一次。

m3u8 会正常显示并写入 `logs/videos.log`；大量 `.ts` 和 `.m4s` 分片默认不输出，只在 `--debug` 下显示简短行，避免刷屏。

## 6. 验收

1. **普通 HTTPS**：代理后的浏览器能访问 HTTPS 网站。
2. **测试 MP4**：访问 MP4 URL，控制台出现 `[VIDEO FOUND]`，且 `logs/videos.log` 有记录。
3. **微信**：用 `--debug` 启动，播放小程序视频。能看到微信请求说明代理链路生效；捕获到 MP4、m3u8、M4S 或 `video/*` 即为 MVP 成功。

## 7. 常见问题

### 完全看不到微信请求

微信流量没有进入 `127.0.0.1:8888`。检查 Windows 代理是否确实指向该地址、微信是否需要重启，以及该微信版本是否忽略系统代理。工具无法仅靠自身强制微信采用代理。

### 能看到普通请求，但视频未出现

视频可能使用了未覆盖的 MIME/协议、独立网络通道、QUIC/HTTP3，或者请求没有返回可识别响应。保留 debug 输出用于继续分析，但日志中可能含敏感 URL/token，分享前请脱敏。

### HTTPS 失败

先确认已通过 `http://mitm.it` 安装 mitmproxy CA，并重启测试应用。如果浏览器 HTTPS 正常而特定小程序失败，可能是 certificate pinning、不受支持的流量、QUIC/HTTP3、DRM 或其他访问控制：

```text
HTTPS interception failed
possible certificate pinning / unsupported traffic
```

本项目不尝试绕过这些保护。mitmproxy 控制台也可能给出 TLS 握手错误，可结合 debug 模式判断。

### 上游连接失败

确认 v2rayN 正在运行、`type` 与端口匹配。HTTP 端口必须配 `http`，SOCKS 端口必须配 `socks5`。不要照抄示例端口。

## 隐私与安全

捕获日志可能包含带签名、账号标识或短期凭据的 URL。不要公开分享 `logs/videos.log`。本工具仅用于你有权测试的流量和资源。
## 自动下载（第二阶段试验功能）

`config.json` 中：

```json
"auto_download": true,
"output_dir": "output"
```

开启后，程序会把捕获到的 MP4/WebM/MOV 放入 `output/`，下载在后台进行，不阻塞微信播放；同一 URL 每次运行只排队一次，已存在的文件会跳过，未完成文件使用 `.part` 后缀。控制台会显示 `[DOWNLOAD START]`、进度和 `[DOWNLOAD COMPLETE]`。

程序也会递归检查 JSON API 响应中的直接视频 URL。如果课程目录 API 已返回全部播放地址，可一次发现并下载；如果目录只返回课节 ID，仍需根据该站点详情接口展开。相关课程接口会保存到 `logs/course_api.jsonl` 供分析。该文件可能包含登录信息和个人数据，请勿分享。

自动下载只用于你有权访问、保存的内容，不绕过付费权限、DRM、证书固定或其他访问控制。带签名的 URL 可能过期；下载失败时可在登录会话有效期间重新打开目录。

## GUI 桌面版

双击项目根目录的 `Start-WechatVideoSniffer.bat` 启动界面，或运行：

```powershell
.\.venv\Scripts\pythonw.exe gui.py
```

界面提供：

- **启动监测**：记录当前 Windows 代理，启动抓取服务，再将系统代理临时切换到 `127.0.0.1:8888`。
- **停止监测**：恢复启动前的代理设置并停止服务。
- **关闭窗口**：执行与“停止监测”相同的恢复流程。
- **视频或播放页面地址**：支持直接 MP4/M3U8、普通 HTTP(S) 播放页面和 `#小程序://` 口令。普通页面会在浏览器打开并等待播放请求；小程序口令会复制到剪贴板，需要在微信中打开并播放。
- **保存位置**：可输入或浏览选择目录，并可直接打开目录。
- **实时日志**：显示捕获和下载状态。

代理恢复信息持久化在 `logs/proxy_backup.json`。如果上次异常退出，GUI 下次启动会优先恢复原代理。强制结束进程、系统崩溃或断电无法执行正常退出逻辑，但该备份可用于下次启动恢复。

> 直接地址不代表程序能绕过登录、授权、DRM 或证书固定。对于需要微信会话的小程序页面，仍需在微信中打开并点击播放，程序随后自动捕获和下载。
