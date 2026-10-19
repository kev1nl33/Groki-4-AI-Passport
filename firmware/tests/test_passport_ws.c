#include "passport_ws.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_hello(void)
{
    const char *expected =
        "{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
        "\"features\":{\"mcp\":false},"
        "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":16000,"
        "\"channels\":1,\"frame_duration\":20}}";
    char buf[256];
    size_t n = pv_ws_build_hello(buf, sizeof(buf));
    assert(n == strlen(expected));
    assert(!strcmp(buf, expected));
    assert(pv_ws_build_hello(buf, n) == 0);      /* no room for the NUL */
    assert(pv_ws_build_hello(buf, n + 1) == n);
}

static void test_endpoint(void)
{
    pv_ws_endpoint_t e;
    assert(pv_ws_parse_endpoint("ws://192.168.1.2:8765", &e));
    assert(!strcmp(e.host, "192.168.1.2") && e.port == 8765 && !strcmp(e.path, "/"));
    assert(pv_ws_parse_endpoint("ws://gw.local:9000/pcm", &e));
    assert(!strcmp(e.host, "gw.local") && e.port == 9000 && !strcmp(e.path, "/pcm"));
    const char *bad[] = {
        "", "http://h:1", "wss://h:1", "ws://h", "ws://:1", "ws://h:0",
        "ws://h:65536", "ws://h:1?x", "ws://h:1#x", "ws://h:1/ x",
        "ws://user@h:1", "ws://-h:1", "ws://h..x:1", "ws://h:abc",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        assert(!pv_ws_parse_endpoint(bad[i], &e));
    }
    char long_host[96] = "ws://";
    size_t i = 5;
    for (; i < 5 + PV_WS_HOST_MAX + 4; ++i) long_host[i] = 'a';
    strcpy(long_host + i, ":1");
    assert(!pv_ws_parse_endpoint(long_host, &e));
}

static void test_parse_hello(void)
{
    pv_server_hello_t h;
    const char *ok = "{\"type\":\"hello\",\"session_id\":\"abc123\",\"version\":1}";
    assert(pv_parse_server_hello(ok, strlen(ok), &h) == PV_HELLO_OK);
    assert(!strcmp(h.session_id, "abc123") && h.version == 1);

    const char *alias = "{ \"type\" : \"server_hello\" , \"session_id\" : \"s-1\" }";
    assert(pv_parse_server_hello(alias, strlen(alias), &h) == PV_HELLO_OK);
    assert(!strcmp(h.session_id, "s-1") && h.version == 0);

    /* Nested values must not satisfy top-level keys. */
    const char *nested =
        "{\"type\":\"hello\",\"features\":{\"type\":\"x\",\"version\":9},\"version\":2}";
    assert(pv_parse_server_hello(nested, strlen(nested), &h) == PV_HELLO_OK);
    assert(h.version == 2);

    /* Stage C: server audio_params are extra data and must be ignored. */
    const char *pcm = "{\"type\":\"hello\",\"session_id\":\"abc\",\"version\":1,"
        "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":24000,"
        "\"channels\":1,\"frame_duration\":20}}";
    assert(pv_parse_server_hello(pcm, strlen(pcm), &h) == PV_HELLO_OK);
    assert(!strcmp(h.session_id, "abc") && h.version == 1);

    const char *tts = "{\"type\":\"tts\",\"session_id\":\"s\"}";
    assert(pv_parse_server_hello(tts, strlen(tts), &h) == PV_HELLO_UNSUPPORTED);

    const char *bad[] = {
        "",
        "[]",
        "{\"session_id\":\"s\"}",
        "{\"type\":\"hello\"",
        "{\"type\":\"hello\",\"session_id\":\"unterminated}",
        "{\"type\":\"hello\",\"version\":\"1\"}",
        "{\"type\":\"hello\",\"version\":123456}",
        "{\"type\":\"hello\",\"session_id\":\"01234567890123456789012345678901234567890123456789012345678901234567\"}",
        "{\"type\":\"hello\"} trailing",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        assert(pv_parse_server_hello(bad[i], strlen(bad[i]), &h) == PV_HELLO_MALFORMED);
    }
    char big[PV_WS_MAX_MESSAGE + 2];
    memset(big, ' ', sizeof(big));
    big[0] = '{';
    assert(pv_parse_server_hello(big, PV_WS_MAX_MESSAGE + 1, &h) == PV_HELLO_MALFORMED);
    /* Common JSON escapes are accepted, including surrogate pairs. */
    const char *escaped =
        "{\"type\":\"hello\",\"session_id\":\"a\\\"b\\\\c\\n\\u4F60\"}";
    assert(pv_parse_server_hello(escaped, strlen(escaped), &h) == PV_HELLO_OK);
    assert(!strcmp(h.session_id, "a\"b\\c\n\xE4\xBD\xA0"));
    const char *emoji =
        "{\"type\":\"hello\",\"session_id\":\"\\uD83D\\uDE00\"}";
    assert(pv_parse_server_hello(emoji, strlen(emoji), &h) == PV_HELLO_OK);
    assert(!strcmp(h.session_id, "\xF0\x9F\x98\x80"));
    const char *trailing_nul = "{\"type\":\"hello\"}";
    assert(pv_parse_server_hello(trailing_nul, strlen(trailing_nul) + 1, &h) == PV_HELLO_OK);
}

static void test_server_audio_params(void)
{
    pv_server_hello_t h;
    const char *pcm =
        "{\"type\":\"hello\",\"session_id\":\"s\",\"version\":1,"
        "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":24000,"
        "\"channels\":1,\"frame_duration\":20}}";
    assert(pv_parse_server_hello(pcm, strlen(pcm), &h) == PV_HELLO_OK);
    assert(h.audio.present && !strcmp(h.audio.format, "pcm"));
    assert(h.audio.sample_rate == 24000 && h.audio.channels == 1);
    assert(h.audio.frame_duration == 20);
    assert(pv_server_audio_valid(&h));

    const char *reordered =
        "{\"audio_params\":{\"frame_duration\":20,\"channels\":1,"
        "\"sample_rate\":24000,\"format\":\"pcm\",\"volume\":3},"
        "\"version\":1,\"session_id\":\"s\",\"type\":\"server_hello\"}";
    assert(pv_parse_server_hello(reordered, strlen(reordered), &h) == PV_HELLO_OK);
    assert(pv_server_audio_valid(&h));

    const char *bad[] = {
        "{\"type\":\"hello\",\"session_id\":\"s\",\"audio_params\":"
            "{\"format\":\"opus\",\"sample_rate\":24000,\"channels\":1,"
            "\"frame_duration\":20}}",
        "{\"type\":\"hello\",\"session_id\":\"s\",\"audio_params\":"
            "{\"format\":\"pcm\",\"sample_rate\":16000,\"channels\":1,"
            "\"frame_duration\":20}}",
        "{\"type\":\"hello\",\"session_id\":\"s\",\"audio_params\":"
            "{\"format\":\"pcm\",\"sample_rate\":24000,\"channels\":2,"
            "\"frame_duration\":20}}",
        "{\"type\":\"hello\",\"session_id\":\"s\",\"audio_params\":"
            "{\"format\":\"pcm\",\"sample_rate\":24000,\"channels\":1,"
            "\"frame_duration\":60}}",
        "{\"type\":\"hello\",\"session_id\":\"s\"}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i)
        assert(pv_parse_server_hello(bad[i], strlen(bad[i]), &h) == PV_HELLO_OK &&
               !pv_server_audio_valid(&h));

    const char *malformed[] = {
        "{\"type\":\"hello\",\"audio_params\":[]}",
        "{\"type\":\"hello\",\"audio_params\":\"pcm\"}",
        "{\"type\":\"hello\",\"audio_params\":{\"format\":\"pcm\"}",
        "{\"type\":\"hello\",\"audio_params\":{\"sample_rate\":\"x\"}}",
    };
    for (size_t i = 0; i < sizeof(malformed) / sizeof(malformed[0]); ++i)
        assert(pv_parse_server_hello(malformed[i], strlen(malformed[i]), &h)
               == PV_HELLO_MALFORMED);

    /* Nested audio fields must not satisfy top-level hello fields. */
    const char *nested =
        "{\"type\":\"hello\",\"audio_params\":{\"version\":9},"
        "\"version\":1,\"session_id\":\"s\"}";
    assert(pv_parse_server_hello(nested, strlen(nested), &h) == PV_HELLO_OK);
    assert(h.version == 1);
}

static void test_tts_state(void)
{
    pv_tts_state_t state = PV_TTS_NONE;
    const char *start = "{\"type\":\"tts\",\"state\":\"start\"}";
    assert(pv_parse_tts_state(start, strlen(start), &state) == PV_TTS_OK);
    assert(state == PV_TTS_START);
    const char *stop = "{ \"state\" : \"stop\" , \"type\" : \"tts\" }";
    assert(pv_parse_tts_state(stop, strlen(stop), &state) == PV_TTS_OK);
    assert(state == PV_TTS_STOP);
    const char *sentence =
        "{\"type\":\"tts\",\"state\":\"sentence_start\",\"text\":\"hi\"}";
    assert(pv_parse_tts_state(sentence, strlen(sentence), &state) == PV_TTS_OK);
    assert(state == PV_TTS_SENTENCE_START);

    const char *other = "{\"type\":\"listen\",\"state\":\"start\"}";
    assert(pv_parse_tts_state(other, strlen(other), &state) == PV_TTS_UNSUPPORTED);

    const char *bad[] = {
        "",
        "[]",
        "{\"type\":\"tts\"}",
        "{\"type\":\"tts\",\"state\":\"paused\"}",
        "{\"type\":\"tts\",\"state\":\"start\"",
        "{\"type\":\"tts\",\"state\":\"start\"} trailing",
        "{\"type\":\"tts\",\"state\":{\"value\":\"start\"}}",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i)
        assert(pv_parse_tts_state(bad[i], strlen(bad[i]), &state) == PV_TTS_MALFORMED);
    char big[PV_WS_MAX_MESSAGE + 2];
    memset(big, ' ', sizeof(big));
    assert(pv_parse_tts_state(big, PV_WS_MAX_MESSAGE + 1, &state) == PV_TTS_MALFORMED);
}

static void test_text_classification(void)
{
    pv_server_hello_t h;
    const char *start = "{\"type\":\"tts\",\"state\":\"start\"}";
    const char *sentence =
        "{\"type\":\"tts\",\"state\":\"sentence_start\",\"text\":\"hi\"}";
    const char *stop = "{\"type\":\"tts\",\"state\":\"stop\"}";
    /* Back-to-back messages are classified independently: no shared slot. */
    assert(pv_ws_classify_text(start, strlen(start), &h) == PV_TEXT_TTS_START);
    assert(pv_ws_classify_text(sentence, strlen(sentence), &h) == PV_TEXT_IGNORE);
    assert(pv_ws_classify_text(stop, strlen(stop), &h) == PV_TEXT_TTS_STOP);

    const char *mcp =
        "{\"type\":\"mcp\",\"payload\":{\"text\":\"a\\\"b\\\\c\\n\\u4F60\"}}";
    assert(pv_ws_classify_text(mcp, strlen(mcp), &h) == PV_TEXT_IGNORE);
    const char *llm =
        "{\"type\":\"llm\",\"emotion\":\"happy\",\"text\":\"line\\n\\uD83D\\uDE00\"}";
    assert(pv_ws_classify_text(llm, strlen(llm), &h) == PV_TEXT_IGNORE);
    /* Malformed non-hello traffic is ignored, not treated as a dead link. */
    const char *broken_mcp = "{\"type\":\"mcp\",\"payload\":{\"text\":\"oops";
    assert(pv_ws_classify_text(broken_mcp, strlen(broken_mcp), &h) == PV_TEXT_IGNORE);
    const char *broken_tts = "{\"type\":\"tts\",\"state\":";
    assert(pv_ws_classify_text(broken_tts, strlen(broken_tts), &h) == PV_TEXT_IGNORE);
    /* Only hello types are fully validated. */
    const char *broken_hello = "{\"type\":\"hello\",\"session_id\":";
    assert(pv_ws_classify_text(broken_hello, strlen(broken_hello), &h)
           == PV_TEXT_HELLO_INVALID);
    const char *nested_type =
        "{\"payload\":{\"type\":\"tts\",\"state\":\"start\"}}";
    assert(pv_ws_classify_text(nested_type, strlen(nested_type), &h)
           == PV_TEXT_IGNORE);

    const char *hello =
        "{\"type\":\"hello\",\"session_id\":\"s\",\"version\":1,"
        "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":24000,"
        "\"channels\":1,\"frame_duration\":20}}";
    assert(pv_ws_classify_text(hello, strlen(hello), &h) == PV_TEXT_HELLO);
    assert(h.version == 1 && pv_server_audio_valid(&h));
}

static void test_transport_event_classification(void)
{
    const pv_ws_event_ids_t ids = {
        .connected = 11, .disconnected = 12, .closed = 13,
        .finish = 14, .error = 15, .data = 16,
    };
    assert(pv_ws_classify_event(11, &ids) == PV_WS_KIND_OPEN);
    assert(pv_ws_classify_event(12, &ids) == PV_WS_KIND_CLOSED);
    assert(pv_ws_classify_event(13, &ids) == PV_WS_KIND_CLOSED); /* clean peer close */
    assert(pv_ws_classify_event(14, &ids) == PV_WS_KIND_CLOSED); /* thread finish */
    assert(pv_ws_classify_event(15, &ids) == PV_WS_KIND_ERROR);
    assert(pv_ws_classify_event(16, &ids) == PV_WS_KIND_DATA);
    assert(pv_ws_classify_event(99, &ids) == PV_WS_KIND_IGNORE);
    assert(pv_ws_classify_event(11, NULL) == PV_WS_KIND_IGNORE);
}

static void test_backoff(void)
{
    assert(pv_ws_backoff_ms(0) == 1000);
    assert(pv_ws_backoff_ms(1) == 2000);
    assert(pv_ws_backoff_ms(2) == 4000);
    assert(pv_ws_backoff_ms(3) == 8000);
    assert(pv_ws_backoff_ms(4) == 16000);
    assert(pv_ws_backoff_ms(5) == 30000);
    assert(pv_ws_backoff_ms(31) == 30000);
}

static void test_hello_validation(void)
{
    pv_ws_model_t m;
    pv_ws_init(&m);
    assert(pv_ws_event(&m, PV_EV_WIFI_ONLINE, 0, 1000));
    unsigned g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_OPEN, g, 2000));

    pv_server_hello_t h = {.version = 1};
    assert(!pv_ws_hello(&m, g, 3000, &h));              /* empty session id */
    strcpy(h.session_id, "s");
    h.version = 0;
    assert(!pv_ws_hello(&m, g, 3000, &h));              /* missing version */
    h.version = 2;
    assert(!pv_ws_hello(&m, g, 3000, &h));              /* wrong version */
    assert(m.state == PV_WS_HANDSHAKING);               /* never falsely Ready */
    h.version = 1;
    assert(pv_ws_hello(&m, g, 3000, &h));
    assert(m.state == PV_WS_READY);
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_CLOSED, g, 4000));
    assert(m.state == PV_WS_RETRY_WAIT);
}

static void test_model(void)
{
    pv_ws_model_t m;
    pv_ws_init(&m);
    assert(m.state == PV_WS_IDLE && m.generation == 0 && m.deadline_us == 0);
    assert(!pv_ws_timeout_due(&m, 1));

    assert(pv_ws_event(&m, PV_EV_WIFI_ONLINE, 0, 1000));
    assert(m.state == PV_WS_CONNECTING && m.generation == 1);
    assert(m.deadline_us == 1000 + PV_WS_CONNECT_TIMEOUT_MS * 1000LL);

    unsigned g = m.generation;
    assert(!pv_ws_event(&m, PV_EV_TRANSPORT_OPEN, g - 1, 2000)); /* stale */
    g = m.generation;
    assert(pv_ws_hello(&m, g, 2000, &(pv_server_hello_t){.version = 1}) == false);
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_OPEN, g, 2000));
    assert(m.state == PV_WS_HANDSHAKING);
    assert(m.deadline_us == 2000 + PV_WS_HELLO_TIMEOUT_MS * 1000LL);

    pv_server_hello_t hello = {.version = 1};
    strcpy(hello.session_id, "session-1");
    assert(pv_ws_hello(&m, g, 3000, &hello));
    assert(m.state == PV_WS_READY && !strcmp(m.session_id, "session-1"));
    assert(m.attempt == 0 && m.deadline_us == 0 && !pv_ws_timeout_due(&m, 9999999));

    /* READY close starts a fresh backoff at the base delay. */
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_CLOSED, g, 4000));
    assert(m.state == PV_WS_RETRY_WAIT && m.attempt == 1);
    assert(m.deadline_us == 4000 + 1000 * 1000LL);
    assert(pv_ws_timeout_due(&m, m.deadline_us));
    g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TIMEOUT, g, m.deadline_us));
    assert(m.state == PV_WS_CONNECTING && m.generation == g + 1);

    /* Consecutive failures double the delay: 1 s, 2 s, 4 s, 8 s, 16 s. */
    g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_ERROR, g, 10000));
    assert(m.state == PV_WS_RETRY_WAIT && m.deadline_us == 10000 + 2000 * 1000LL);
    assert(pv_ws_event(&m, PV_EV_TIMEOUT, m.generation, m.deadline_us));
    g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_ERROR, g, 20000));
    assert(m.state == PV_WS_RETRY_WAIT && m.deadline_us == 20000 + 4000 * 1000LL);
    assert(pv_ws_event(&m, PV_EV_TIMEOUT, m.generation, m.deadline_us));
    g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_OPEN, g, 30000));
    assert(pv_ws_event(&m, PV_EV_HELLO_INVALID, g, 31000));
    assert(m.state == PV_WS_RETRY_WAIT && m.deadline_us == 31000 + 8000 * 1000LL);

    /* A handshake timeout is also a failure. */
    assert(pv_ws_event(&m, PV_EV_TIMEOUT, m.generation, m.deadline_us));
    g = m.generation;
    assert(pv_ws_event(&m, PV_EV_TRANSPORT_OPEN, g, 40000));
    int64_t handshake_timeout = 40000 + PV_WS_HELLO_TIMEOUT_MS * 1000LL;
    assert(pv_ws_event(&m, PV_EV_TIMEOUT, g, handshake_timeout));
    assert(m.state == PV_WS_RETRY_WAIT);
    assert(m.deadline_us == handshake_timeout + 16000 * 1000LL);

    /* Wi-Fi loss cancels timers and resets the backoff history. */
    assert(pv_ws_event(&m, PV_EV_WIFI_OFFLINE, 0, 50000));
    assert(m.state == PV_WS_IDLE && m.attempt == 0 && m.deadline_us == 0);
    assert(!pv_ws_event(&m, PV_EV_TRANSPORT_CLOSED, m.generation, 51000));
    assert(!pv_ws_event(&m, PV_EV_WIFI_OFFLINE, 0, 51000));
    assert(!pv_ws_event(&m, PV_EV_TIMEOUT, m.generation, 51000));
}

int main(void)
{
    test_hello();
    test_endpoint();
    test_parse_hello();
    test_server_audio_params();
    test_tts_state();
    test_text_classification();
    test_transport_event_classification();
    test_backoff();
    test_hello_validation();
    test_model();
    puts("passport websocket protocol tests: PASS");
    return 0;
}
