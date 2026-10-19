**简体中文** | [English](README.md)

# AI Passport Voice

给 [FoloToy AI Passport](https://github.com/FoloToy/ai-passport)（ESP32-C3、
8 MB Flash、240×320 ST7789 屏、ES8311 音频、三颗按键）做的语音对话应用：
设备通过局域网 WebSocket 连到你电脑上的网关，用你已登录的 Grok 账号说话。

一个仓库，两个部分：

- **`firmware/`** — ESP-IDF 5.5.3 固件，基于上游 MIT 固件修改：SoftAP 首次
  配网、状态页、按住说话、16 kHz PCM 上行、24 kHz PCM 播放、音量调节与
  NVS 持久化、有界断线重连。
- **`gateway/`** — 电脑端 Python 网关，基于 [Grok Bot](https://github.com/JosssphZhou/groki-bot)
  网关（MIT）修改，新增显式协商的 PCM 直通模式：上行 `pcm/16000/1/20`、
  下行 `pcm/24000/1/20`、手动轮次提交、按帧节奏发送、背压与单轮输出预算。
  Opus 客户端保持原有 60 ms 路径不变。

## 工作方式

```text
AI Passport ⇄ Wi-Fi ⇄ WebSocket ws://<电脑局域网IP>:8765 ⇄ 网关 ⇄ Grok 登录态语音
```

语音使用你电脑上已登录的 Grok Bot 桌面应用（一个本地 Node 会话桥保持登录），
不需要单独的 xAI 语音 API key。音频是裸 PCM16，设备端不做 Opus，省下无
PSRAM 的内存预算。

## 当前状态

阶段 A–E 已全部实现并通过实机验收（2026-10-19）：配网与 NVS 持久化、
WebSocket 握手与重连、PCM 上行与识别、24 kHz 播放与半双工、松开即提交与
音量持久化。完整验收记录、已知限制和未复测项见 [DELIVERY.md](DELIVERY.md)。

## 快速开始

1. **构建固件** — 激活 ESP-IDF 5.5.3，在 `firmware/` 下执行：

   ```sh
   ./tools/validate.sh          # macOS：CC="$PWD/../tools/apple-cc" ./tools/validate.sh
   ```

   合并镜像从 `0x0` 刷入（合并刷写可能清除设备已存数据），细节见
   [DELIVERY.md](DELIVERY.md)。

2. **安装网关**（macOS、Grok Bot 桌面应用已登录、Node 22+、
   [uv](https://docs.astral.sh/uv/)）：

   ```sh
   cd gateway && uv sync --all-extras
   ```

3. **配置 `gateway/.env`**（agent ID 换成你自己的）：

   ```dotenv
   STACKCHAN_VOICE_BACKEND=gbot
   STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
   STACKCHAN_TOOL_BOT=assistant
   STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
   ```

4. **启动登录桥和网关** — 具体命令见 [DELIVERY.md](DELIVERY.md)；用
   `http://127.0.0.1:8766/debug/status` 检查。

5. **设备配网** — 首次开机自动开热点（名称和随机密码显示在屏幕上）；手机
   连接后打开 `http://192.168.4.1`，填 Wi-Fi 和 `ws://<电脑局域网IP>:8765`。

6. **开始对话** — 屏幕显示 `Ready` 后，按住 OK 说话，松开发送；UP/DOWN 调
   音量。

## 文档

- [DELIVERY.md](DELIVERY.md) — 交付说明：构建、刷机、网关启动、配网、按键、
  状态、限制（中文）。
- [firmware/docs/assets/passport-voice.zh_CN.md](firmware/docs/assets/passport-voice.zh_CN.md)
  — 各阶段设计与验收计划（[English](firmware/docs/assets/passport-voice.md)）。
- [gateway/README.zh-CN.md](gateway/README.zh-CN.md) — 网关参考文档（上游文档，
  保留 Grok 后端细节）。

## 许可证与来源

MIT。`firmware/` 基于上游 [FoloToy/ai-passport](https://github.com/FoloToy/ai-passport)
（MIT）；`gateway/` 基于上游 [JosssphZhou/groki-bot](https://github.com/JosssphZhou/groki-bot)
（MIT，源自 kisaragi-mochi/stackchan-mcp）。详见 [NOTICE.md](NOTICE.md)。
API key 和 agent ID 只放在 `gateway/.env`，该文件不会提交。
