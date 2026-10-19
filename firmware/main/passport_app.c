#include "passport_app.h"
#include "passport_config.h"
#include "passport_ws.h"
#include "passport_audio.h"
#include "passport_downlink.h"
#include "passport_face.h"
#include "bsp_display.h"
#include "bsp_i2c.h"
#include "bsp_battery.h"
#include "bsp_audio.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "esp_websocket_client.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "lvgl.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CONNECT_US (25LL * 1000000)
typedef enum { EV_KEY, EV_FORM, EV_LINK, EV_IP, EV_DOWN, EV_STOP, EV_WS, EV_AUDIO, EV_TEXT } event_kind_t;
typedef enum { WS_OPEN, WS_CLOSED, WS_ERROR } ws_kind_t;
typedef enum {
    AUDIO_SEND_FAILED,
    AUDIO_STOP_SENT,
    AUDIO_PLAY_DONE,
    AUDIO_PLAY_FAILED,
} audio_kind_t;
typedef struct {
    event_kind_t kind;
    unsigned generation;
    bsp_btn_t key;
    bsp_btn_ev_t key_event;
    pv_config_t config;
    esp_netif_ip_info_t ip;
    uint8_t ssid[32];
    uint8_t ssid_len;
    uint8_t bssid[6];
    ws_kind_t ws;
    unsigned ws_client_generation; /* stale-client guard; never a raw pointer */
    audio_kind_t audio;
    pv_text_kind_t text;
    pv_server_hello_t hello; /* parsed in the WS callback, not read back later */
} app_event_t;
/* Queue is lifetime-long: callbacks never race a deleted queue or screen. */
/* RAM budget (ESP32-C3, no PSRAM): owner task 6144 B stack, HTTP server
   6144 B, event queue 8 * ~500 B ~ 4 KB, setup page ~1.9 KB Flash.
   Stage B adds the WebSocket task (now 6144 B because Stage D parses bounded
   text in the callback) and a 4097 B receive buffer.
   Stage C adds, only after the first hold-to-talk and only when the BSP
   audio init succeeds: I2S full-duplex DMA 6 * 240 frames * 2 ch * 2 B *
   TX/RX = 11,520 B, capture task 3072 B, send task 3072 B, and a bounded
   PCM queue of 8 * (640 + 2) B ~ 5.1 KB. Stage D adds, with the same lazy
   downlink queue 8 * (960 + 2) B ~ 7.7 KB is allocated at startup so a
   first TTS reply cannot lose its opening frames during audio bring-up;
   the playback task 3072 B, one I2S/codec io mutex, and one WS reassembly
   mutex are created with the other audio resources. 16 kHz↔24 kHz
   switches close/reopen the codec (no extra persistent DMA); the codec
   buffer and DMA are reused. Audio is deliberately lazy so Wi-Fi/HTTP/
   WebSocket startup does not compete with the DMA allocation; the boot
   log keeps free heap and the largest free block for review. */
static QueueHandle_t s_queue;
static atomic_uint s_generation;
static atomic_uint s_wifi_loss; /* generation+1; zero means no pending loss */
static atomic_bool s_form_pending;
static atomic_bool s_form_open;
static pv_ws_model_t s_ws;
static esp_websocket_client_handle_t s_ws_client;
static atomic_uint s_ws_client_generation;
/* Worst case ws:// + 63 host + ':' + 5 port digits + 127 path + NUL = 202. */
static char s_ws_uri[210];
static char s_ws_rx[PV_WS_MAX_MESSAGE + 1]; /* WS-callback reassembly slot */
static int s_ws_msg_opcode;                  /* 0x1 text / 0x2 binary */
typedef struct { uint16_t len; uint8_t data[PV_AUDIO_FRAME_BYTES]; } pcm_frame_t;
typedef struct { uint16_t len; uint8_t data[PV_DOWN_FRAME_BYTES]; } down_frame_t;
static pv_audio_model_t s_audio;
static pv_audio_stats_t s_capture_stats; /* owned by the capture task */
static QueueHandle_t s_pcm_queue;
static TaskHandle_t s_capture_task, s_send_task;
static atomic_bool s_capture_run, s_capture_idle, s_stop_pending, s_stop_sent;
static atomic_uint s_pending; /* coalesced release/stop/disconnect bits */
static atomic_uint s_pending_ws_generation; /* client that reported the loss */
static atomic_uint s_audio_dropped;
static bool s_audio_initialized;
static bool s_bsp_audio_inited;
static SemaphoreHandle_t s_ws_send_lock;
/* Stage D downlink: separate bounded queue and codec-format arbitration. */
static pv_down_model_t s_down;
static pv_down_rx_t s_down_rx;       /* armed gate + WS-callback accumulator */
static pv_audio_stats_t s_down_stats; /* owned by the WS callback */
static unsigned s_down_drop_logged;   /* WS-callback log rate limit */
static QueueHandle_t s_down_queue;
static TaskHandle_t s_play_task;
static SemaphoreHandle_t s_audio_io_lock;  /* bsp_audio_read/write/set_format */
static SemaphoreHandle_t s_down_acc_lock;  /* WS reassembly across tasks */
static atomic_bool s_down_audio_ok;        /* server hello matched PCM 24k */
static atomic_bool s_down_format_24k;      /* playback format is active */
static atomic_bool s_down_stop_pending;    /* tts.stop: drain before Ready */
static atomic_uint s_down_dropped;
static bool s_down_error;                  /* owner-only UI flag */
/* Stage F avatar: the play task publishes a peak level, the LVGL timer reads
   it. One atomic, no lock on the audio path. */
static atomic_uchar s_mouth_level;
static pv_face_model_t s_face;
static lv_timer_t *s_face_timer;
static lv_obj_t *s_face_box, *s_eye_l, *s_eye_r, *s_pupil_l, *s_pupil_r, *s_mouth;
static pv_model_t s_model;
static pv_config_t s_saved, s_active;
static bool s_nvs_ready, s_wifi_ready, s_wifi_started, s_sta_enabled;
static uint8_t s_volume = 50;
static bool s_volume_dirty;
static int64_t s_volume_changed, s_wifi_retry_at, s_thinking_at;
static unsigned s_wifi_retries;
static int s_backend; /* 0 preparing, 1 ready, 2 error */
static bool s_wait_reply, s_up_long;
static atomic_int s_voice_notice; /* latest state survives a full event queue */
static atomic_bool s_voice_available; /* callback gate follows ordered ready/error */
static int64_t s_backend_at, s_diag_at;
static bool s_setup, s_barrier, s_attempt_pending, s_new_candidate;
static bool s_close_pending;
static int64_t s_deadline, s_close_at, s_last_input;
static httpd_handle_t s_http;
static esp_netif_t *s_sta, *s_ap;
static esp_event_handler_instance_t s_wifi_handler, s_ip_handler;
static char s_ap_name[25], s_ap_password[17], s_token[33], s_ip_text[24];
static uint8_t s_bssid[6];
static lv_obj_t *s_screen, *s_status, *s_details, *s_hint, *s_battery, *s_volume_label;
static const char *TAG = "passport";

/* Stage F avatar geometry, in screen pixels. The face box sits between the
   status line and the detail lines; only objects inside it are invalidated. */
#define FACE_X 44
#define FACE_Y 92
#define FACE_W 152
#define FACE_H 130
#define EYE_W 40
#define EYE_MAX_H 56
#define EYE_MIN_H 4
#define EYE_Y 16
#define EYE_L_X 20
#define EYE_R_X 92
#define PUPIL_W 14
#define PUPIL_H 18
#define MOUTH_W 52
#define MOUTH_MAX_H 36
#define MOUTH_MIN_H 4
#define MOUTH_Y 92

static void face_apply(const pv_face_out_t *out);
static void face_timer_cb(lv_timer_t *timer);

static void audio_ws_sync(void);
static void audio_ws_lost(void);
static void audio_downlink_reset(bool error);
static void audio_downlink_start(void);
static void audio_downlink_stop(void);
static void audio_downlink_finish(bool error);

static void show(const char *status, const char *hint)
{
    /* Battery I2C is owned by this task, never executed in LVGL callbacks. */
    int battery = bsp_battery_soc();
    if (!bsp_lvgl_lock(500)) return;
    lv_label_set_text(s_status, status);
    if (s_setup) lv_label_set_text_fmt(s_details, "Join Wi-Fi\n%s\nPassword\n%s\nhttp://192.168.4.1", s_ap_name, s_ap_password);
    else if (s_ws.state == PV_WS_READY && s_ws.session_id[0])
        lv_label_set_text_fmt(s_details, "%s\nSession %.16s", s_ip_text, s_ws.session_id);
    else
        lv_label_set_text_fmt(s_details, "%s\nGateway: %s", s_ip_text,
                              s_ws.state == PV_WS_READY ? "ready" : "connecting");
    lv_label_set_text(s_hint,hint);
    lv_label_set_text_fmt(s_volume_label,"Volume %u",(unsigned)s_volume);
    if (battery < 0) lv_label_set_text(s_battery, "--");
    else lv_label_set_text_fmt(s_battery, "%d%%", battery);
    /* Setup shows the hotspot password, which needs the tall detail area the
       avatar normally occupies; swap the two rather than squeezing the text. */
    if (s_face_box) {
        if (s_setup) {
            lv_obj_add_flag(s_face_box, LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_pos(s_details, 18, 111);
            lv_obj_set_size(s_details, 204, 140);
        } else {
            lv_obj_remove_flag(s_face_box, LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_pos(s_details, 18, 226);
            lv_obj_set_size(s_details, 204, 34);
        }
    }
    bsp_lvgl_unlock();
}
/* Wi-Fi is connected; report the gateway handshake phase instead of a bare
   "connected" so a gateway outage is visible without blocking the UI. */
static void show_online(void)
{
    const char *status;
    if (s_down_error) {
        show("Audio error", "Gateway must send\npcm 24k mono 20ms");
        return;
    }
    if (s_ws.state == PV_WS_READY && pv_down_speaking(&s_down)) {
        show("Speaking...", "Hold OK after speech");
        return;
    }
    if(s_ws.state==PV_WS_READY && s_backend!=1){
        show(s_backend==2 ? "Voice unavailable" : "Voice starting...", "OK: retry");return;
    }
    if (s_ws.state == PV_WS_READY) {
        if(s_wait_reply){show("Thinking...","Waiting for voice");return;}
        switch (s_audio.state) {
        case PV_AUDIO_LISTENING: status = "Listening..."; break;
        case PV_AUDIO_THINKING: status = "Thinking..."; break;
        case PV_AUDIO_ERROR: status = "Send failed"; break;
        default: status = "Ready"; break;
        }
    } else {
        status = s_ws.state == PV_WS_RETRY_WAIT ? "Retrying..." : "Connecting...";
    }
    show(status, s_ws.state == PV_WS_READY ? "Hold OK to talk\nHold UP: setup"
                                           : "Gateway status\nHold UP: setup");
}
static lv_obj_t *label(int y, int height, const lv_font_t *font)
{
    lv_obj_t *o = lv_label_create(s_screen);
    lv_obj_set_pos(o, 18, y); lv_obj_set_size(o, 204, height);
    lv_obj_set_style_text_font(o, font, 0);
    lv_label_set_long_mode(o, LV_LABEL_LONG_WRAP);
    return o;
}

/* Map the 0..100 face model onto five plain objects. Sizes change, positions
   stay inside the face box, so only that region is invalidated. */
static void face_apply(const pv_face_out_t *out)
{
    const int eye_h = EYE_MIN_H + (int)out->eye_open * (EYE_MAX_H - EYE_MIN_H) / 100;
    const int eye_y = EYE_Y + (EYE_MAX_H - eye_h) / 2;
    const int mouth_h = MOUTH_MIN_H + (int)out->mouth_open * (MOUTH_MAX_H - MOUTH_MIN_H) / 100;
    lv_obj_set_pos(s_eye_l, EYE_L_X, eye_y);
    lv_obj_set_size(s_eye_l, EYE_W, eye_h);
    lv_obj_set_pos(s_eye_r, EYE_R_X, eye_y);
    lv_obj_set_size(s_eye_r, EYE_W, eye_h);

    if (out->eye_open < PV_FACE_PUPIL_MIN_OPEN) {
        lv_obj_add_flag(s_pupil_l, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_pupil_r, LV_OBJ_FLAG_HIDDEN);
    } else {
        /* Pupil travel is bounded to the eye it sits in. */
        const int dx = (int)out->pupil_dx - 50;
        const int dy = (int)out->pupil_dy - 50;
        const int px = EYE_W / 2 - PUPIL_W / 2 + dx * (EYE_W - PUPIL_W) / 100;
        const int py = eye_h / 2 - PUPIL_H / 2 + dy * (eye_h - PUPIL_H) / 100;
        lv_obj_set_pos(s_pupil_l, EYE_L_X + px, eye_y + py);
        lv_obj_set_pos(s_pupil_r, EYE_R_X + px, eye_y + py);
        lv_obj_remove_flag(s_pupil_l, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_pupil_r, LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_set_pos(s_mouth, (FACE_W - MOUTH_W) / 2, MOUTH_Y + (MOUTH_MAX_H - mouth_h) / 2);
    lv_obj_set_size(s_mouth, MOUTH_W, mouth_h);
}

/* Runs on the LVGL task (priority 4, below every audio task). 10 Hz is enough
   for the blink and mouth shapes; the status line stays the precise signal. */
static void face_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    if (!s_face_box || lv_obj_has_flag(s_face_box, LV_OBJ_FLAG_HIDDEN)) return;
    const char *text = lv_label_get_text(s_status);
    pv_face_out_t out;
    pv_face_tick(&s_face, (uint32_t)(esp_timer_get_time() / 1000),
                 esp_random(), pv_face_state_for_status(text),
                 atomic_load(&s_mouth_level), &out);
    face_apply(&out);
}

static lv_obj_t *face_part(lv_color_t color, int radius)
{
    lv_obj_t *o = lv_obj_create(s_face_box);
    lv_obj_set_style_bg_color(o, color, 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_border_width(o, 0, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static bool ui_start(void)
{
    if (!bsp_lvgl_lock(1000)) return false;
    s_screen=lv_obj_create(NULL);
    lv_obj_set_style_bg_color(s_screen, lv_color_hex(0x10212C), 0);
    lv_obj_set_style_text_color(s_screen, lv_color_hex(0xE9F1F5), 0);
    lv_obj_remove_flag(s_screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *title=label(18,25,&lv_font_montserrat_14);
    lv_label_set_text(title,"PASSPORT / A");
    s_battery=label(18,25,&lv_font_montserrat_14);
    lv_obj_set_pos(s_battery,174,18); lv_obj_set_width(s_battery,48);
    lv_obj_set_style_text_align(s_battery, LV_TEXT_ALIGN_RIGHT,0);
    s_status=label(52,30,&lv_font_montserrat_20);
    lv_obj_set_style_text_color(s_status,lv_color_hex(0x64DEC7),0);
    s_details=label(226,34,&lv_font_montserrat_14);
    s_volume_label=label(258,18,&lv_font_montserrat_14);
    s_hint=label(276,36,&lv_font_montserrat_14);
    lv_label_set_text(s_status,"Starting...");

    /* Stage F: five plain objects, no images, no extra frame buffer. */
    s_face_box = lv_obj_create(s_screen);
    lv_obj_set_pos(s_face_box, FACE_X, FACE_Y);
    lv_obj_set_size(s_face_box, FACE_W, FACE_H);
    lv_obj_set_style_bg_opa(s_face_box, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(s_face_box, 0, 0);
    lv_obj_set_style_pad_all(s_face_box, 0, 0);
    lv_obj_remove_flag(s_face_box, LV_OBJ_FLAG_SCROLLABLE);
    s_eye_l = face_part(lv_color_hex(0xE9F1F5), LV_RADIUS_CIRCLE);
    s_eye_r = face_part(lv_color_hex(0xE9F1F5), LV_RADIUS_CIRCLE);
    s_pupil_l = face_part(lv_color_hex(0x10212C), LV_RADIUS_CIRCLE);
    s_pupil_r = face_part(lv_color_hex(0x10212C), LV_RADIUS_CIRCLE);
    lv_obj_set_size(s_pupil_l, PUPIL_W, PUPIL_H);
    lv_obj_set_size(s_pupil_r, PUPIL_W, PUPIL_H);
    s_mouth = face_part(lv_color_hex(0x64DEC7), 8);
    pv_face_init(&s_face, (uint32_t)(esp_timer_get_time() / 1000), esp_random());
    pv_face_out_t initial;
    pv_face_tick(&s_face, (uint32_t)(esp_timer_get_time() / 1000), esp_random(),
                 PV_FACE_IDLE, PV_FACE_MOUTH_NONE, &initial);
    face_apply(&initial);
    s_face_timer = lv_timer_create(face_timer_cb, 100, NULL);

    lv_screen_load(s_screen); bsp_lvgl_unlock(); return true;
}
static void random_hex(char *out, size_t bytes)
{
    static const char digits[]="0123456789abcdef";
    for (size_t i=0;i<bytes;++i) { uint8_t b=(uint8_t)esp_random(); out[2*i]=digits[b>>4];out[2*i+1]=digits[b&15]; }
    out[2*bytes]=0;
}
static esp_err_t respond(httpd_req_t *req, const char *status, const char *body)
{
    httpd_resp_set_status(req,status);
    httpd_resp_set_type(req,"text/html; charset=utf-8");
    httpd_resp_set_hdr(req,"Cache-Control","no-store");
    httpd_resp_set_hdr(req,"Connection","close");
    httpd_resp_set_hdr(req,"X-Content-Type-Options","nosniff");
    httpd_resp_set_hdr(req,"Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'");
    return httpd_resp_send(req,body,HTTPD_RESP_USE_STRLEN);
}
static bool header_equals(httpd_req_t *req, const char *name, const char *expected, bool optional)
{
    char value[96]; size_t n=httpd_req_get_hdr_value_len(req,name);
    if (!n) return optional;
    return n<sizeof(value) && httpd_req_get_hdr_value_str(req,name,value,sizeof(value))==ESP_OK && !strcmp(value,expected);
}
static esp_err_t get_form(httpd_req_t *req)
{
    if (!header_equals(req,"Host","192.168.4.1",false)) return respond(req,"403 Forbidden","请使用 http://192.168.4.1 打开配网页。");
    char page[1900];
    snprintf(page,sizeof(page),"<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Passport 配网</title><style>body{font:18px system-ui;max-width:28em;margin:2em auto;padding:16px;background:#10212c;color:#e9f1f5}input,button{box-sizing:border-box;width:100%%;padding:12px;margin:8px 0 20px;font:inherit}button{background:#64dec7;border:0}</style><h1>连接 Passport</h1><p>输入 2.4 GHz Wi-Fi。拿到 IP 后才保存；网关地址本阶段只保存，不连接。</p><form method=post action=/configure><input type=hidden name=token value='%s'><label>Wi-Fi 名称<input name=ssid maxlength=32 required autocomplete=off></label><label>Wi-Fi 密码（开放网络留空）<input name=pass type=password maxlength=63 autocomplete=new-password></label><label>网关地址<input name=gateway maxlength=191 placeholder='ws://192.168.1.2:8765' required></label><button>验证并保存</button></form><p>请留意设备屏幕。失败可重新提交，OK 可取消并恢复旧配置。</p></html>",s_token);
    return respond(req,"200 OK",page);
}
static esp_err_t post_form(httpd_req_t *req)
{
    if (!header_equals(req,"Host","192.168.4.1",false) || !header_equals(req,"Origin","http://192.168.4.1",true) || !header_equals(req,"Content-Type","application/x-www-form-urlencoded",false)) return respond(req,"403 Forbidden","请求来源或格式不正确，请重新打开配网页。");
    /* Owner-mirrored acceptability: reject busy states here instead of
       answering 200 and silently dropping the submission later. */
    if (!atomic_load(&s_form_open)) return respond(req,"409 Conflict","设备正忙，请查看设备屏幕后重试。");
    if (req->content_len<=0 || req->content_len>PV_FORM_MAX) return respond(req,"413 Content Too Large","表单过长，请缩短输入。");
    char body[PV_FORM_MAX+1]; size_t used=0;
    while (used<req->content_len) {
        int n=httpd_req_recv(req,body+used,req->content_len-used);
        if (n<=0) return respond(req,"408 Request Timeout","接收未完成，请重新提交。");
        used+=(size_t)n;
    }
    body[used]=0;
    app_event_t event={.kind=EV_FORM};
    pv_validation_t valid=memchr(body,0,used) ? PV_BAD_FORM : pv_parse_form(body,s_token,&event.config);
    memset(body,0,sizeof(body));
    if (valid!=PV_VALID) {
        const char *messages[]={"", "表单字段或长度不正确，请重新打开页面。", "页面已失效，请重新打开。", "Wi-Fi 名称需为 1–32 字节。", "密码为空或 8–63 字节。", "地址需为 ws://主机:端口，可带简单路径。"};
        return respond(req,"400 Bad Request",messages[valid]);
    }
    bool expected=false;
    if (!atomic_compare_exchange_strong(&s_form_pending,&expected,true)) return respond(req,"409 Conflict","正在验证上一次配置，请查看设备屏幕后重试。");
    /* Response goes first. Owner may stop httpd only after this handler returns. */
    esp_err_t err=respond(req,"200 OK","已接收，正在验证连接。请查看设备屏幕；失败后可返回重新提交。成功后临时热点将关闭。");
    if (err==ESP_OK && xQueueSend(s_queue,&event,0)==pdTRUE) return ESP_OK;
    atomic_store(&s_form_pending,false);
    memset(&event.config,0,sizeof(event.config));
    return err==ESP_OK ? ESP_FAIL : err;
}
static esp_err_t http_start(void)
{
    httpd_config_t cfg=HTTPD_DEFAULT_CONFIG();
    cfg.stack_size=6144; cfg.max_uri_handlers=2; cfg.max_open_sockets=3;
    cfg.lru_purge_enable=true; cfg.recv_wait_timeout=3; cfg.send_wait_timeout=3;
    esp_err_t e=httpd_start(&s_http,&cfg);
    if (e!=ESP_OK) return e;
    httpd_uri_t get={.uri="/",.method=HTTP_GET,.handler=get_form};
    httpd_uri_t post={.uri="/configure",.method=HTTP_POST,.handler=post_form};
    e=httpd_register_uri_handler(s_http,&get);
    if (e==ESP_OK) e=httpd_register_uri_handler(s_http,&post);
    if (e!=ESP_OK) { httpd_stop(s_http); s_http=NULL; }
    return e;
}
static void network_event(void *arg, esp_event_base_t base,int32_t id,void *data)
{
    (void)arg;
    app_event_t e={.generation=atomic_load(&s_generation)};
    if (base==WIFI_EVENT && id==WIFI_EVENT_STA_STOP) e.kind=EV_STOP;
    else if (base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED){
        e.kind=EV_DOWN;atomic_store(&s_wifi_loss,e.generation+1);
    }
    else if (base==WIFI_EVENT && id==WIFI_EVENT_STA_CONNECTED) {
        const wifi_event_sta_connected_t *c=data; e.kind=EV_LINK;
        e.ssid_len=c->ssid_len; memcpy(e.ssid,c->ssid,32); memcpy(e.bssid,c->bssid,6);
    } else if (base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) { e.kind=EV_IP;e.ip=((ip_event_got_ip_t *)data)->ip_info; }
    else return;
    (void)xQueueSend(s_queue,&e,0);
}
static esp_err_t network_init(void)
{
    if (s_wifi_ready) return ESP_OK;
    esp_err_t e=esp_netif_init();
    if (e!=ESP_OK) return e;
    e=esp_event_loop_create_default(); if (e!=ESP_OK && e!=ESP_ERR_INVALID_STATE) return e;
    esp_netif_config_t sta_cfg=ESP_NETIF_DEFAULT_WIFI_STA(),ap_cfg=ESP_NETIF_DEFAULT_WIFI_AP();
    s_sta=esp_netif_new(&sta_cfg); s_ap=esp_netif_new(&ap_cfg);
    if (!s_sta || !s_ap) return ESP_ERR_NO_MEM;
    e=esp_netif_attach_wifi_station(s_sta);
    if (e==ESP_OK) e=esp_wifi_set_default_wifi_sta_handlers();
    if (e==ESP_OK) e=esp_netif_attach_wifi_ap(s_ap);
    if (e==ESP_OK) e=esp_wifi_set_default_wifi_ap_handlers();
    if (e!=ESP_OK) return e;
    wifi_init_config_t cfg=WIFI_INIT_CONFIG_DEFAULT();cfg.nvs_enable=false;
    e=esp_wifi_init(&cfg);
    if (e==ESP_OK) e=esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (e==ESP_OK) e=esp_event_handler_instance_register(WIFI_EVENT,ESP_EVENT_ANY_ID,network_event,NULL,&s_wifi_handler);
    if (e==ESP_OK) e=esp_event_handler_instance_register(IP_EVENT,IP_EVENT_STA_GOT_IP,network_event,NULL,&s_ip_handler);
    if (e==ESP_OK) s_wifi_ready=true;
    return e;
}
static esp_err_t ap_start(void)
{
    if (s_setup) return ESP_OK;
    uint32_t id=esp_random(); snprintf(s_ap_name,sizeof(s_ap_name),"Passport-%06lx",(unsigned long)(id & 0xffffff));
    random_hex(s_ap_password,8); random_hex(s_token,16);
    wifi_config_t cfg={0};
    memcpy(cfg.ap.ssid,s_ap_name,strlen(s_ap_name));cfg.ap.ssid_len=strlen(s_ap_name);
    memcpy(cfg.ap.password,s_ap_password,strlen(s_ap_password));
    cfg.ap.channel=1;cfg.ap.max_connection=1;cfg.ap.authmode=WIFI_AUTH_WPA2_PSK;
    esp_err_t e=esp_wifi_set_mode(s_sta_enabled ? WIFI_MODE_APSTA : WIFI_MODE_AP);
    if (e==ESP_OK) e=esp_wifi_set_config(WIFI_IF_AP,&cfg);
    if (e==ESP_OK && !s_wifi_started) { e=esp_wifi_start();if(e==ESP_OK)s_wifi_started=true; }
    if (e==ESP_OK) e=http_start();
    if (e==ESP_OK) s_setup=true;
    return e;
}
static esp_err_t setup_close(void)
{
    /* Called only by owner, never by a URI/event/button callback. */
    if (s_http) { esp_err_t e=httpd_stop(s_http);if(e!=ESP_OK)return e;s_http=NULL; }
    esp_err_t e=esp_wifi_set_mode(WIFI_MODE_STA);
    if(e!=ESP_OK)return e;
    s_setup=false;s_close_pending=false;
    memset(s_ap_password,0,sizeof(s_ap_password));memset(s_token,0,sizeof(s_token));
    atomic_store(&s_form_pending,false);return ESP_OK;
}
static esp_err_t load_saved(void)
{
    esp_err_t e=nvs_flash_init();
    if(e!=ESP_OK)return e; /* Never erase an unknown/old NVS partition. */
    s_nvs_ready=true;
    nvs_handle_t h; e=nvs_open("passport",NVS_READONLY,&h);
    if(e==ESP_ERR_NVS_NOT_FOUND)return ESP_OK; /* namespace absent: fresh unit */
    if(e!=ESP_OK)return e;
    uint8_t volume = 50;
    if(nvs_get_u8(h,"volume",&volume)==ESP_OK && volume<=100)s_volume=volume;
    size_t n=sizeof(s_saved);
    e=nvs_get_blob(h,"config_v1",&s_saved,&n);
    nvs_close(h);
    if(e==ESP_ERR_NVS_NOT_FOUND)return ESP_OK; /* nothing stored yet */
    if(e!=ESP_OK)return e;                     /* real NVS read failure */
    /* A blob with another size/version or invalid content is stale data, not
       a storage failure: fall back to first-time setup and keep NVS intact. */
    if(n!=sizeof(s_saved) || !pv_config_valid(&s_saved)){
        ESP_LOGW(TAG,"stored config rejected (len=%u ver=%u); entering setup",
                 (unsigned)n,(unsigned)s_saved.version);
        memset(&s_saved,0,sizeof(s_saved));
        return ESP_OK;
    }
    s_model.has_saved=true;return ESP_OK;
}
static esp_err_t save_active(void)
{
    if(!s_nvs_ready)return ESP_ERR_INVALID_STATE;
    nvs_handle_t h; esp_err_t e=nvs_open("passport",NVS_READWRITE,&h);
    if(e!=ESP_OK)return e;
    e=nvs_set_blob(h,"config_v1",&s_active,sizeof(s_active));
    if(e==ESP_OK)e=nvs_commit(h);
    nvs_close(h);
    if(e==ESP_OK)s_saved=s_active;
    return e;
}
static void failed(const char *status)
{
    pv_failed(&s_model);s_deadline=0;atomic_store(&s_form_pending,false);
    if(s_model.has_saved && !s_setup){
        s_wifi_retry_at=esp_timer_get_time()+(int64_t)pv_ws_backoff_ms(s_wifi_retries)*1000;
        if(s_wifi_retries<5)++s_wifi_retries;
    }
    show(status,s_setup ? "Resubmit in browser\nOK: cancel" : "OK: retry\nHold UP: setup");
}
static void begin_attempt(void)
{
    s_attempt_pending=false;s_barrier=false;
    pv_begin(&s_model,s_model.has_saved,s_new_candidate);
    atomic_store(&s_generation,s_model.generation);
    wifi_config_t cfg={0};
    memcpy(cfg.sta.ssid,s_active.ssid,strlen(s_active.ssid));
    memcpy(cfg.sta.password,s_active.password,strlen(s_active.password));
    esp_err_t e=esp_wifi_set_mode(s_setup ? WIFI_MODE_APSTA : WIFI_MODE_STA);
    if(e==ESP_OK)s_sta_enabled=true;
    if(e==ESP_OK)e=esp_wifi_set_config(WIFI_IF_STA,&cfg);
    if(e==ESP_OK && !s_wifi_started) { e=esp_wifi_start();if(e==ESP_OK)s_wifi_started=true; }
    if(e==ESP_OK)e=esp_wifi_connect();
    memset(&cfg,0,sizeof(cfg));
    if(e!=ESP_OK){failed("Connect failed");return;}
    s_deadline=esp_timer_get_time()+CONNECT_US;
    show("Connecting...",s_setup ? "Checking Wi-Fi\nOK: cancel" : "Checking Wi-Fi");
}
static void request_attempt(const pv_config_t *c,bool candidate)
{
    s_active=*c;s_new_candidate=candidate;s_close_pending=false;
    s_attempt_pending=true;
    s_model.state=PV_CONNECTING;s_model.linked=false;
    /* AP mode stops only STA, retaining HTTP/AP. STA_STOP is an event-loop
       barrier: all prior station events are consumed before a new generation.
       Outside setup, stop the whole stack instead: switching to AP mode here
       would rebroadcast the stale AP SSID/password during the retry window. */
    if(s_sta_enabled){
        s_barrier=true;s_deadline=esp_timer_get_time()+CONNECT_US;
        esp_err_t e;
        if(s_setup){
            e=esp_wifi_set_mode(WIFI_MODE_AP);
        }else{
            e=esp_wifi_stop();
            if(e==ESP_OK)s_wifi_started=false;
        }
        if(e!=ESP_OK){s_barrier=false;s_attempt_pending=false;failed("Wi-Fi stop failed");}
        else s_sta_enabled=false;
    } else begin_attempt();
}
/* The owner creates/destroys the client while the audio send task may be
   inside send_bin; one mutex serializes both. */
static int ws_send_text_locked(const char *data, size_t len)
{
    if (!s_ws_send_lock || xSemaphoreTake(s_ws_send_lock, pdMS_TO_TICKS(1000)) != pdTRUE)
        return -1;
    int sent = s_ws_client
        ? esp_websocket_client_send_text(s_ws_client, data, (int)len,
                                         pdMS_TO_TICKS(500))
        : -1;
    xSemaphoreGive(s_ws_send_lock);
    return pv_audio_send_ok(sent, len) ? sent : -1;
}

static int ws_send_bin_locked(const void *data, size_t len)
{
    if (!s_ws_send_lock || xSemaphoreTake(s_ws_send_lock, pdMS_TO_TICKS(1000)) != pdTRUE)
        return -1;
    int sent = s_ws_client
        ? esp_websocket_client_send_bin(s_ws_client, (const char *)data, (int)len,
                                        pdMS_TO_TICKS(500))
        : -1;
    xSemaphoreGive(s_ws_send_lock);
    return pv_audio_send_ok(sent, len) ? sent : -1;
}

static void ws_destroy(void)
{
    if (s_ws_send_lock) xSemaphoreTake(s_ws_send_lock, portMAX_DELAY);
    if (s_ws_client) {
        esp_websocket_client_stop(s_ws_client);
        esp_websocket_client_destroy(s_ws_client);
        s_ws_client = NULL;
    }
    if (s_ws_send_lock) xSemaphoreGive(s_ws_send_lock);
    /* Invalidate events already queued by the destroyed client. */
    atomic_fetch_add(&s_ws_client_generation, 1);
    atomic_store(&s_voice_notice,0);atomic_store(&s_voice_available,false);s_backend=0;s_backend_at=0;
    s_ws_msg_opcode = 0;
}

/* Logged once after a real parse so the watermark reflects Stage D work. */
static void ws_log_stack_watermark(void)
{
    static bool logged;
    if (logged) return;
    logged = true;
    ESP_LOGI(TAG, "WS task stack high-water=%u",
             (unsigned)uxTaskGetStackHighWaterMark(NULL));
}

static void ws_binary_chunk(const esp_websocket_event_data_t *d)
{
    if (!d || d->data_len <= 0 || !atomic_load(&s_down_audio_ok)) return;
    if (!s_down_queue || !s_down_acc_lock) return;
    if (xSemaphoreTake(s_down_acc_lock, 0) != pdTRUE) return;
    const uint8_t *bytes = (const uint8_t *)d->data_ptr;
    size_t len = (size_t)d->data_len, off = 0;
    while (off < len) {
        size_t used = 0;
        down_frame_t frame = {.len = PV_DOWN_FRAME_BYTES};
        if (pv_down_rx_append(&s_down_rx, bytes + off, len - off, &used,
                              frame.data)) {
            bool accepted = xQueueSend(s_down_queue, &frame, 0) == pdTRUE;
            if (pv_audio_queue_frame(&s_down_stats, accepted)) {
                down_frame_t oldest;
                if (xQueueReceive(s_down_queue, &oldest, 0) == pdTRUE)
                    (void)xQueueSend(s_down_queue, &frame, 0);
                unsigned dropped = s_down_stats.dropped;
                atomic_store(&s_down_dropped, dropped);
                if (dropped != s_down_drop_logged &&
                    (dropped == 1 || dropped % 50 == 0)) {
                    s_down_drop_logged = dropped;
                    ESP_LOGW(TAG, "downlink queue full: dropped=%u", dropped);
                }
            }
        }
        if (used == 0) break;
        off += used;
    }
    xSemaphoreGive(s_down_acc_lock);
}

/* Runs in the WebSocket client task: reassemble one bounded message, parse it
   immediately, and enqueue a self-contained result. The owner never reads the
   shared slot, so back-to-back tts.start/sentence_start cannot overwrite it. */
static void ws_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)base;
    /* The client generation is registered with the handler; callbacks from a
       torn-down client are rejected before they can touch current state. */
    unsigned client_generation = (unsigned)(uintptr_t)arg;
    if (client_generation != atomic_load(&s_ws_client_generation)) return;
    static const pv_ws_event_ids_t ids = {
        .connected = WEBSOCKET_EVENT_CONNECTED,
        .disconnected = WEBSOCKET_EVENT_DISCONNECTED,
        .closed = WEBSOCKET_EVENT_CLOSED, /* 1.8.0 clean peer close */
        .finish = WEBSOCKET_EVENT_FINISH,
        .error = WEBSOCKET_EVENT_ERROR,
        .data = WEBSOCKET_EVENT_DATA,
    };
    app_event_t e = {.kind = EV_WS};
    switch (pv_ws_classify_event(id, &ids)) {
    case PV_WS_KIND_OPEN:
        e.ws = WS_OPEN;
        break;
    case PV_WS_KIND_CLOSED:
        e.ws = WS_CLOSED;
        atomic_store(&s_pending_ws_generation, client_generation);
        pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_WS_LOST);
        break;
    case PV_WS_KIND_ERROR:
        e.ws = WS_ERROR;
        atomic_store(&s_pending_ws_generation, client_generation);
        pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_WS_LOST);
        break;
    case PV_WS_KIND_DATA: {
        const esp_websocket_event_data_t *d = data;
        if (!d || d->data_len <= 0) return;
        if (d->op_code == 0x1 || d->op_code == 0x2)
            s_ws_msg_opcode = d->op_code;
        bool binary = s_ws_msg_opcode == 0x2; /* continuation 0x0 inherits */
        if (binary) {
            ws_binary_chunk(d);
            ws_log_stack_watermark();
            return;
        }
        if (d->op_code != 0x1 && d->op_code != 0x0) return;
        if (d->payload_offset < 0 || d->payload_len < 0 ||
            d->payload_len > (int)PV_WS_MAX_MESSAGE) return;
        size_t total = (size_t)d->payload_offset + (size_t)d->data_len;
        if (total > PV_WS_MAX_MESSAGE) return;
        memcpy(s_ws_rx + d->payload_offset, d->data_ptr, (size_t)d->data_len);
        if (total < (size_t)d->payload_len) return; /* wait for the rest */
        pv_server_hello_t hello;
        pv_text_kind_t kind = pv_ws_classify_text(s_ws_rx, total, &hello);
        ws_log_stack_watermark();
        if (kind == PV_TEXT_IGNORE) return;
        if(kind==PV_TEXT_VOICE_READY)atomic_store(&s_voice_available,true);
        if(kind==PV_TEXT_VOICE_ERROR)atomic_store(&s_voice_available,false);
        if(kind==PV_TEXT_TTS_START && !atomic_load(&s_voice_available))return;
        if (kind == PV_TEXT_TTS_START || kind == PV_TEXT_TTS_STOP) {
            bool start = kind == PV_TEXT_TTS_START;
            pv_audio_note_pending(&s_pending, start ? PV_AUDIO_PENDING_TTS_START
                                                    : PV_AUDIO_PENDING_TTS_STOP);
            if (s_down_acc_lock &&
                xSemaphoreTake(s_down_acc_lock, 0) == pdTRUE) {
                if (start) pv_down_rx_arm(&s_down_rx);
                else pv_down_rx_disarm(&s_down_rx);
                xSemaphoreGive(s_down_acc_lock);
            }
        }
        if(kind==PV_TEXT_VOICE_READY || kind==PV_TEXT_VOICE_ERROR || kind==PV_TEXT_INPUT_SHORT)
            atomic_store(&s_voice_notice,kind);
        e.kind = EV_TEXT;
        e.ws_client_generation=client_generation;
        e.text = kind;
        if (kind == PV_TEXT_HELLO) e.hello = hello;
        break;
    }
    default:
        return;
    }
    e.ws_client_generation = client_generation;
    (void)xQueueSend(s_queue, &e, 0);
}

static esp_err_t ws_start(void)
{
    pv_ws_endpoint_t endpoint;
    if (!pv_ws_parse_endpoint(s_saved.gateway, &endpoint)) {
        ESP_LOGE(TAG, "gateway URL unsupported");
        return ESP_ERR_INVALID_ARG;
    }
    int n = snprintf(s_ws_uri, sizeof(s_ws_uri), "ws://%s:%u%s",
                     endpoint.host, (unsigned)endpoint.port, endpoint.path);
    if (n <= 0 || (size_t)n >= sizeof(s_ws_uri)) {
        ESP_LOGE(TAG, "gateway URI too long");
        return ESP_ERR_INVALID_SIZE;
    }
    if (s_ws_send_lock && xSemaphoreTake(s_ws_send_lock, pdMS_TO_TICKS(1000)) != pdTRUE)
        return ESP_ERR_TIMEOUT;
    esp_websocket_client_config_t cfg = {0};
    cfg.uri = s_ws_uri;
    cfg.disable_auto_reconnect = true; /* the owner drives the backoff */
    cfg.network_timeout_ms = 10000;
    cfg.buffer_size = 1024;
    cfg.task_stack = 6144; /* Stage D parses bounded JSON in the callback */
    s_ws_client = esp_websocket_client_init(&cfg);
    if (!s_ws_client) {
        ESP_LOGE(TAG, "websocket init failed");
        if (s_ws_send_lock) xSemaphoreGive(s_ws_send_lock);
        return ESP_ERR_NO_MEM;
    }
    /* Events queued by this client carry this generation; older clients'
       late events are discarded even if their heap address is reused. */
    unsigned generation = atomic_fetch_add(&s_ws_client_generation, 1) + 1;
    esp_err_t e = esp_websocket_register_events(
        s_ws_client, WEBSOCKET_EVENT_ANY, ws_event,
        (void *)(uintptr_t)generation);
    if (e == ESP_OK) e = esp_websocket_client_start(s_ws_client);
    if (s_ws_send_lock) xSemaphoreGive(s_ws_send_lock);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "websocket start failed: %s", esp_err_to_name(e));
        ws_destroy(); /* takes the lock itself */
    }
    return e;
}

static void ws_wifi_online(void)
{
    if (!s_saved.gateway[0]) return;
    if (!pv_ws_event(&s_ws, PV_EV_WIFI_ONLINE, 0, esp_timer_get_time())) return;
    audio_ws_lost();
    ws_destroy();
    if (ws_start() != ESP_OK) {
        pv_ws_event(&s_ws, PV_EV_TRANSPORT_ERROR, s_ws.generation,
                    esp_timer_get_time());
    }
    audio_ws_sync();
    show_online();
}

static void ws_restart(void)
{
    audio_ws_lost();
    ws_destroy();
    pv_ws_init(&s_ws);
    ws_wifi_online();
}

static void ws_handle(const app_event_t *e)
{
    if (e->ws_client_generation != atomic_load(&s_ws_client_generation)) return;
    int64_t now = esp_timer_get_time();
    if (e->ws == WS_OPEN) {
        pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_WS_LOST);
        if (!pv_ws_event(&s_ws, PV_EV_TRANSPORT_OPEN, s_ws.generation, now)) return;
        char hello[192];
        size_t n = pv_ws_build_hello(hello, sizeof(hello));
        int sent = n ? ws_send_text_locked(hello, n) : -1;
        if (sent < 0) {
            pv_ws_event(&s_ws, PV_EV_TRANSPORT_ERROR, s_ws.generation, now);
            audio_ws_sync();
            ws_destroy();
        } else {
            audio_ws_sync();
        }
        show_online();
        return;
    }
    pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_WS_LOST);
    pv_ws_event_t event = e->ws == WS_CLOSED ? PV_EV_TRANSPORT_CLOSED
                                             : PV_EV_TRANSPORT_ERROR;
    if (pv_ws_event(&s_ws, event, s_ws.generation, now)) {
        audio_ws_sync();
        ws_destroy();
    } else {
        audio_ws_sync();
    }
    show_online();
}

static void audio_runtime_reset(void)
{
    atomic_store(&s_capture_run, false);
    atomic_store(&s_stop_pending, false);
    atomic_store(&s_stop_sent, true);
    pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_STOP_SENT);
    if (s_pcm_queue) xQueueReset(s_pcm_queue);
}

static void audio_ws_lost(void)
{
    if (pv_audio_event(&s_audio, PV_AUDIO_WS_LOST) == PV_AUDIO_ACT_RESET)
        audio_runtime_reset();
    atomic_store(&s_down_audio_ok, false);
    audio_downlink_reset(false);
}

static void audio_ws_sync(void)
{
    if (s_ws.state == PV_WS_READY) {
        (void)pv_audio_event(&s_audio, PV_AUDIO_WS_READY);
    } else {
        audio_ws_lost();
    }
}

static bool audio_send_listen(const char *state)
{
    char message[160];
    int n = snprintf(message, sizeof(message),
                     "{\"type\":\"listen\",\"state\":\"%s\",\"mode\":\"manual\","
                     "\"session_id\":\"%s\"}", state, s_ws.session_id);
    if (n <= 0 || (size_t)n >= sizeof(message)) return false;
    return ws_send_text_locked(message, (size_t)n) >= 0;
}

/* Capture/send tasks report failures through the owner queue only. */
static void audio_task_fail(void)
{
    atomic_store(&s_capture_run, false);
    atomic_store(&s_stop_pending, false);
    app_event_t e = {.kind = EV_AUDIO, .audio = AUDIO_SEND_FAILED};
    (void)xQueueSend(s_queue, &e, 0);
}

static void capture_task(void *arg)
{
    (void)arg;
    for (;;) {
        if (!atomic_load(&s_capture_run)) {
            atomic_store(&s_capture_idle, true);
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        atomic_store(&s_capture_idle, false);
        pcm_frame_t frame = {.len = PV_AUDIO_FRAME_BYTES};
        esp_err_t re = ESP_ERR_INVALID_STATE;
        if (xSemaphoreTake(s_audio_io_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            re = bsp_audio_read(frame.data, frame.len);
            xSemaphoreGive(s_audio_io_lock);
        }
        if (re != ESP_OK) {
            audio_task_fail();
            continue;
        }
        if (!atomic_load(&s_capture_run)) continue; /* release discards in-flight */
        bool accepted = xQueueSend(s_pcm_queue, &frame, 0) == pdTRUE;
        if (pv_audio_queue_frame(&s_capture_stats, accepted)) {
            pcm_frame_t oldest;
            if (xQueueReceive(s_pcm_queue, &oldest, 0) == pdTRUE)
                (void)xQueueSend(s_pcm_queue, &frame, 0);
            atomic_store(&s_audio_dropped, s_capture_stats.dropped);
        }
    }
}

static void send_task(void *arg)
{
    (void)arg;
    for (;;) {
        pcm_frame_t frame;
        if (xQueueReceive(s_pcm_queue, &frame, pdMS_TO_TICKS(20)) == pdTRUE) {
            if (atomic_load(&s_down_format_24k)) continue; /* half-duplex */
            if (ws_send_bin_locked(frame.data, frame.len) < 0) {
                xQueueReset(s_pcm_queue);
                audio_task_fail();
                continue;
            }
        }
        if (atomic_load(&s_stop_pending) && atomic_load(&s_capture_idle) &&
            uxQueueMessagesWaiting(s_pcm_queue) == 0) {
            atomic_store(&s_stop_pending, false);
            pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_STOP_SENT);
            app_event_t done = {.kind = EV_AUDIO, .audio = AUDIO_STOP_SENT};
            (void)xQueueSend(s_queue, &done, 0);
        }
    }
}

static bool audio_event_post(audio_kind_t kind)
{
    app_event_t e = {.kind = EV_AUDIO, .audio = kind};
    return xQueueSend(s_queue, &e, 0) == pdTRUE;
}

/* All codec format switches and PCM reads/writes share this mutex: the BSP
   requires reads/writes to be stopped before set_format reopens the codec. */
static bool audio_set_format_locked(uint32_t hz)
{
    if (!s_audio_io_lock) return false;
    if (xSemaphoreTake(s_audio_io_lock, pdMS_TO_TICKS(1000)) != pdTRUE)
        return false;
    esp_err_t e = bsp_audio_set_format(hz, 16, 1);
    xSemaphoreGive(s_audio_io_lock);
    return e == ESP_OK;
}

static void audio_downlink_drop_queue(void)
{
    if (s_down_queue) xQueueReset(s_down_queue);
    atomic_store(&s_down_stop_pending, false);
    pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_PLAY_DONE);
    pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_PLAY_FAILED);
}

static void audio_downlink_clear(void)
{
    audio_downlink_drop_queue();
    /* Disarm and discard any partial frame; only a fresh tts.start re-arms. */
    if (s_down_acc_lock && xSemaphoreTake(s_down_acc_lock, portMAX_DELAY) == pdTRUE) {
        pv_down_rx_disarm(&s_down_rx);
        xSemaphoreGive(s_down_acc_lock);
    }
}

static void play_task(void *arg)
{
    (void)arg;
    int64_t last_write = 0, quiet_since = 0;
    bool write_failed = false, done_latched = false;
    for (;;) {
        down_frame_t frame;
        bool writing = atomic_load(&s_down_format_24k);
        if (writing && !write_failed && s_down_queue &&
            xQueuePeek(s_down_queue, &frame, pdMS_TO_TICKS(20)) == pdTRUE) {
            esp_err_t e = ESP_ERR_INVALID_STATE;
            if (xSemaphoreTake(s_audio_io_lock, pdMS_TO_TICKS(200)) == pdTRUE) {
                e = bsp_audio_write(frame.data, frame.len);
                xSemaphoreGive(s_audio_io_lock);
            }
            if (e == ESP_OK) {
                (void)xQueueReceive(s_down_queue, &frame, 0);
                last_write = esp_timer_get_time();
                /* Peak over the 480 PCM16 samples of one 20 ms frame. */
                uint16_t peak = 0;
                for (size_t i = 0; i + 1 < frame.len; i += 2) {
                    int16_t sample = (int16_t)((uint16_t)frame.data[i] |
                                               (uint16_t)frame.data[i + 1] << 8);
                    uint16_t mag = sample < 0 ? (uint16_t)(-sample) : (uint16_t)sample;
                    if (mag > peak) peak = mag;
                }
                uint32_t level = ((uint32_t)peak * 100u) / 32768u;
                atomic_store(&s_mouth_level, (unsigned char)(level > 100u ? 100u : level));
            } else if (!write_failed) {
                write_failed = true;
                pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_PLAY_FAILED);
                (void)audio_event_post(AUDIO_PLAY_FAILED);
            }
        } else if (!writing || write_failed) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        int64_t now = esp_timer_get_time();
        if (atomic_load(&s_down_format_24k) && last_write == 0)
            last_write = now;
        if (!atomic_load(&s_down_format_24k)) {
            write_failed = false;
            done_latched = false;
            last_write = 0;
            quiet_since = 0;
            continue;
        }
        if (atomic_load(&s_down_stop_pending)) {
            if (uxQueueMessagesWaiting(s_down_queue) == 0) {
                if (!quiet_since) quiet_since = now;
                else if (now - quiet_since >= (int64_t)PV_DOWN_DRAIN_GRACE_MS * 1000 &&
                         !done_latched) {
                    done_latched = true;
                    pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_PLAY_DONE);
                    (void)audio_event_post(AUDIO_PLAY_DONE);
                }
            } else {
                quiet_since = 0;
            }
        } else if (last_write &&
                   now - last_write >= (int64_t)PV_DOWN_IDLE_TIMEOUT_MS * 1000 &&
                   !done_latched) {
            done_latched = true;
            pv_audio_note_pending(&s_pending, PV_AUDIO_PENDING_PLAY_DONE);
            (void)audio_event_post(AUDIO_PLAY_DONE);
        }
    }
}

/* Lazy on the first hold: keeps the 11.25 KB I2S DMA and codec out of the
   Wi-Fi/WebSocket bring-up. Runs in the owner task, serialized with history. */
static bool audio_ensure_ready(void)
{
    if (s_audio_initialized) return true;
    if (!s_bsp_audio_inited) {
        if (bsp_audio_init() != ESP_OK) return false;
        /* BSP starts muted; remember a moderate level across codec reopens. */
        bsp_audio_set_volume(s_volume);
        s_bsp_audio_inited = true;
    }
    if (!audio_set_format_locked(16000)) return false;
    if (!s_pcm_queue)
        s_pcm_queue = xQueueCreate(PV_AUDIO_QUEUE_DEPTH, sizeof(pcm_frame_t));
    if (!s_pcm_queue) return false;
    if (!s_capture_task &&
        xTaskCreate(capture_task, "pv_capture", 3072, NULL, 6, &s_capture_task) != pdPASS)
        return false;
    if (!s_send_task &&
        xTaskCreate(send_task, "pv_send", 3072, NULL, 6, &s_send_task) != pdPASS)
        return false;
    if (!s_play_task &&
        xTaskCreate(play_task, "pv_play", 3072, NULL, 6, &s_play_task) != pdPASS)
        return false;
    s_audio_initialized = true;
    ESP_LOGI(TAG, "audio ready: 16 kHz/16-bit/mono, 640 B per 20 ms");
    return true;
}

/* Downlink owns the device: return the uplink model to a clean Idle. */
static void audio_uplink_idle(void)
{
    pv_audio_init(&s_audio);
    if (s_ws.state == PV_WS_READY)
        (void)pv_audio_event(&s_audio, PV_AUDIO_WS_READY);
}

/* Owner-side downlink transitions. The playback task only moves bytes. */
static void audio_downlink_reset(bool error)
{
    s_wait_reply=false;s_thinking_at=0;
    (void)pv_down_event(&s_down, error ? PV_DOWN_EV_ERROR : PV_DOWN_EV_LOST);
    audio_downlink_clear();
    if (atomic_exchange(&s_down_format_24k, false))
        (void)audio_set_format_locked(16000);
    if (error) s_down_error = true; /* sticky until a valid hello */
}

static void audio_downlink_finish(bool error)
{
    pv_down_action_t action = pv_down_event(
        &s_down, error ? PV_DOWN_EV_ERROR : PV_DOWN_EV_DRAINED);
    audio_downlink_clear();
    if (atomic_exchange(&s_down_format_24k, false) && !audio_set_format_locked(16000))
        error = true;
    audio_uplink_idle();
    s_down_error = error;
    (void)action; /* STOP_AUDIO or RESET: the format switch above is the work */
    show_online();
}

static void audio_downlink_start(void)
{
    s_wait_reply=false;s_thinking_at=0;
    if (s_ws.state != PV_WS_READY || s_backend!=1 || !atomic_load(&s_down_audio_ok)) return;
    if (!audio_ensure_ready()) {
        audio_downlink_clear();
        s_down_error = true;
        show_online();
        return;
    }
    /* Half-duplex: end the uplink and discard anything not yet sent. */
    audio_runtime_reset();
    audio_uplink_idle();
    /* tts.start precedes its binary frames, but the owner may process it
       after the WS callback queued them. Only a previous draining segment
       is discarded; a fresh segment keeps its already-arrived frames. */
    if (s_down.state == PV_DOWN_DRAINING) audio_downlink_drop_queue();
    else atomic_store(&s_down_stop_pending, false);
    /* The WS callback already armed this segment; this is idempotent. */
    if (s_down_acc_lock && xSemaphoreTake(s_down_acc_lock, portMAX_DELAY) == pdTRUE) {
        pv_down_rx_arm(&s_down_rx);
        xSemaphoreGive(s_down_acc_lock);
    }
    /* The gateway may still consider its manual listening window open. */
    if (s_ws.state == PV_WS_READY) (void)audio_send_listen("stop");
    if (pv_down_event(&s_down, PV_DOWN_EV_START) != PV_DOWN_ACT_START_AUDIO)
        return;
    if (!audio_set_format_locked(PV_DOWN_SAMPLE_RATE)) {
        (void)pv_down_event(&s_down, PV_DOWN_EV_ERROR);
        audio_downlink_clear();
        audio_uplink_idle();
        s_down_error = true;
        show_online();
        return;
    }
    atomic_store(&s_down_format_24k, true);
    atomic_store(&s_down_stop_pending, false);
    s_down_error = false;
    show_online();
}

static void audio_downlink_stop(void)
{
    if (s_down.state != PV_DOWN_SPEAKING) return;
    (void)pv_down_event(&s_down, PV_DOWN_EV_STOP);
    atomic_store(&s_down_stop_pending, true);
    show_online();
}

/* listen.stop is sent exactly once per utterance, from the owner task. */
static void audio_stop_complete(void)
{
    if (s_audio.state != PV_AUDIO_THINKING) return;
    if (s_ws.state != PV_WS_READY || !audio_send_listen("stop")) {
        pv_audio_event(&s_audio, PV_AUDIO_SEND_FAILED);
        audio_runtime_reset();
    } else {
        pv_audio_event(&s_audio, PV_AUDIO_STOPPED); /* Thinking -> Idle */
        atomic_store(&s_stop_sent, true);
    }
    show_online();
}

static void audio_press(void)
{
    /* Half-duplex: presses are ignored while speaking/draining. */
    if (!pv_down_can_listen(&s_down) || s_wait_reply || s_backend!=1) return;
    /* The previous utterance is still draining or its stop is not yet out. */
    if (!atomic_load(&s_stop_sent)) return;
    if (s_setup || s_ws.state != PV_WS_READY) return;
    /* A failed 24k->16k restore must not leave capture at the wrong rate. */
    if (s_audio_initialized && !audio_set_format_locked(16000)) {
        s_down_error = true;
        show_online();
        return;
    }
    atomic_store(&s_down_format_24k, false);
    s_down_error = false; /* a new press retries after a playback error */
    if (pv_audio_event(&s_audio, PV_AUDIO_PRESS) != PV_AUDIO_ACT_START) return;
    if (!audio_ensure_ready() || !audio_send_listen("start")) {
        pv_audio_event(&s_audio, PV_AUDIO_SEND_FAILED);
        audio_runtime_reset();
        show_online();
        return;
    }
    atomic_store(&s_capture_idle, false);
    atomic_store(&s_capture_run, true);
    show_online();
}

static void audio_release(void)
{
    if (pv_audio_event(&s_audio, PV_AUDIO_RELEASE) != PV_AUDIO_ACT_STOP) return;
    /* Stop capture first; the send task drains the bounded queue and only
       then emits listen.stop (capture_idle is set after the last read). */
    s_wait_reply=true;s_thinking_at=esp_timer_get_time();
    atomic_store(&s_capture_run, false);
    atomic_store(&s_stop_pending, true);
    atomic_store(&s_stop_sent, false);
    show_online();
}

static void save_and_finish(void)
{
    esp_err_t e=save_active();pv_saved(&s_model,e==ESP_OK);
    if(e!=ESP_OK){atomic_store(&s_form_pending,false);show("Save failed","UP: retry save\nOK: cancel");return;}
    /* Grace period also lets phone TCP delivery finish before AP teardown. */
    s_close_pending=s_setup;s_close_at=esp_timer_get_time()+1500000;
    ws_restart();
    show_online();
}
static void process(const app_event_t *e)
{
    if(e->kind==EV_WS){ws_handle(e);return;}
    if(e->kind==EV_TEXT){
        if(e->ws_client_generation!=atomic_load(&s_ws_client_generation))return;
        int64_t now=esp_timer_get_time();
        bool handshaking=s_ws.state==PV_WS_CONNECTING || s_ws.state==PV_WS_HANDSHAKING;
        if(e->text==PV_TEXT_VOICE_READY || e->text==PV_TEXT_VOICE_ERROR || e->text==PV_TEXT_INPUT_SHORT){
            int expected=e->text;
            if(!atomic_compare_exchange_strong(&s_voice_notice,&expected,0))return;
        }
        if(e->text==PV_TEXT_VOICE_READY){s_backend=1;s_backend_at=0;show_online();return;}
        if(e->text==PV_TEXT_VOICE_ERROR){
            audio_runtime_reset();audio_downlink_reset(true);audio_uplink_idle();
            s_down_error=false;s_backend=2;s_thinking_at=0;show_online();return;
        }
        if(e->text==PV_TEXT_INPUT_SHORT){
            audio_uplink_idle();s_wait_reply=false;s_thinking_at=0;show("Hold OK to talk","Press was too short");return;
        }
        if(e->text==PV_TEXT_HELLO){
            /* A duplicate hello on a READY connection must not re-handshake. */
            if(!handshaking)return;
            s_backend=0;s_backend_at=now;
            if(!pv_server_audio_valid(&e->hello)){
                atomic_store(&s_down_audio_ok,false);
                if(pv_ws_event(&s_ws,PV_EV_HELLO_INVALID,s_ws.generation,now)){
                    audio_ws_sync();ws_destroy();
                }
                s_down_error=true;show_online();return;
            }
            if(pv_ws_hello(&s_ws,s_ws.generation,now,&e->hello)){
                s_down_error=false;
                atomic_store(&s_down_audio_ok,true);
                audio_ws_sync();
            } else if(s_ws.state==PV_WS_HANDSHAKING &&
                      pv_ws_event(&s_ws,PV_EV_HELLO_INVALID,s_ws.generation,now)){
                audio_ws_sync();ws_destroy();
            }
            show_online();return;
        }
        if(e->text==PV_TEXT_HELLO_INVALID){
            /* Non-hello traffic is never fatal; malformed hello only matters
               before the connection has been accepted. */
            if(handshaking && pv_ws_event(&s_ws,PV_EV_HELLO_INVALID,s_ws.generation,now)){
                audio_ws_sync();ws_destroy();show_online();
            }
            return;
        }
        if(e->text==PV_TEXT_TTS_START){
            if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_TTS_START))
                audio_downlink_start();
        } else if(e->text==PV_TEXT_TTS_STOP){
            if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_TTS_STOP))
                audio_downlink_stop();
        }
        return;
    }
    if(e->kind==EV_AUDIO){
        if(e->audio==AUDIO_SEND_FAILED){
            pv_audio_event(&s_audio,PV_AUDIO_SEND_FAILED);
            audio_runtime_reset();
            show_online();
        } else if(e->audio==AUDIO_STOP_SENT){
            if(pv_audio_take_pending(&s_pending, PV_AUDIO_PENDING_STOP_SENT))
                audio_stop_complete();
        } else if(e->audio==AUDIO_PLAY_DONE){
            if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_PLAY_DONE))
                audio_downlink_finish(false);
        } else if(e->audio==AUDIO_PLAY_FAILED){
            if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_PLAY_FAILED))
                audio_downlink_finish(true);
        }
        return;
    }
    if(e->kind==EV_STOP){
        if(s_barrier && s_attempt_pending)begin_attempt();
        return;
    }
    if(e->kind==EV_KEY){
        s_last_input=esp_timer_get_time();bsp_display_backlight(70);
        if(e->key==BSP_BTN_UP && e->key_event==BSP_BTN_PRESS)s_up_long=false;
        if(e->key==BSP_BTN_UP && e->key_event==BSP_BTN_LONG)s_up_long=true;
        if((e->key==BSP_BTN_UP || e->key==BSP_BTN_DOWN) && e->key_event==BSP_BTN_CLICK){
            if(e->key==BSP_BTN_UP && s_up_long)return;
            s_volume=pv_volume_step(s_volume,e->key==BSP_BTN_UP ? 10 : -10);
            s_volume_dirty=true;s_volume_changed=esp_timer_get_time()/1000;
            if(s_bsp_audio_inited && xSemaphoreTake(s_audio_io_lock,pdMS_TO_TICKS(100))==pdTRUE){
                bsp_audio_set_volume(s_volume);xSemaphoreGive(s_audio_io_lock);
            }
            show_online();return;
        }
        if(e->key==BSP_BTN_OK && e->key_event==BSP_BTN_PRESS){
            if(s_ws.state!=PV_WS_READY || s_backend!=1){
                if(s_model.state==PV_FAILED && s_model.has_saved)request_attempt(&s_saved,false);
                else {ws_restart();show_online();}
                return;
            }
            audio_press();return;
        }
        if(e->key==BSP_BTN_OK && e->key_event==BSP_BTN_RELEASE){
            if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_RELEASE))
                audio_release();
            return;
        }
        if(e->key==BSP_BTN_UP && e->key_event==BSP_BTN_LONG && !s_attempt_pending && s_model.state!=PV_CONNECTING && s_model.state!=PV_SAVING){
            if(s_model.state==PV_SAVE_FAILED){save_and_finish();return;}
            if(ap_start()!=ESP_OK){failed("Setup failed");return;}
            s_close_pending=false;show("Wi-Fi setup","Open page on phone\nOK: cancel");
        } else if(e->key==BSP_BTN_OK && e->key_event==BSP_BTN_CLICK){
            if(s_setup){
                bool restore=pv_cancel(&s_model);atomic_store(&s_form_pending,false);s_close_pending=false;
                if(restore){if(setup_close()!=ESP_OK){failed("Close failed");return;}request_attempt(&s_saved,false);}
                else { /* No saved network: discard candidate, retain setup for retry. */
                    s_attempt_pending=false;s_barrier=false;s_deadline=0;
                    if(s_sta_enabled){esp_wifi_set_mode(WIFI_MODE_AP);s_sta_enabled=false;}
                    memset(&s_active,0,sizeof(s_active));show("Wi-Fi setup","No saved network\nOpen page to retry");
                }
            } else if(s_model.state==PV_FAILED){
                if(s_model.has_saved)request_attempt(&s_saved,false);
                else if(ap_start()==ESP_OK)show("Wi-Fi setup","Open page on phone");
            }
        }
        return;
    }
    if(e->kind==EV_FORM){
        if(!s_setup || s_barrier || s_model.state==PV_CONNECTING || s_model.state==PV_SAVING){atomic_store(&s_form_pending,false);return;}
        request_attempt(&e->config,true);return;
    }
    if(s_barrier || e->generation!=s_model.generation)return;
    if(e->kind==EV_LINK){
        if(e->ssid_len==strlen(s_active.ssid) && !memcmp(e->ssid,s_active.ssid,e->ssid_len) && pv_link(&s_model,e->generation))memcpy(s_bssid,e->bssid,6);
    } else if(e->kind==EV_IP){
        wifi_ap_record_t ap;esp_netif_ip_info_t current;
        /* Check actual current association and IP in addition to event order. */
        if(esp_wifi_sta_get_ap_info(&ap)!=ESP_OK || strcmp((char *)ap.ssid,s_active.ssid) || memcmp(ap.bssid,s_bssid,6) || esp_netif_get_ip_info(s_sta,&current)!=ESP_OK || !current.ip.addr || current.ip.addr!=e->ip.ip.addr)return;
        if(!pv_got_ip(&s_model,e->generation))return;
        s_wifi_retries=0;s_wifi_retry_at=0;
        s_deadline=0;snprintf(s_ip_text,sizeof(s_ip_text),IPSTR,IP2STR(&e->ip.ip));
        if(s_model.candidate)save_and_finish();else{ws_wifi_online();show_online();}
    } else if(e->kind==EV_DOWN){
        unsigned expected=e->generation+1;
        if(!atomic_compare_exchange_strong(&s_wifi_loss,&expected,0))return;
        audio_ws_lost();
        ws_destroy();pv_ws_event(&s_ws,PV_EV_WIFI_OFFLINE,0,esp_timer_get_time());
        if(s_model.state==PV_ONLINE || s_model.state==PV_SAVE_FAILED || s_model.state==PV_CONNECTING)failed("Wi-Fi disconnected");
    }
}
/* Mirror of the owner's form-acceptability so the HTTP task can reject busy
   states with 409 instead of acknowledging a submission it would drop. */
static void sync_form_open(void)
{
    atomic_store(&s_form_open,s_setup && !s_barrier &&
                 s_model.state!=PV_CONNECTING && s_model.state!=PV_SAVING);
}
static void owner(void *arg)
{
    (void)arg;s_last_input=esp_timer_get_time();
    /* Battery init owns the shared I2C bus and may wait up to ~5 s for the
       first SOC reading: run it after the first frame, before any network
       allocation. Failure only degrades the battery readout to "--". */
    if(bsp_battery_init()!=ESP_OK)ESP_LOGW(TAG,"battery unavailable; showing --");
    esp_err_t storage=load_saved();
    if(storage!=ESP_OK){show("Storage error","NVS kept intact\nRestart to retry");vTaskDelete(NULL);return;}
    if(network_init()!=ESP_OK){show("Network error","Restart to retry");vTaskDelete(NULL);return;}
    ESP_LOGI(TAG,"Stage A heap=%u largest=%u",(unsigned)esp_get_free_heap_size(),(unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    if(s_model.has_saved)request_attempt(&s_saved,false);
    else if(ap_start()==ESP_OK){s_model.state=PV_SETUP;show("Wi-Fi setup","Open page on phone\nOK: cancel");}
    else failed("Setup failed");
    app_event_t event;
    for(;;){
        if(xQueueReceive(s_queue,&event,pdMS_TO_TICKS(200))==pdTRUE){
            process(&event);memset(&event,0,sizeof(event));
            while(xQueueReceive(s_queue,&event,0)==pdTRUE){process(&event);memset(&event,0,sizeof(event));}
        }
        /* A full queue can drop an event copy, never the critical state. */
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_WS_LOST)){
            /* Ignore a close reported by a client that has since been
               replaced; its event may have been dropped from a full queue. */
            if(atomic_load(&s_pending_ws_generation)==atomic_load(&s_ws_client_generation)){
                audio_ws_lost();ws_destroy();
                pv_ws_event(&s_ws,PV_EV_TRANSPORT_CLOSED,s_ws.generation,esp_timer_get_time());
                show_online();
            }
        }
        int ready_notice=atomic_load(&s_voice_notice);
        if(ready_notice){app_event_t voice={.kind=EV_TEXT,.text=(pv_text_kind_t)ready_notice,.ws_client_generation=atomic_load(&s_ws_client_generation)};process(&voice);}
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_STOP_SENT))audio_stop_complete();
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_TTS_START))audio_downlink_start();
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_TTS_STOP))audio_downlink_stop();
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_PLAY_FAILED))audio_downlink_finish(true);
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_PLAY_DONE))audio_downlink_finish(false);
        if(pv_audio_take_pending(&s_pending,PV_AUDIO_PENDING_RELEASE))audio_release();
        unsigned loss=atomic_load(&s_wifi_loss);
        if(loss){
            if(loss-1==s_model.generation && !s_barrier){app_event_t down={.kind=EV_DOWN,.generation=loss-1};process(&down);}
            else atomic_compare_exchange_strong(&s_wifi_loss,&loss,0);
        }
        int notice=atomic_load(&s_voice_notice);
        if(notice){app_event_t voice={.kind=EV_TEXT,.text=(pv_text_kind_t)notice,.ws_client_generation=atomic_load(&s_ws_client_generation)};process(&voice);}
        int64_t now=esp_timer_get_time();
        if(s_ws.state==PV_WS_READY && s_backend==0 && s_backend_at && now-s_backend_at>15000000){s_backend=2;show_online();}
        if(now-s_diag_at>=60000000){
            s_diag_at=now;
            ESP_LOGI(TAG,"health heap=%u largest=%u owner_stack=%u up_drop=%u down_drop=%u",(unsigned)esp_get_free_heap_size(),(unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),(unsigned)uxTaskGetStackHighWaterMark(NULL),(unsigned)atomic_load(&s_audio_dropped),(unsigned)atomic_load(&s_down_dropped));
        }
        bool audio_busy=s_audio.state==PV_AUDIO_LISTENING || s_audio.state==PV_AUDIO_THINKING || s_wait_reply || pv_down_speaking(&s_down);
        if(pv_volume_save_due(s_volume_dirty,audio_busy,now/1000,s_volume_changed)){
            nvs_handle_t h;esp_err_t e=nvs_open("passport",NVS_READWRITE,&h);
            if(e==ESP_OK){e=nvs_set_u8(h,"volume",s_volume);if(e==ESP_OK)e=nvs_commit(h);nvs_close(h);}
            if(e==ESP_OK)s_volume_dirty=false;
            else {s_volume_changed=now/1000;ESP_LOGW(TAG,"volume save failed: %s",esp_err_to_name(e));}
        }
        if(s_wait_reply || s_audio.state==PV_AUDIO_THINKING){
            if(!s_thinking_at)s_thinking_at=now;
            else if(now-s_thinking_at>20000000){audio_runtime_reset();audio_uplink_idle();s_backend=2;s_wait_reply=false;s_thinking_at=0;show_online();}
        }else s_thinking_at=0;
        if(s_wifi_retry_at && now>=s_wifi_retry_at && !s_setup && s_model.state==PV_FAILED){
            s_wifi_retry_at=0;request_attempt(&s_saved,false);
        }
        if(s_deadline && now>=s_deadline){s_attempt_pending=false;s_barrier=false;failed("Connection timeout");}
        if(s_close_pending && now>=s_close_at){
            if(setup_close()!=ESP_OK)failed("Close failed");else show_online();
        }
        if(pv_ws_timeout_due(&s_ws,now) &&
           pv_ws_event(&s_ws,PV_EV_TIMEOUT,s_ws.generation,now)){
            audio_ws_sync();
            ws_destroy();
            if(s_ws.state==PV_WS_CONNECTING && ws_start()!=ESP_OK)
                pv_ws_event(&s_ws,PV_EV_TRANSPORT_ERROR,s_ws.generation,now);
            audio_ws_sync();
            show_online();
        }
        sync_form_open();
        if(now-s_last_input>60000000)bsp_display_backlight(15);
    }
}
void passport_app_start(void)
{
    s_queue=xQueueCreate(8,sizeof(app_event_t));
    s_ws_send_lock=xSemaphoreCreateMutex();
    s_audio_io_lock=xSemaphoreCreateMutex();
    s_down_acc_lock=xSemaphoreCreateMutex();
    /* Lightweight and needed before bsp_audio_init so a first TTS reply can
       queue while the codec is still being opened. */
    s_down_queue=xQueueCreate(PV_DOWN_QUEUE_DEPTH,sizeof(down_frame_t));
    pv_audio_init(&s_audio);
    pv_down_init(&s_down);
    pv_down_rx_init(&s_down_rx);
    atomic_store(&s_stop_sent, true);
    if(!s_queue || !s_ws_send_lock || !s_audio_io_lock || !s_down_acc_lock ||
       !s_down_queue || !ui_start()){ESP_LOGE(TAG,"Application allocation failed");return;}
    if(xTaskCreate(owner,"passport_owner",6144,NULL,5,NULL)!=pdPASS){show("Task error","Restart to retry");return;}
}
/* Called from the BSP callback: bounded event copy only. */
void passport_app_key(bsp_btn_t key,bsp_btn_ev_t event)
{
    if(!s_queue)return;
    if(key==BSP_BTN_OK && event==BSP_BTN_RELEASE)
        pv_audio_note_pending(&s_pending,PV_AUDIO_PENDING_RELEASE);
    app_event_t e={.kind=EV_KEY,.key=key,.key_event=event};
    (void)xQueueSend(s_queue,&e,0);
}
