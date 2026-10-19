# AI Passport Voice 交付说明

- 交付日期：2026-10-19
- 仓库：本目录即完整交付（`firmware/` + `gateway/` + 根级文档）
- 形态：单仓库双目录，公开开源（MIT）

## 1. 交付内容

| 部件 | 位置 | 说明 |
| --- | --- | --- |
| 设备固件 | `firmware/` | 基于 FoloToy ai-passport（MIT）的阶段 A–E 语音应用；随上游工程规则、skills、双语文档与测试门禁 |
| 电脑端网关 | `gateway/` | 基于 Grok Bot 网关（MIT）的 PCM 直通扩展；含 uv.lock 锁定依赖 |
| macOS 编译适配 | `tools/apple-cc` | 把门禁使用的 GNU `-Wl,--gc-sections` 映射为 Apple ld 的 `-Wl,-dead_strip`，仅本机构建需要 |
| 阶段设计文档 | `firmware/docs/assets/passport-voice.md`（双语） | 每阶段批准范围、实现细节与验收清单 |

上游来源与许可证汇总见 [NOTICE.md](NOTICE.md)。

## 2. 验证状态

按 Build / Host tests / Device tests / Unverified 分列：

- **Build**：阶段 E 最终固件合并镜像 `full.bin`
  SHA256 `9ee8a4f574c6cd295ba08617abf15a4c88606b474de2eb49e66e518d6c24c959`
  （1,541,408 字节），对应 ELF `9991d06f…`，独立归档校验（`tools/archive_firmware.py verify`）
  通过。本仓库源码与该构建一致（发布前仅更新了文档措辞）；重新构建时版本号
  与哈希会不同，属正常现象，以当次 `verify` 输出为准。
- **Host tests**：固件 `./tools/validate.sh --static` 通过（含 40 项接线契约
  测试）；网关 pytest 全量通过（859 passed / 2 skipped，发布副本中 live-view
  用例因未随附上游人脸追踪工具而模块级跳过，见该测试文件内说明）。
- **Device tests**：A–E 每阶段都经用户实机验收（见第 8 节）。
- **Unverified**：短按"按住说话"提示、断网自动重连、离线时 OK 立即重试、
  云端错误恢复、网关重启恢复 —— 均已实现并有单元测试覆盖，但未单独安排
  实机复测；随时可以补验。

## 3. 环境要求

- 设备：FoloToy AI Passport（ESP32-C3 单核、8 MB Flash、无 PSRAM、
  ST7789 240×320、ES8311、GPIO0 三键 ADC 电阻梯）。
- 电脑：macOS；已登录 Grok Bot 桌面应用；Node.js 22+；uv；系统 libopus
  （`uv sync --all-extras` 的 tts extra 需要）。
- 网络：设备与电脑同一 2.4 GHz 局域网。

## 4. 固件构建与刷机

```sh
cd firmware
# 激活 ESP-IDF 5.5.3 后：
./tools/validate.sh                       # 完整门禁（主机测试 + 固件 + 合并镜像归档）
# macOS 上主机链接需要适配器：
CC="$PWD/../tools/apple-cc" ./tools/validate.sh
```

- 产物：`build/firmware/<full-bin-sha256>/full.bin`（含 ELF/MAP/分区表等），
  交付前用 `python3 tools/archive_firmware.py verify <归档目录>` 校验。
- 刷机（示例，端口按实际填写）：

  ```sh
  esptool --chip esp32c3 -p /dev/cu.usbmodemXXXX -b 460800 \
      write_flash 0x0 build/firmware/<sha>/full.bin
  ```

- ⚠ 从 `0x0` 合并刷写可能清除设备已存数据（包括已保存的配网）；刷机前
  确认端口与固件 SHA256。

## 5. 电脑端网关启动（Grok 语音）

```sh
cd gateway
uv sync --all-extras
cp .env.example .env        # 然后编辑 .env，只改下面四项
```

`.env` 需要的四项（agent ID 在 Grok Bot 应用里查看，或用 `gbot bots list`）：

```dotenv
STACKCHAN_VOICE_BACKEND=gbot
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
STACKCHAN_TOOL_BOT=assistant
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
```

两个终端分别保持运行：

```sh
# 终端 1：登录桥（不读 .env，需要显式传参；只绑 127.0.0.1:18771）
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID \
STACKCHAN_TOOL_BOT=assistant \
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID \
node scripts/gbot_voice_session_bridge.mjs

# 终端 2：网关（WebSocket 0.0.0.0:8765，状态页 8766）
STACKCHAN_USB_DISABLE=1 STACKCHAN_WAKE_WORD=0 uv run --all-extras stackchan-mcp
```

- 检查：浏览器打开 `http://127.0.0.1:8766/debug/status`；设备连上后
  `device.connected=true`。
- 注意：网关绑定 `0.0.0.0:8765`；若本机 8765 已被其它服务占用，先处理再启动。
- Grok 语音桥走 Grok Bot 应用内部接口，App 大版本更新可能影响可用性；
  网关日志会给出具体错误。

## 6. 设备配网与日常使用

**首次配网**：无保存配置时开机自动进入配网 —— 屏幕显示热点名、随机密码和
`http://192.168.4.1`。手机连接该热点（旧密码不会被记住，忽略系统记忆），
浏览器打开 `192.168.4.1`，填入 2.4 GHz Wi-Fi 名称/密码和网关地址
`ws://<电脑局域网IP>:8765`，提交。设备拿到 IP 后才写入 NVS，随后热点关闭、
自动连接并进入 `Connecting → Ready`。

**重新配网**：空闲时长按 UP 进入。

**按键**：

| 操作 | 功能 |
| --- | --- |
| 长按 OK | 开始说话（`Listening...`），无需长按 1 秒，按下即采集 |
| 松开 OK | 提交本轮语音（`Thinking...` → 回复播放 `Speaking...` → 回到 `Ready`） |
| UP / DOWN 单击 | 音量 ±10（0–100），屏幕显示数值，重启保留 |
| 长按 UP（空闲） | 进入配网 |
| OK（配网中） | 取消并恢复原配置 |

## 7. 状态页含义

| 状态 | 含义 |
| --- | --- |
| `Starting` | 启动初始化 |
| `Connecting` | 正在连 Wi-Fi / 网关握手 |
| `Ready` | 就绪，可以按住 OK 说话 |
| `Retrying` | 网关断开，按退避自动重连（最长约 30 s 一轮） |
| `Listening...` | 正在采集你的说话 |
| `Thinking...` | 已提交，等待云端回复 |
| `Speaking...` | 正在播放回复 |
| `Voice unavailable` | 语音后端未就绪或启动失败 |
| `Audio error` | 音频参数不匹配或播放失败，已断开待重连 |

配网页为中文；状态页为英文（内置 CJK 字库缺少所需字形，中文子集留待后续）。

## 8. 实机验收记录（用户确认）

- **A**：开机自动进配网、表单配置、错误密码可重试、成功关热点、重启自动
  连回、取消保留旧配置。
- **B**：`Connecting → Ready`，网关 `device.connected=true`；停网关转
  `Retrying`、界面可用，重启网关约 28 s 自动回 `Ready`。
- **C**：按住 OK 上行 PCM，`Listening→Thinking→Ready`；网关收到音频并生成
  Grok 识别与回复文本（本阶段音频丢弃不播放）。
- **D**：可听到完整回复、音调正常，可连续多轮对话（修复 BSP 默认音量 0 与
  网关丢帧后确认）。
- **E**：松开 OK 后立即收到回复；音量重启保留正常。

## 9. 已知限制

- **半双工**：播放回复期间按 OK 无效，不能打断对方。
- 设备侧只实现 PCM（无 Opus）；无设备端唤醒词，必须按键说话。
- `ws://` 明文、仅局域网、无鉴权令牌（`STACKCHAN_TOKEN` 需固件配合发送
  Authorization，当前固件未实现）——只在可信局域网使用，不要暴露到公网。
- 单设备会话；Wi-Fi 仅 2.4 GHz。
- 状态页英文；无中文显示。
- 语音最短时长阈值为本地 200 ms 产品决策，非协议要求。
- Grok 语音依赖 Grok Bot 桌面应用登录与内部接口。

## 10. 安全约定

- `gateway/.env` 已被 gitignore；agent ID、API key、Wi-Fi 密码一律不进
  代码、日志与 git。
- 本仓库文档只使用占位符（`YOUR_AGENT_ID`、`<电脑局域网IP>`）。

## 11. 上游来源与许可证

见 [NOTICE.md](NOTICE.md)。固件与网关均为 MIT；修改保留了上游版权与
许可证声明；未使用 emotion-ball 数据。

## 12. 继续优化的建议入口

1. 补验第 2 节 Unverified 项（断网恢复、错误恢复、网关重启恢复、短按提示）。
2. 网关鉴权：`STACKCHAN_TOKEN` + 固件侧 Authorization 头。
3. 状态页中文字形子集（见 `firmware/docs` 的中文字体指南）。
4. 打断（barge-in）与设备端唤醒 —— 涉及音频架构变更，需先重估内存。
5. 根目录 CI：gateway pytest + firmware static checks。
6. 长对话下的内存/稳定性观察（网关输出预算已有，设备端可加计数上报）。
