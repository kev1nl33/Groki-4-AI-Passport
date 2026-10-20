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
- **阶段 H（2026-10-20 最终候选，已保留 NVS 刷入，待用户验收）**：Build PASS；完整固件门禁
  exit=0，独立 archive verify PASS。full.bin SHA256
  `b8a62ad86df300ca7a553e1a1ff3cd273c7875ab06321ae5f3448e303068f36c`
  （1,549,696 字节）；ELF SHA256
  `78dd6be4595aa5991e8b7f8bf5cfdeb8d700d912fa22829db3f422d53ade9392`。
  归档 `firmware/build/firmware/b8a62ad86df300ca7a553e1a1ff3cd273c7875ab06321ae5f3448e303068f36c/`。
  网关全量 pytest **872 passed / 4 skipped，31.86 秒，exit=0**。
  设备 owner 按 ID 授予空闲许可，PLAY_DONE 才完成通知；30 秒双侧上限；
  OK 可取消准备/播放，LAN gemini_say 拒绝，通知 job 可查询或 CLI --wait。
  Device tests：经用户授权，归档独立 verify PASS，新旧已验证分区表逐字节一致；
  仅将 1,484,160 字节应用写入 `0x10000`，esptool Hash verified，实际擦写
  `0x10000–0x17afff`，未覆盖 NVS、PHY、bootloader 或分区表。设备自动联网，
  无需重设配网；14 秒日志未见崩溃/断言/存储错误，heap=120180/largest=110592，
  串口已关闭。正本网关受控重启加载 H，既有登录桥保持健康；唯一一条
  “任务完成了”带 flash 通知 job 1 最终 `done`（设备完成回执），随后设备仍在线。
  用户确认第一条已听到声音；随后按授权仅补发一条“屏幕提示测试”带 flash，
  job 2 最终 `done`；用户随后确认看到了提示、闪烁后回到 Ready。
  按授权仅发送一条较长 OK 停播测试（flash=false），job 3 最终 `failed`、
  `ok=false`，返回体无错误详情；设备仍在线、tts_active=false。用户随后确认
  播放中短按 OK“立即停止了，回到 Ready”，该用户可感知停播行为实机 PASS；
  job 未返回 cancelled：只读检查发现云取消先完成时，通知服务把
  `speak_text` 的 `ok=false/error=cancelled` 映射为 failed，且 job 未保存 error；
  属取消结果竞争缺陷。现存日志未记录回执/两个完成事件，无法证明现场精确先后。
  已仅修复 gateway：保留当前 job 的明确 cancelled 结果，其他真实失败仍为 failed；
  新增两种取消顺序、错误 ID 与迟到回执隔离回归。focused 128 passed；全量
  **876 passed / 4 skipped，30.31 秒，exit=0**。固件未改，未重跑固件构建。
  已受控重启网关加载修复（新实例 2026-10-20 11:12:35 启动，约4秒设备与云
  自动就绪）；通知入口仍仅回环监听，只读鉴权检查无凭据401、有凭据查询不存在job404。
  helper保持健康，未发通知。**修复已加载，cancelled设备复验未做**；历史job3失败不回写。
  job 4 为随后单条通知，3.569 秒完成；用户确认原回复说完后才播放、未打断且有间隔，
  体验通过。但提交采样 `tts_active=false`，忙时排队机制仍未实证。还需验准备阶段取消、并发 OK、
  延迟回复、取消尾帧、断线恢复与闪烁配网。取消时尚无云响应 ID 会关闭云会话，
  需验既有恢复提示与重新开始对话。保留 G4 改动，无重编、commit、push 或
  依赖安装；凭据仅由进程加载既有 dotenv，未显示或修改。

## 桌面工具恢复补验（2026-10-20）

- 根因：helper 在网关退出后保留旧 activeCall，新会话注册持续 `409 call_conflict`；
  另有失效 sandbox 连接永久缓存，使断流后注册持续 502。音频在线不能证明桌面工具就绪。
- 已修复最后事件流关闭释放归属、迟到清理隔离、30 秒未开流注册过期，以及失败连接
  按实例失效；仍在线冲突保持 409，业务错误/用户取消不清缓存，不自动重放 POST。
- 主机测试：两处 RED 已实际失败；修复后 Node helper **14 passed**，Python focused
  **74 passed**，最终 gateway 全量 **876 passed / 4 skipped，29.57 秒，exit=0**。
- 运行：helper 已从旧参考目录受控切换到正本；既有网关无需重启自动恢复 native ready。
  最终 helper 的 health/events 均 200；观察到一次断流后约 7 秒重新 presence 200/ready。
- 真实探针：一次语音会话文字注入确实触发 search_conversations，但当时下层 502，返回
  native_not_ready，不能判通过。随后无发声原生只读调用按实际 schema 使用
  if_missing=say-no-record，返回 kind=served、output 有 hits/more、无 error；不展示内容。
  前两次参数校验拒绝 invalid_input，未执行查询。未调用 send_task。
- **验证边界**：已证明原生只读搜索接口可用；电脑信息任务的实际执行/权限及设备原问句
  语音验收待用户补验。固件未改、未刷机，G4/H 改动保留，无依赖安装、commit 或 push。

## 桌面工具恢复补验（2026-10-20）

- 根因：helper 在网关退出后保留旧 activeCall，新会话注册持续 `409 call_conflict`；
  另有失效 sandbox 连接永久缓存，使断流后注册持续 502。音频在线不能证明桌面工具就绪。
- 已修复最后事件流关闭释放归属、迟到清理隔离、30 秒未开流注册过期，以及失败连接
  按实例失效；仍在线冲突保持 409，业务错误/用户取消不清缓存，不自动重放 POST。
- 主机测试：两处 RED 已实际失败；修复后 Node helper **14 passed**，Python focused
  **74 passed**，最终 gateway 全量 **876 passed / 4 skipped，29.57 秒，exit=0**。
- 运行：helper 已从旧参考目录受控切换到正本；既有网关无需重启自动恢复 native ready。
  最终 helper 的 health/events 均 200；观察到一次断流后约 7 秒重新 presence 200/ready。
- 真实探针：一次语音会话文字注入确实触发 search_conversations，但当时下层 502，返回
  native_not_ready，不能判通过。随后无发声原生只读调用按实际 schema 使用
  if_missing=say-no-record，返回 kind=served、output 有 hits/more、无 error；不展示内容。
  前两次参数校验拒绝 invalid_input，未执行查询。未调用 send_task。
- **验证边界**：已证明原生只读搜索接口可用；电脑信息任务的实际执行/权限及设备原问句
  语音验收待用户补验。固件未改、未刷机，G4/H 改动保留，无依赖安装、commit 或 push。

## 桌面助手“还是不可以”第二轮修复（2026-10-20）

- 最新实机 11:27:03.213 工具为 send_task，outcome=new_user_required，user_epoch=0。
  根因：manual_turn_mode 设备跳过 VAD，send_audio_stream_end 提交 commit 后未递增
  user_epoch，导致安全门禁始终拒绝。
- 修复：commit 成功后 +1，interrupt 同步清 manual_open/bytes，避免取消后意外授权。
  新增 5 条回归：有效提交授权一次、同轮重复拒绝、短/空/取消/发送失败不授权、
  内部 inject 不授权、auto VAD 照旧。GREEN 后全量 **881 passed / 4 skipped，30.81 秒**。
- 本机真实探针：临时 helper、生产 GbotVoiceBridge、合成只读 OS 请求音频、无 ESP32
  连接、无设备声音。commit 后 user_epoch=1，send_task dispatch relay=accepted，
  tool_outcomes send_task:native_accepted=1。已证明生产 manual 音频提交路径可授权并
  执行 send_task；不证明电脑信息实际读取内容或设备语音体验。
- 受控重启：gateway 15275 → 23267 / supervisor 23251，原 env/stdin 保留，helper 20097 未动。
  最终端口：gateway 23267 @ *:8765 / *:8766 / 127.0.0.1:8767；helper 20097 @ 127.0.0.1:18771。
  device.connected=true，native health/events 200。
- **用户最终验收（PASS）**：按住 OK 说“查看一下我电脑的信息”，用户明确确认
  “现在好像可以了，已经能正常查看我电脑了”。三根因（409 旧会话归属、502 失效
  sandbox 缓存、manual 录音未授权 send_task）经物理语音原问句实机验证通过。
  此为用户确认，独立于本机探针；不再发声或重启。

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

- 归档：`build/firmware/<full-bin-sha256>/`，包含
  `FoloToy-AI-Passport-full.bin`、组件镜像、匹配的 ELF/MAP 和分区表。
  交付前用 `python3 tools/archive_firmware.py verify <归档目录>` 校验。
- 日常更新默认保留 NVS，避免用户重新连接热点：先确认当前设备与待刷固件的
  分区布局和配置格式兼容，再使用归档里的组件镜像按实际偏移刷写。当前 factory
  应用偏移为 `0x10000`；不得把应用镜像写到 `0x0`。不使用会重新构建的命令替换
  已批准的固件，也不默认写入 NVS/PHY 区域。
- 首次完整安装或明确同意清除配置时，才使用从 `0x0` 写入的合并镜像
  `FoloToy-AI-Passport-full.bin`。它可能清除 Wi-Fi、网关地址、令牌和音量等设置。
- 每次刷机前确认端口、固件 SHA256、实际写入范围和用户授权；分区兼容性无法
  证明时先停止并说明原因，不自动擦除 Flash。

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
- 主动播报（阶段 H）：在 `.env` 设 `STACKCHAN_NOTIFY_TOKEN`（留空则复用
  `STACKCHAN_TOKEN`；两者都空则入口不启动）。网关启动后用
  `gateway/tools/notify "任务完成了" --flash` 让设备在空闲时开口；入口仅
  绑定 `127.0.0.1:8767`，对话中排队（60 秒过期），设备离线返回明确错误。
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
- 主动播报（阶段 H）由 Grok 语音会话复述，不保证逐字；语音会话不可用时
  播报失败（明确报错，不静默）。注入后若立刻按键说话，播报按现有半双工
  语义优先播放。

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
