#pragma once
/* Stage B: bounded WebSocket gateway protocol logic, independent of ESP-IDF. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PV_WS_MAX_MESSAGE 4096u
#define PV_WS_SESSION_MAX 64u
#define PV_WS_HOST_MAX 63u
#define PV_WS_PATH_MAX 127u
#define PV_WS_CONNECT_TIMEOUT_MS 10000u
#define PV_WS_HELLO_TIMEOUT_MS 5000u
#define PV_WS_BACKOFF_BASE_MS 1000u
#define PV_WS_BACKOFF_MAX_MS 30000u

typedef struct {
    char host[PV_WS_HOST_MAX + 1];
    uint16_t port;
    char path[PV_WS_PATH_MAX + 1];
} pv_ws_endpoint_t;

/* Splits a validated ws://host:port[/path] URL; empty path becomes "/". */
bool pv_ws_parse_endpoint(const char *url, pv_ws_endpoint_t *out);

/* Deterministic client hello. Returns bytes written or 0 if capacity is short. */
size_t pv_ws_build_hello(char *out, size_t capacity);

typedef struct {
    char format[8];
    int sample_rate;
    int channels;
    int frame_duration;
    bool present;
} pv_audio_params_t;

typedef struct {
    char session_id[PV_WS_SESSION_MAX + 1];
    int version;
    pv_audio_params_t audio;
} pv_server_hello_t;

typedef enum { PV_HELLO_OK, PV_HELLO_UNSUPPORTED, PV_HELLO_MALFORMED } pv_hello_result_t;

/* Bounded, top-level-only parse. Only "hello"/"server_hello" types are accepted. */
pv_hello_result_t pv_parse_server_hello(const char *message, size_t len,
                                        pv_server_hello_t *out);

/* Stage D downlink must be exactly PCM 24 kHz/16-bit/mono/20 ms. */
#define PV_DOWN_SAMPLE_RATE 24000
#define PV_DOWN_FRAME_DURATION_MS 20
bool pv_server_audio_valid(const pv_server_hello_t *hello);

typedef enum {
    PV_TTS_NONE,
    PV_TTS_START,
    PV_TTS_STOP,
    PV_TTS_SENTENCE_START,
} pv_tts_state_t;

typedef enum { PV_TTS_OK, PV_TTS_UNSUPPORTED, PV_TTS_MALFORMED } pv_tts_result_t;

typedef enum {
    PV_TEXT_IGNORE,       /* non-hello/tts, sentence_start, or unparsable non-hello */
    PV_TEXT_HELLO,        /* complete, bounded hello/server_hello */
    PV_TEXT_HELLO_INVALID,/* type is hello but the message is malformed */
    PV_TEXT_TTS_START,
    PV_TEXT_TTS_STOP,
    PV_TEXT_VOICE_READY,
    PV_TEXT_VOICE_ERROR,
    PV_TEXT_INPUT_SHORT,
    PV_TEXT_NOTIFY_START, /* Stage H proactive announce bracket (UI only) */
    PV_TEXT_NOTIFY_STOP,
} pv_text_kind_t;

/* Classify a complete text message in the WS callback. Only hello types are
   fully validated; malformed non-hello traffic is ignored, never fatal. */
pv_text_kind_t pv_ws_classify_text(const char *message, size_t len,
                                   pv_server_hello_t *hello);

/* Bounded top-level parse for {"type":"tts","state":"..."} messages. */
pv_tts_result_t pv_parse_tts_state(const char *message, size_t len,
                                   pv_tts_state_t *out);

typedef enum {
    PV_NOTIFY_NONE,
    PV_NOTIFY_START,
    PV_NOTIFY_STOP,
    PV_NOTIFY_CANCEL,
} pv_notify_state_t;

typedef enum { PV_NOTIFY_OK, PV_NOTIFY_UNSUPPORTED, PV_NOTIFY_MALFORMED } pv_notify_result_t;

/* Bounded top-level parse for {"type":"notify","state":"...","flash":bool}.
   Stage H: brackets a proactive announcement so the UI can show a notice;
   "flash" defaults to false when absent. */
pv_notify_result_t pv_parse_notify(const char *message, size_t len,
                                   pv_notify_state_t *state, bool *flash);
/* Zero means absent/invalid. IDs bind grants, audio and completion on one WS. */
unsigned pv_ws_request_id(const char *message, size_t len);

typedef enum {
    PV_WS_IDLE,
    PV_WS_CONNECTING,
    PV_WS_HANDSHAKING,
    PV_WS_READY,
    PV_WS_RETRY_WAIT,
} pv_ws_state_t;

typedef enum {
    PV_EV_WIFI_ONLINE,
    PV_EV_WIFI_OFFLINE,
    PV_EV_TRANSPORT_OPEN,
    PV_EV_TRANSPORT_CLOSED,
    PV_EV_TRANSPORT_ERROR,
    PV_EV_HELLO_OK,
    PV_EV_HELLO_INVALID,
    PV_EV_TIMEOUT,
} pv_ws_event_t;

typedef struct {
    pv_ws_state_t state;
    unsigned generation;
    unsigned attempt;      /* consecutive failures; drives backoff */
    int64_t deadline_us;   /* 0 = no pending timer */
    char session_id[PV_WS_SESSION_MAX + 1];
} pv_ws_model_t;

uint32_t pv_ws_backoff_ms(unsigned attempt);
void pv_ws_init(pv_ws_model_t *model);
bool pv_ws_event(pv_ws_model_t *model, pv_ws_event_t event, unsigned generation,
                 int64_t now_us);
bool pv_ws_hello(pv_ws_model_t *model, unsigned generation, int64_t now_us,
                 const pv_server_hello_t *hello);
bool pv_ws_timeout_due(const pv_ws_model_t *model, int64_t now_us);

/* Transport event classification kept pure so the clean-close mapping is
   host-testable. The caller supplies its SDK's numeric event ids. */
typedef enum {
    PV_WS_KIND_IGNORE,
    PV_WS_KIND_OPEN,
    PV_WS_KIND_CLOSED,
    PV_WS_KIND_ERROR,
    PV_WS_KIND_DATA,
} pv_ws_kind_t;

typedef struct {
    int32_t connected, disconnected, closed, finish, error, data;
} pv_ws_event_ids_t;

pv_ws_kind_t pv_ws_classify_event(int32_t id, const pv_ws_event_ids_t *ids);
