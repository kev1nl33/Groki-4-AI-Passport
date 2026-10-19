**简体中文** · [English](passport-voice.md)

# Passport Voice — 已实现阶段 A–F

状态（2026-10-19）：阶段 A–F 全部实现并通过实机验收。阶段 F（状态驱动头像，固件 `29567dc1…`）实机验收通过：Ready 眨眼、按住说话睁大眼、Thinking 瞳孔扫动、回复时嘴随语音开合且音频不卡、配网界面完整显示热点密码。已批准方向为局域网 WebSocket、网关 PCM 扩展与临时 SoftAP 首次配网；范围按下列各阶段逐步扩大。各阶段验收清单保留执行前的原文，实测结果见仓库根目录 `DELIVERY.md`。

## 实现计划与边界

阶段 A 按五步推进。(1) 文件职责：`main/passport_config.[ch]` 承载可主机测试的纯逻辑表单解码、校验与连接模型；`main/passport_app.[ch]` 承载终身 owner 任务、临时 AP、HTTP 表单、NVS blob 与独立状态页；`main/main.c` 仅做启动接线。(2) 红绿测试：`tests/test_passport_config.c` 先对桩实现失败、实现后通过；`tests/test_stage_a_wiring.py` 固化回调、退出与凭据卫生契约。(3) 集成：回调只入队；`httpd_stop` 仅在 owner 调用；用 `WIFI_EVENT_STA_STOP` 屏障加现场 `esp_wifi_sta_get_ap_info`/`esp_netif_get_ip_info` 复核，防止上一代的迟到 GOT_IP 被当作新凭据成功。(4) 门禁：先 `./tools/validate.sh --static`，再 `--firmware` 并归档合并镜像。(5) 验收：下方真机清单，本次未执行。

1. 在 `main/` 增加纯逻辑表单解码、SSID/密码/URL 校验和状态机；测试放 `tests/`，接入已有验证门禁，先红后绿。
2. 单一 owner 任务串行执行网络和存储。Wi-Fi 和按钮回调只入队。HTTP 有界完整接收表单，校验会话令牌和同源请求，先返回响应，再交给 owner 处理候选配置。
3. 无凭据时启动 2.4 GHz WPA2 SoftAP，仅一个客户端。屏幕显示 AP 名、每次随机密码和 `http://192.168.4.1`，密码不写日志。候选 STA 拿到 IP 后才将所有字段写入单个版本化 NVS blob 并提交；此前保留旧配置。NVS 初始化错误不自动擦除。
4. 自建独立 LVGL 状态页，复用 BSP 显示、按钮、共享 I2C 和电池。空闲时长按 UP 重配；OK 取消并恢复旧配置，没有配网会话时用于重试错误。保存失败可恢复。不用组合键、demo 外壳或假音量功能。保持禁用 BLE，音频延迟到阶段 C 首次按住时才初始化。
5. 运行纯逻辑红/绿测试、ESP-IDF 5.5.3 完整门禁、归档校验。不刷机、不提交、不推送。

## UI 与资源策略

阶段 A 屏幕使用 English 状态文字：内建 Source Han Sans SC 16 CJK 子集未覆盖必要的中文固定字集。设备不直接显示动态 SSID 和 gateway 字符串，避免缺字或凭据暴露。状态页包含状态、IP、配网操作及右上角电量。浏览器配网页使用中文说明。HTTP/AP 仅在候选连接拿到 IP 且保存成功后退出；连接或保存失败保留配网入口。A 阶段不分配音频/I2S 和 BLE 运行资源。进入阶段 B 前需实测空闲堆、最大连续块及任务栈余量。阶段 B 待办：为临时配网 AP 增加空闲超时（无客户端配置时自动关闭）；阶段 A 中配网屏开启期间 AP 保持可用。

## 阶段 B（已批准，最小 WebSocket 握手）

Wi-Fi 拿到 IP 且存在已保存 gateway 后，owner 打开到已保存 `ws://主机:端口[/路径]` 的 WebSocket 连接（不产生其他网关流量），发送：

```json
{"type":"hello","version":1,"transport":"websocket","features":{"mcp":false},"audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}
```

仅接受有界（≤4096 字节）文本回复，顶层 `type` 为 `hello` 或 `server_hello`；只有 `version` 为 `1` 且 `session_id` 非空才视为握手成功，否则按失败重连。状态页区分 `Connecting...`、`Ready`（显示截断的会话号）和 `Retrying...`。连接与握手超时分别为 10 s 和 5 s；失败按 1、2、4、8、16、30 s 确定性指数退避（封顶 30 s），成功握手或 Wi-Fi 重连后重置。Wi-Fi、按键、HTTP 和 WebSocket 事件只入队；网络调用与 UI 更新都在 owner 任务，断线与重试不阻塞界面。阶段 B 明确不发送/接收音频，也不修改 gateway。客户端依赖 `espressif/esp_websocket_client`（1.8.0）通过 `main/idf_component.yml` 与 `dependencies.lock` 固定。

### 阶段 B 真机验收（尚未执行）

1. 在电脑启动 gateway，确认局域网内可访问其状态接口 `/debug/status`。
2. 用该 gateway 地址完成设备配置（阶段 A 流程或已有 NVS），并让设备带着已保存网络启动。
3. 屏幕应从 `Connecting...` 进入 `Ready`，gateway 的 `/debug/status` 应显示该会话；网关日志应体现 `transport=websocket`、`version=1`、`mcp=false` 以及 opus/16000/单声道/60 ms 参数。
4. 停止 gateway：屏幕应在握手超时内显示 `Retrying...`，界面保持可响应（长按 UP 仍能进入配网，OK 不被阻塞）；重启 gateway 后应按退避重连回 `Ready`。
5. 使用不可达端口：重试间隔应封顶 30 s，无重启、看门狗或界面卡死；观察堆与最大连续块稳定。
6. 关闭/恢复 Wi-Fi：设备应回到 `Retrying...`/`Connecting...` 后自动回到 `Ready`，无需手动重启。
7. 确认本阶段不传音频：gateway 不应要求设备发送麦克风帧。

## 阶段 C（已批准，PCM 上行）

握手进入 `Ready` 后按住 OK 开始一次语音。owner 先发送 `{"type":"listen","state":"start","mode":"manual","session_id":"..."}` 再开启采集；松开立刻停止采集，发送任务排空有界队列后才由 owner 发送 `listen.stop`。设备采集 16 kHz、16 bit、单声道 PCM，每 20 ms 通过二进制 WebSocket 帧发送 640 字节。hello 改为声明：

```json
{"type":"hello","version":1,"transport":"websocket","features":{"mcp":false},"audio_params":{"format":"pcm","sample_rate":16000,"channels":1,"frame_duration":20}}
```

网关只有在该参数元组完全一致时才启用 PCM 直通：640 字节帧不做 Opus 解码，直接交给现有唤醒门与 bridge；其他长度丢弃并计数；服务器 hello 返回 `pcm/24000/1/20`。Opus 客户端保持原路径与参数。设备忽略服务器返回的 `audio_params`。

采集与发送分属两个 3 KB 任务，中间是 8 帧（160 ms）有界队列。队列满时淘汰最旧帧并计数，网络发送绝不阻塞采集。音频/I2S 在首次按住时才初始化（I2S DMA 11,520 B），避免与 Wi-Fi、HTTP、WebSocket 启动争内存。按住时显示 `Listening...`，松开后显示 `Thinking...`；二进制发送失败会停止采集并显示 `Send failed`；WebSocket 或 Wi-Fi 断开立即中止本次语音并清空队列。本阶段收到的二进制下行帧直接丢弃、不做 JSON 解析，播放留到阶段 D；因此服务器 hello 中的 `pcm/24000/1/20` 下行参数只作协商声明，阶段 D 前不会真正产生下行音频。PCM 的服务器 hello 在注册后、Gemini 冷启动前立即发送，避免约 10 s 的上游建连撞上固件 5 s 握手窗口；Opus 旧客户端保持原顺序。松开、发送完成和断线事件同时以原子待处理标志合并、由 owner 轮询，队列满时也不会导致麦克风常开。所有网络写入都在 owner/发送任务中，并用互斥量保护客户端销毁；按键与 WebSocket 回调仍然只入队。

### 阶段 C 真机验收（尚未执行）

1. 连接可达网关进入 `Ready`；按住 OK，屏幕显示 `Listening...`，网关日志出现 `listen.start`。
2. 说话后松开，屏幕显示 `Thinking...`；确认二进制上行以 640 字节帧排空，随后收到 `listen.stop`，网关将该连接标记为 PCM 且不执行 Opus 解码。
3. 按住时停止网关：本次语音必须立即中止、队列清空，界面进入 `Retrying...` 且不卡死。
4. 按住时断开 Wi-Fi：同样立即停止；恢复后下一次按住正常开始。
5. 网络较慢时长按：确认内存有界、丢帧计数增长，无无限缓冲和界面卡顿。
6. 确认本阶段设备不播放音频，也不把二进制下行帧当 JSON 解析。

## 阶段 D（已批准，PCM 播放与半双工）

只有协商为精确 PCM 元组的连接启用 PCM 下行。设备解析服务器 hello 的 `audio_params`，要求 `format=pcm`、`sample_rate=24000`、`channels=1`、`frame_duration=20`；任一不符即显示 `Audio error` 并断开该连接，不播放、不回退 Opus。网关两路 TTS 源本身都是 24 kHz PCM16 单声道（Gemini Live `on_audio`，以及 Grok/Gbot bridge 的 `response.audio.delta`，其输出配置为 24000），因此直接下发、不做 Opus 编码。

网关把 24 kHz PCM 切成固定 960 字节（20 ms）帧，非整倍数尾部补静音，使用 16 帧（约 320 ms）有界队列；队列满时对输出 worker 施加背压，不丢正常回复音频，等待期间不持有发送锁。Grok 和 Gemini 独立接收控制/VAD。PCM 连接中等待与执行中的回调共同受电脑端预算限制：5,760,000 字节 PCM（120 秒）和 4096 个回调；超限会明确中止当前回复、回收额度、通知设备 stop，保持连接可供下一轮使用。取消也会唤醒阻塞的帧生产者。独立发送任务按每帧 20 ms 节奏发送（开头最多预灌 160 ms），不会把整段回复瞬间灌给设备。`tts.start`/`tts.stop` 语义不变：`start` 停止上行，`stop` 表示本段结束。Opus 连接保持原 60 ms 编码路径与无界队列。服务器 hello 声明的 `pcm/24000/1/20` 现在与实际发送一致。

设备端把 WebSocket 二进制帧跨分片、跨多帧重组为 960 字节帧，放入独立的 8 帧（约 160 ms）有界队列，满时丢最旧。独立播放任务在音频 I/O 互斥量保护下调用 `bsp_audio_write`。初始化明确设置适中的 50% 播放音量；16↔24 kHz 重新打开 codec 后保留该值。收到 `tts.start` 时先停止采集并丢弃未发送的上行音频，再把 codec 切到 24 kHz/16 bit/单声道；收到 `tts.stop` 后继续播放队列，队列排空并有 100 ms 宽限（或 5 s 无音频超时）后切回 16 kHz 并回到 `Ready`。状态页显示 `Speaking...`；说话/排空期间按下 OK 被忽略。断线、播放失败或格式切换失败会清空双向队列、恢复 16 kHz，并显示 `Retrying...`/`Audio error`，不会卡在 Speaking。二进制帧绝不进入 JSON 解析。文本在 WebSocket 回调内完成解析，owner 收到自包含的 hello/TTS 结果，因此背靠背的 `tts.start` 与 `sentence_start` 不会互相覆盖；非 hello 文本解析失败一律忽略，只有 hello 类型做完整校验。中止或断线时，已进入设备队列的约 160 ms 音频仍可能播完再完成停止/清空。

内存：下行队列 8 × (960 + 2) B 约 7.7 KB，播放任务 3 KB，另加两个小互斥量；16↔24 kHz 切换会重新打开 codec，但复用现有 I2S DMA。音频仍在首次按住或首段 TTS 时延迟初始化，启动日志保留空闲堆与最大连续块。

### 阶段 D 真机验收（尚未执行）

1. 连接返回 `pcm/24000/1/20` 的网关，确认 `Ready` 后触发一次回复。
2. 确认显示 `Speaking...`，音调和速度正常，且说话期间没有上行帧。
3. 说话期间按住 OK：应被忽略，界面保持 `Speaking...`。
4. 触发长回复：播放应连续且按节奏进行、内存有界；网络卡顿时不得一次性灌完整段。
5. 播放结束后回到 `Ready`，下一次按住仍以 16 kHz 正常录音。
6. 说话中断开 Wi-Fi/网关：播放立即停止、队列清空，界面离开 `Speaking...` 且无需重启。
7. 让网关返回错误的采样率/声道/帧长：设备必须显示 `Audio error` 且不播放。
8. 确认 Opus 客户端仍保持原有 60 ms Opus 行为（回归）。

## 真机验收（尚未执行）

验证首次启动及 WPA2 AP、正确/错误/不可用 STA、DHCP 超时、超长/畸形/中断/跨站表单、新配置保存及重启、保存失败重试/取消、旧配置恢复、各配网状态下 UP 长按和 OK 取消、反复进出与断线重连、HTTP 响应先于 AP 消失、电池缺失、屏幕裁切、堆/任务/socket 稳定。gateway 不应收到任何流量。本次实现不刷写设备。

## 阶段 E（已实现，2026-10-19 实机验收通过）

- PCM 按住说话使用明确手动轮次：松开即提交，不补尾部静音；空/过短输入清空，重复 stop 幂等。自动 VAD 与旧 Opus 客户端保持原行为。Grok 会话在接收音频前设为 `turn_detection: null`，松开后在同一有序 WebSocket 发送 `input_audio_buffer.commit` 再 `response.create`；committed 确认不再创建回复。200 ms 最低输入是本地产品阈值，并非已知的服务商协议要求。xAI 公开语音指南支持手动 commit，内部 Grok Bot 路由仍需真实端到端验收。
- UP/DOWN 单击按 10 调整音量至 0–100；UP 长按仍为配网，不额外触发音量单击。屏幕显示值。独立可选 NVS `volume` 键缺省 50，保留 `config_v1`；空闲 2 秒后保存，采集/播放时不写入。不擦除、不迁移。
- Wi-Fi 与 WebSocket 有界退避重连，离线时 OK 立即重试，语音未就绪不录音；配网保持可用。
- 传输连接与语音后端就绪分别显示。后端启动/失败、回复超时应进入可恢复且可行动的状态，不永久 Thinking。保留半双工与音频流格式。
- 只增加有界计数、heap/最大连续块/任务栈诊断；新增日志不含凭据和私人转录。不美化界面、不安装依赖、不刷机、不提交/推送、不重启当前网关加载半成品。
- 测试手动/自动轮次、空/短/重复 stop、音量兼容持久化、重连和后端恢复、播放后下一轮；完成 static/firmware/archive 门禁和网关测试，独立审查后再实机验收。

## 阶段 F（已实现，2026-10-19 实机验收通过）

状态驱动头像：五个普通 LVGL 对象（两只眼睛、两个瞳孔、一张嘴），固定在状态行与详情行之间。无图片、无画布、无新增帧缓冲，只失效这几个对象，渲染仍走现有 40 行局部缓冲。英文状态行保留，头像不会遮住状态。

动画决策在 `main/passport_face.c`，纯逻辑模块，不引用 LVGL 与音频，由 `tests/test_passport_face.c` 覆盖；应用层只把 0–100 的输出映射成对象几何。100 ms 的 LVGL 定时器（优先级 4，低于所有优先级 6 的音频任务）驱动它；播放任务把每帧 PCM 峰值写入一个原子变量供定时器读取，音频路径不加锁。

映射：Ready/Starting/Connecting 眨眼；Listening 睁大眼；Thinking 瞳孔扫动；Speaking 嘴随播放音量开合（立即张开，约 500 ms 收拢）；Retrying 慢速扫动；Audio error、Send failed、Voice unavailable 及配网/存储错误半闭眼下垂。配网界面打开时头像隐藏。

网关的 Grok PCM 链路不发送任何表情消息，头像只跟随设备本地状态。网关能为 MCP 客户端发送 `{"type":"llm","emotion":...}`，固件目前忽略该类型。

### 阶段 F 真机验收（尚未执行）

1. 开机确认头像出现在状态行下方并在 Ready 时眨眼，状态文字保持可读。
2. 按住 OK：眼睛立即睁大；松开：Thinking 期间瞳孔扫动。
3. 回复时嘴随语音开合且音频连续；回复长于 30 秒时确认无卡顿。
4. 停止网关：头像在 Retrying/Audio error 时呈现下垂表情，回到 Ready 后恢复眨眼。
5. 长按 UP 进入配网：头像隐藏、密码可读；取消后头像恢复。
6. 对比开机 `heap=`/`largest=` 日志与阶段 E（121348/110592），确认降幅很小。
