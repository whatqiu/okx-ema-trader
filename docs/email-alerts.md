# 一键启动和信号邮件

双击根目录 `start-platform.bat`，自动构建有变化的前端，启动后端并打开浏览器，无需运行 `npm run dev`。首次构建需安装 Node.js。保持启动窗口开启，Ctrl+C 停止。重复启动只打开现有服务。

邮件仅在自动交易扫描识别做多或做空信号时发送，包含币种、方向、信号 K 线时间和原因。无信号、启动和行情更新不发邮件。需要在平台开启自动扫描并添加币种；平台关闭时不扫描。受风控或仓位限制而未成交的信号仍提醒，邮件不代表成交。

本地配置文件：`config/email.local.json`（已忽略，不提交 Git）。参照 `config/email.example.json` 填写：

- `enabled`：填好后设为 `true`。
- `host`、`port`、`security`：发件邮箱服务商提供的 SMTP 地址、端口、加密方式（`ssl` 或 `starttls`）。
- `username`：发件邮箱。
- `password`：SMTP 授权码，仅在本地填写。
- `recipient`：`qiuxingxin@126.com`。

配置保存后自动读取，无需重启。相同币种、方向和 K 线只入队一次；失败后 60 秒重试，重启后继续待发任务。SMTP 接收后、本地记录前中断可能造成重复邮件。配置前的历史信号不补发；关闭提醒期间不记录新任务，重新启用后继续已有待发任务。错误和发送记录见 `logs/platform.log`。
