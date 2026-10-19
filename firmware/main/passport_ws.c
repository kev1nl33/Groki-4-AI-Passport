#include "passport_ws.h"
#include <ctype.h>
#include <stdio.h>
#include <string.h>

static bool is_space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }

bool pv_ws_parse_endpoint(const char *u, pv_ws_endpoint_t *out)
{
    if (!u || !out || strncmp(u, "ws://", 5)) return false;
    const char *p = u + 5, *host = p;
    bool label_start = true;
    for (; *p && *p != ':'; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '.') {
            if (label_start || p[-1] == '-') return false;
            label_start = true;
        } else {
            if (!(isalnum(c) && c < 128) && c != '-') return false;
            if (label_start && c == '-') return false;
            label_start = false;
        }
    }
    size_t host_len = (size_t)(p - host);
    if (host_len == 0 || host_len > PV_WS_HOST_MAX || label_start || p[-1] == '-' || *p++ != ':')
        return false;
    unsigned port = 0, digits = 0;
    while (*p >= '0' && *p <= '9') {
        port = port * 10 + (unsigned)(*p++ - '0');
        if (++digits > 5 || port > 65535) return false;
    }
    if (!digits || !port) return false;
    const char *path = *p ? p : "/";
    size_t path_len = strlen(path);
    if (path_len > PV_WS_PATH_MAX || path[0] != '/') return false;
    for (const char *q = path; *q; ++q) {
        unsigned char c = (unsigned char)*q;
        if (!(isalnum(c) && c < 128) && !strchr("/-._~", c)) return false;
    }
    pv_ws_endpoint_t parsed = {0};
    memcpy(parsed.host, host, host_len);
    parsed.port = (uint16_t)port;
    memcpy(parsed.path, path, path_len);
    *out = parsed;
    return true;
}

size_t pv_ws_build_hello(char *out, size_t capacity)
{
    if (!out) return 0;
    const char *hello =
        "{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
        "\"features\":{\"mcp\":false},"
        "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":16000,"
        "\"channels\":1,\"frame_duration\":20}}";
    int n = snprintf(out, capacity, "%s", hello);
    return n > 0 && (size_t)n < capacity ? (size_t)n : 0;
}

/* Bounded JSON string reader. When out is NULL the value is validated and
   skipped; otherwise it must fit capacity (including the NUL). */
static bool read_hex4(const char *s, size_t len, size_t *i, unsigned *out)
{
    if (*i + 4 > len) return false;
    unsigned value = 0;
    for (unsigned n = 0; n < 4; ++n) {
        char c = s[(*i)++];
        value <<= 4;
        if (c >= '0' && c <= '9') value |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') value |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') value |= (unsigned)(c - 'A' + 10);
        else return false;
    }
    *out = value;
    return true;
}

static bool append_utf8(char *out, size_t capacity, size_t *used, unsigned cp)
{
    unsigned char bytes[4];
    size_t n;
    if (cp <= 0x7F) { bytes[0] = (unsigned char)cp; n = 1; }
    else if (cp <= 0x7FF) {
        bytes[0] = (unsigned char)(0xC0 | (cp >> 6));
        bytes[1] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp <= 0xFFFF) {
        bytes[0] = (unsigned char)(0xE0 | (cp >> 12));
        bytes[1] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        bytes[2] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 3;
    } else if (cp <= 0x10FFFF) {
        bytes[0] = (unsigned char)(0xF0 | (cp >> 18));
        bytes[1] = (unsigned char)(0x80 | ((cp >> 12) & 0x3F));
        bytes[2] = (unsigned char)(0x80 | ((cp >> 6) & 0x3F));
        bytes[3] = (unsigned char)(0x80 | (cp & 0x3F));
        n = 4;
    } else {
        return false;
    }
    if (out) {
        if (*used + n >= capacity) return false;
        memcpy(out + *used, bytes, n);
    }
    *used += n;
    return true;
}

/* Bounded JSON string reader. Supports common escapes (" \\ \/ b f n r t
   and \uXXXX including surrogate pairs). When out is NULL the value is
   validated and skipped. */
static bool read_string(const char *s, size_t len, size_t *i, char *out, size_t capacity)
{
    if (*i >= len || s[*i] != '"') return false;
    ++*i;
    size_t used = 0;
    while (*i < len) {
        unsigned char c = (unsigned char)s[(*i)++];
        if (c == '"') {
            if (out) out[used] = 0;
            return true;
        }
        if (c < 0x20) return false;
        if (c == '\\') {
            if (*i >= len) return false;
            char esc = s[(*i)++];
            unsigned cp;
            switch (esc) {
            case '"': cp = '"'; break;
            case '\\': cp = '\\'; break;
            case '/': cp = '/'; break;
            case 'b': cp = '\b'; break;
            case 'f': cp = '\f'; break;
            case 'n': cp = '\n'; break;
            case 'r': cp = '\r'; break;
            case 't': cp = '\t'; break;
            case 'u':
                if (!read_hex4(s, len, i, &cp) || cp == 0) return false;
                if (cp >= 0xD800 && cp <= 0xDBFF) {
                    unsigned low;
                    if (*i + 2 > len || s[*i] != '\\' || s[*i + 1] != 'u')
                        return false;
                    *i += 2;
                    if (!read_hex4(s, len, i, &low) || low < 0xDC00 || low > 0xDFFF)
                        return false;
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                    return false;
                }
                break;
            default:
                return false;
            }
            if (!append_utf8(out, capacity, &used, cp)) return false;
            continue;
        }
        if (out) {
            if (used + 1 >= capacity) return false;
            out[used++] = (char)c;
        }
    }
    return false;
}

/* Skip one JSON value with bounded nesting; keys inside it are not top-level. */
static bool skip_value(const char *s, size_t len, size_t *i, unsigned depth)
{
    if (*i >= len || depth > 8) return false;
    char c = s[*i];
    if (c == '"') return read_string(s, len, i, NULL, 0);
    if (c == '{' || c == '[') {
        char open = c, close = c == '{' ? '}' : ']';
        unsigned nested = 0;
        while (*i < len) {
            char x = s[(*i)];
            if (x == '"') {
                if (!read_string(s, len, i, NULL, 0)) return false;
                continue;
            }
            if (x == open) ++nested;
            else if (x == close) {
                if (--nested == 0) { ++*i; return true; }
            }
            ++*i;
        }
        return false;
    }
    size_t start = *i;
    while (*i < len && (isalnum((unsigned char)s[*i]) || strchr("-+.", s[*i]))) ++*i;
    return *i > start;
}


static bool read_int(const char *s, size_t len, size_t *i, int *out)
{
    if (*i >= len || s[*i] < '0' || s[*i] > '9') return false;
    unsigned value = 0, digits = 0;
    while (*i < len && s[*i] >= '0' && s[*i] <= '9') {
        value = value * 10 + (unsigned)(s[*i] - '0');
        ++(*i);
        if (++digits > 6) return false;
    }
    *out = (int)value;
    return true;
}

/* Nested top-level object; unknown fields are skipped, never promoted. */
static bool parse_audio_params(const char *s, size_t len, size_t *i,
                               pv_audio_params_t *out)
{
    while (*i < len && is_space(s[*i])) ++*i;
    if (*i >= len || s[(*i)++] != '{') return false;
    pv_audio_params_t parsed = {0};
    parsed.present = true;
    bool closed = false;
    while (*i < len) {
        while (*i < len && is_space(s[*i])) ++*i;
        if (*i >= len) break;
        if (s[*i] == '}') { ++*i; closed = true; break; }
        if (s[*i] != '"') return false;
        char key[24];
        if (!read_string(s, len, i, key, sizeof(key))) return false;
        while (*i < len && is_space(s[*i])) ++*i;
        if (*i >= len || s[(*i)++] != ':') return false;
        while (*i < len && is_space(s[*i])) ++*i;
        if (*i >= len) return false;
        if (!strcmp(key, "format")) {
            if (!read_string(s, len, i, parsed.format, sizeof(parsed.format)))
                return false;
        } else if (!strcmp(key, "sample_rate")) {
            if (!read_int(s, len, i, &parsed.sample_rate)) return false;
        } else if (!strcmp(key, "channels")) {
            if (!read_int(s, len, i, &parsed.channels)) return false;
        } else if (!strcmp(key, "frame_duration")) {
            if (!read_int(s, len, i, &parsed.frame_duration)) return false;
        } else if (!skip_value(s, len, i, 0)) {
            return false;
        }
        while (*i < len && is_space(s[*i])) ++*i;
        if (*i < len && s[*i] == ',') { ++*i; continue; }
        if (*i < len && s[*i] == '}') { ++*i; closed = true; break; }
        return false;
    }
    if (!closed) return false;
    *out = parsed;
    return true;
}

pv_hello_result_t pv_parse_server_hello(const char *message, size_t len,
                                        pv_server_hello_t *out)
{
    if (!message || !out || len == 0 || len > PV_WS_MAX_MESSAGE) return PV_HELLO_MALFORMED;
    pv_server_hello_t parsed = {0};
    size_t i = 0;
    while (i < len && is_space(message[i])) ++i;
    if (i >= len || message[i++] != '{') return PV_HELLO_MALFORMED;
    bool type_seen = false, closed = false;
    char type[16] = {0};
    while (i < len) {
        while (i < len && is_space(message[i])) ++i;
        if (i >= len) break;
        if (message[i] == '}') { ++i; closed = true; break; }
        if (message[i] != '"') return PV_HELLO_MALFORMED;
        char key[32];
        if (!read_string(message, len, &i, key, sizeof(key))) return PV_HELLO_MALFORMED;
        while (i < len && is_space(message[i])) ++i;
        if (i >= len || message[i++] != ':') return PV_HELLO_MALFORMED;
        while (i < len && is_space(message[i])) ++i;
        if (i >= len) return PV_HELLO_MALFORMED;
        if (!strcmp(key, "type")) {
            if (!read_string(message, len, &i, type, sizeof(type))) return PV_HELLO_MALFORMED;
            type_seen = true;
        } else if (!strcmp(key, "session_id")) {
            if (!read_string(message, len, &i, parsed.session_id, sizeof(parsed.session_id)))
                return PV_HELLO_MALFORMED;
        } else if (!strcmp(key, "version")) {
            if (message[i] < '0' || message[i] > '9') return PV_HELLO_MALFORMED;
            unsigned value = 0, digits = 0;
            while (i < len && message[i] >= '0' && message[i] <= '9') {
                value = value * 10 + (unsigned)(message[i++] - '0');
                if (++digits > 5) return PV_HELLO_MALFORMED;
            }
            parsed.version = (int)value;
        } else if (!strcmp(key, "audio_params")) {
            if (!parse_audio_params(message, len, &i, &parsed.audio))
                return PV_HELLO_MALFORMED;
        } else if (!skip_value(message, len, &i, 0)) {
            return PV_HELLO_MALFORMED;
        }
        while (i < len && is_space(message[i])) ++i;
        if (i < len && message[i] == ',') { ++i; continue; }
        if (i < len && message[i] == '}') { ++i; closed = true; break; }
        return PV_HELLO_MALFORMED;
    }
    if (!closed) return PV_HELLO_MALFORMED;
    while (i < len && (is_space(message[i]) || message[i] == '\0')) ++i;
    if (i != len) return PV_HELLO_MALFORMED;
    if (!type_seen) return PV_HELLO_MALFORMED;
    if (strcmp(type, "hello") && strcmp(type, "server_hello")) return PV_HELLO_UNSUPPORTED;
    *out = parsed;
    return PV_HELLO_OK;
}

uint32_t pv_ws_backoff_ms(unsigned attempt)
{
    uint64_t delay = PV_WS_BACKOFF_BASE_MS;
    for (unsigned i = 0; i < attempt && delay < PV_WS_BACKOFF_MAX_MS; ++i) delay *= 2;
    return delay >= PV_WS_BACKOFF_MAX_MS ? PV_WS_BACKOFF_MAX_MS : (uint32_t)delay;
}

void pv_ws_init(pv_ws_model_t *model)
{
    if (model) memset(model, 0, sizeof(*model));
}

static bool fail(pv_ws_model_t *m, int64_t now_us)
{
    m->deadline_us = now_us + (int64_t)pv_ws_backoff_ms(m->attempt) * 1000;
    ++m->attempt;
    m->state = PV_WS_RETRY_WAIT;
    m->session_id[0] = 0;
    return true;
}

bool pv_ws_event(pv_ws_model_t *m, pv_ws_event_t event, unsigned generation,
                 int64_t now_us)
{
    if (!m) return false;
    if (event == PV_EV_WIFI_OFFLINE) {
        if (m->state == PV_WS_IDLE) return false;
        ++m->generation;
        m->state = PV_WS_IDLE;
        m->attempt = 0;
        m->deadline_us = 0;
        m->session_id[0] = 0;
        return true;
    }
    if (event == PV_EV_WIFI_ONLINE) {
        if (m->state != PV_WS_IDLE) return false;
        ++m->generation;
        m->state = PV_WS_CONNECTING;
        m->attempt = 0;
        m->deadline_us = now_us + (int64_t)PV_WS_CONNECT_TIMEOUT_MS * 1000;
        return true;
    }
    if (generation != m->generation) return false;
    switch (event) {
    case PV_EV_TRANSPORT_OPEN:
        if (m->state != PV_WS_CONNECTING) return false;
        m->state = PV_WS_HANDSHAKING;
        m->deadline_us = now_us + (int64_t)PV_WS_HELLO_TIMEOUT_MS * 1000;
        return true;
    case PV_EV_TRANSPORT_CLOSED:
    case PV_EV_TRANSPORT_ERROR:
    case PV_EV_HELLO_INVALID:
        if (m->state != PV_WS_CONNECTING && m->state != PV_WS_HANDSHAKING &&
            m->state != PV_WS_READY) return false;
        return fail(m, now_us);
    case PV_EV_TIMEOUT:
        if (m->state == PV_WS_CONNECTING || m->state == PV_WS_HANDSHAKING) {
            return fail(m, now_us);
        }
        if (m->state == PV_WS_RETRY_WAIT) {
            ++m->generation;
            m->state = PV_WS_CONNECTING;
            m->deadline_us = now_us + (int64_t)PV_WS_CONNECT_TIMEOUT_MS * 1000;
            return true;
        }
        return false;
    default:
        return false;
    }
}

bool pv_ws_hello(pv_ws_model_t *m, unsigned generation, int64_t now_us,
                 const pv_server_hello_t *hello)
{
    if (!m || !hello || generation != m->generation || m->state != PV_WS_HANDSHAKING)
        return false;
    /* A reply without the expected protocol version or a session id is not a
       successful handshake; the caller must retry instead of showing Ready. */
    if (hello->version != 1 || hello->session_id[0] == 0) return false;
    (void)now_us;
    m->state = PV_WS_READY;
    m->attempt = 0;
    m->deadline_us = 0;
    memcpy(m->session_id, hello->session_id, sizeof(m->session_id));
    m->session_id[sizeof(m->session_id) - 1] = 0;
    return true;
}

bool pv_ws_timeout_due(const pv_ws_model_t *m, int64_t now_us)
{
    return m && m->deadline_us != 0 && now_us >= m->deadline_us;
}

pv_ws_kind_t pv_ws_classify_event(int32_t id, const pv_ws_event_ids_t *ids)
{
    if (!ids) return PV_WS_KIND_IGNORE;
    if (id == ids->connected) return PV_WS_KIND_OPEN;
    /* esp_websocket_client 1.8.0 emits CLOSED for a clean peer close and a
       separate DISCONNECTED only in some paths; FINISH covers thread exit. */
    if (id == ids->disconnected || id == ids->closed || id == ids->finish)
        return PV_WS_KIND_CLOSED;
    if (id == ids->error) return PV_WS_KIND_ERROR;
    if (id == ids->data) return PV_WS_KIND_DATA;
    return PV_WS_KIND_IGNORE;
}

bool pv_server_audio_valid(const pv_server_hello_t *hello)
{
    return hello && hello->audio.present &&
           !strcmp(hello->audio.format, "pcm") &&
           hello->audio.sample_rate == PV_DOWN_SAMPLE_RATE &&
           hello->audio.channels == 1 &&
           hello->audio.frame_duration == PV_DOWN_FRAME_DURATION_MS;
}

pv_tts_result_t pv_parse_tts_state(const char *message, size_t len,
                                   pv_tts_state_t *out)
{
    if (!message || !out || len == 0 || len > PV_WS_MAX_MESSAGE)
        return PV_TTS_MALFORMED;
    size_t i = 0;
    while (i < len && is_space(message[i])) ++i;
    if (i >= len || message[i++] != '{') return PV_TTS_MALFORMED;
    char type[16] = {0}, state[24] = {0};
    bool type_seen = false, state_seen = false, closed = false;
    while (i < len) {
        while (i < len && is_space(message[i])) ++i;
        if (i >= len) break;
        if (message[i] == '}') { ++i; closed = true; break; }
        if (message[i] != '"') return PV_TTS_MALFORMED;
        char key[32];
        if (!read_string(message, len, &i, key, sizeof(key)))
            return PV_TTS_MALFORMED;
        while (i < len && is_space(message[i])) ++i;
        if (i >= len || message[i++] != ':') return PV_TTS_MALFORMED;
        while (i < len && is_space(message[i])) ++i;
        if (i >= len) return PV_TTS_MALFORMED;
        if (!strcmp(key, "type")) {
            if (!read_string(message, len, &i, type, sizeof(type)))
                return PV_TTS_MALFORMED;
            type_seen = true;
        } else if (!strcmp(key, "state")) {
            if (!read_string(message, len, &i, state, sizeof(state)))
                return PV_TTS_MALFORMED;
            state_seen = true;
        } else if (!skip_value(message, len, &i, 0)) {
            return PV_TTS_MALFORMED;
        }
        while (i < len && is_space(message[i])) ++i;
        if (i < len && message[i] == ',') { ++i; continue; }
        if (i < len && message[i] == '}') { ++i; closed = true; break; }
        return PV_TTS_MALFORMED;
    }
    if (!closed || !state_seen) return PV_TTS_MALFORMED;
    while (i < len && (is_space(message[i]) || message[i] == '\0')) ++i;
    if (i != len) return PV_TTS_MALFORMED;
    if (!type_seen || strcmp(type, "tts")) return PV_TTS_UNSUPPORTED;
    if (!strcmp(state, "start")) *out = PV_TTS_START;
    else if (!strcmp(state, "stop")) *out = PV_TTS_STOP;
    else if (!strcmp(state, "sentence_start")) *out = PV_TTS_SENTENCE_START;
    else return PV_TTS_MALFORMED;
    return PV_TTS_OK;
}

static bool peek_top_level_type(const char *s, size_t len, char *out, size_t capacity)
{
    if (!s || !out || len == 0 || len > PV_WS_MAX_MESSAGE) return false;
    size_t i = 0;
    while (i < len && is_space(s[i])) ++i;
    if (i >= len || s[i++] != '{') return false;
    while (i < len) {
        while (i < len && is_space(s[i])) ++i;
        if (i >= len) return false;
        if (s[i] == '}') return false;
        if (s[i] != '"') return false;
        char key[32];
        if (!read_string(s, len, &i, key, sizeof(key))) return false;
        while (i < len && is_space(s[i])) ++i;
        if (i >= len || s[i++] != ':') return false;
        while (i < len && is_space(s[i])) ++i;
        if (i >= len) return false;
        if (!strcmp(key, "type"))
            return read_string(s, len, &i, out, capacity);
        if (!skip_value(s, len, &i, 0)) return false;
        while (i < len && is_space(s[i])) ++i;
        if (i < len && s[i] == ',') { ++i; continue; }
        if (i < len && s[i] == '}') return false;
        return false;
    }
    return false;
}

pv_text_kind_t pv_ws_classify_text(const char *message, size_t len,
                                   pv_server_hello_t *hello)
{
    char type[16];
    if (!peek_top_level_type(message, len, type, sizeof(type)))
        return PV_TEXT_IGNORE;
    if (!strcmp(type, "hello") || !strcmp(type, "server_hello")) {
        pv_server_hello_t parsed;
        if (pv_parse_server_hello(message, len, &parsed) != PV_HELLO_OK)
            return PV_TEXT_HELLO_INVALID;
        if (hello) *hello = parsed;
        return PV_TEXT_HELLO;
    }
    if (!strcmp(type,"voice_ready")) return PV_TEXT_VOICE_READY;
    if (!strcmp(type,"voice_error")) return PV_TEXT_VOICE_ERROR;
    if (!strcmp(type,"input_short")) return PV_TEXT_INPUT_SHORT;
    if (!strcmp(type, "tts")) {
        pv_tts_state_t state;
        if (pv_parse_tts_state(message, len, &state) != PV_TTS_OK)
            return PV_TEXT_IGNORE;
        if (state == PV_TTS_START) return PV_TEXT_TTS_START;
        if (state == PV_TTS_STOP) return PV_TEXT_TTS_STOP;
    }
    return PV_TEXT_IGNORE;
}
