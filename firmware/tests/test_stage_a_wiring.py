#!/usr/bin/env python3
"""Static wiring contracts for the Stage A application entry and owner."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*\([^;]*?\)\s*\{{", source)
    if not match:
        raise AssertionError(f"function not found: {name}")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"function is unterminated: {name}")


class StageAWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = read("main/main.c")
        cls.app = read("main/passport_app.c")
        cls.cmake = read("main/CMakeLists.txt")
        cls.defaults = read("sdkconfig.defaults")

    def test_button_callback_only_forwards_to_the_application(self) -> None:
        # Button callbacks run on the shared esp_timer task and must stay bounded.
        body = function_body(self.main, "on_key")
        self.assertIn("passport_app_key(btn, ev)", body)
        for banned in ("bsp_lvgl_lock", "nvs_", "esp_wifi_", "httpd_", "vTaskDelay"):
            self.assertNotIn(banned, body)

    def test_ble_stays_disabled_and_audio_is_lazy(self) -> None:
        # Stage A removed BLE; Stage C adds hold-to-talk but must not move
        # audio bring-up into startup, where it would compete with Wi-Fi.
        self.assertNotIn("nimble", self.main)
        self.assertNotIn("CONFIG_BT_ENABLED=y", self.defaults)
        self.assertIn("CONFIG_BT_ENABLED=n", self.defaults)
        self.assertNotIn("bsp_audio_init", self.main)
        self.assertNotIn("bsp_audio_init", function_body(self.app, "owner"))
        startup = function_body(self.app, "passport_app_start")
        self.assertNotIn("bsp_audio_init()", startup)
        self.assertIn("audio_ensure_ready()", function_body(self.app, "audio_press"))

    def test_baseline_demo_sources_are_not_part_of_the_application(self) -> None:
        # The demo test menu and ui_pixel shell stay in the tree for the baseline
        # history but must not be compiled into this application's firmware.
        for banned in ("demo_audio.c", "demo_ble.c", "ui_pixel.c", "demo_navigation.c"):
            self.assertNotIn(banned, self.cmake)
        for source in ("main.c", "passport_app.c", "passport_config.c"):
            self.assertIn(source, self.cmake)

    def test_httpd_stop_is_never_called_from_http_or_event_callbacks(self) -> None:
        # httpd_stop blocks until the HTTP task exits; calling it from a URI
        # handler or an ESP-IDF event callback would deadlock the response.
        for name in ("get_form", "post_form", "network_event", "show", "process"):
            body = function_body(self.app, name)
            self.assertNotIn("httpd_stop", body)
        setup_close = function_body(self.app, "setup_close")
        self.assertIn("httpd_stop", setup_close)
        owner = function_body(self.app, "owner")
        self.assertIn("setup_close()", owner)

    def test_owner_verifies_current_association_before_accepting_an_ip(self) -> None:
        # A late GOT_IP from a previous generation must not approve new
        # credentials: the owner rechecks the live association and IP.
        body = function_body(self.app, "process")
        self.assertIn("e->generation!=s_model.generation", body)
        self.assertIn("esp_wifi_sta_get_ap_info", body)
        self.assertIn("esp_netif_get_ip_info", body)

    def test_passwords_are_never_logged_or_echoed(self) -> None:
        # The form body and password fields must not reach logs or error paths.
        # Log calls must never carry a credential-bearing value; metadata
        # such as blob length/version is fine.
        for banned in ("ESP_LOG.*password", "ESP_LOG.*s_token", "ESP_LOG.*ssid"):
            self.assertNotRegex(self.app, banned)
        self.assertNotIn('ap.password", s_ap_password', self.app)

    def test_owner_initializes_battery_before_networking(self) -> None:
        # Without bsp_battery_init() the fuel gauge never answers and the UI
        # would show "--" forever. It must run before any network allocation.
        body = function_body(self.app, "owner")
        self.assertIn("bsp_battery_init()", body)
        self.assertLess(body.index("bsp_battery_init()"), body.index("network_init()"))

    def test_invalid_stored_config_is_not_a_storage_brick(self) -> None:
        # A stale or corrupt blob is data, not a hardware failure: the device
        # must fall back to first-time setup instead of a dead error screen.
        body = function_body(self.app, "load_saved")
        self.assertNotIn("ESP_ERR_INVALID_VERSION", body)
        self.assertIn("ESP_LOGW", body)
        self.assertIn("return ESP_OK", body)

    def test_form_submission_is_rejected_when_owner_is_busy(self) -> None:
        # The handler must answer 409 for busy states itself instead of
        # acknowledging a submission the owner would silently drop.
        body = function_body(self.app, "post_form")
        self.assertIn("atomic_load(&s_form_open)", body)
        self.assertIn("409 Conflict", body)
        self.assertIn("sync_form_open", function_body(self.app, "owner"))

    def test_non_setup_retry_does_not_rebroadcast_the_stale_ap(self) -> None:
        # Switching a pure-STA retry to AP mode would broadcast the previous
        # session's AP SSID/password; the owner must stop the stack instead.
        body = function_body(self.app, "request_attempt")
        self.assertIn("esp_wifi_stop()", body)
        self.assertIn("esp_wifi_set_mode(WIFI_MODE_AP)", body)
        self.assertLess(body.index("if(s_setup)"), body.index("esp_wifi_stop()"))

    def test_websocket_callback_only_enqueues(self) -> None:
        # The WebSocket event task must not touch LVGL, NVS, or the network
        # synchronously; it copies a bounded chunk and enqueues one event.
        body = function_body(self.app, "ws_event")
        self.assertIn("xQueueSend", body)
        for banned in ("bsp_lvgl_lock", "nvs_", "esp_websocket_client_send",
                       "httpd_", "vTaskDelay"):
            self.assertNotIn(banned, body)

    def test_hello_is_sent_by_the_owner_and_parsed_in_the_callback(self) -> None:
        body = function_body(self.app, "ws_handle")
        self.assertIn("pv_ws_build_hello", body)
        self.assertIn("ws_send_text_locked", body)
        self.assertNotIn("bsp_lvgl_lock", body)  # show_online takes its own lock
        send = function_body(self.app, "ws_send_text_locked")
        self.assertIn("esp_websocket_client_send_text", send)
        self.assertIn("s_ws_send_lock", send)
        callback = function_body(self.app, "ws_event")
        self.assertIn("pv_ws_classify_text", callback)
        self.assertIn("e.kind = EV_TEXT", callback)
        owner = function_body(self.app, "process")
        self.assertIn("pv_ws_hello", owner)
        # The owner must never read the WS callback's reassembly slot.
        self.assertNotIn("s_ws_rx", owner)
        self.assertNotIn("s_ws_rx", function_body(self.app, "ws_handle"))

    def test_server_hello_requires_version_one_and_a_session_id(self) -> None:
        body = function_body(read("main/passport_ws.c"), "pv_ws_hello")
        self.assertIn("hello->version != 1", body)
        self.assertIn("hello->session_id[0] == 0", body)
        handle = function_body(self.app, "process")
        self.assertIn("PV_EV_HELLO_INVALID", handle)

    def test_stale_websocket_events_use_a_monotonic_client_generation(self) -> None:
        # A raw client pointer identity can be reused by a later allocation;
        # the callback and owner must compare a monotonic generation instead.
        self.assertNotIn("e.ws_client = s_ws_client", self.app)
        self.assertIn("s_ws_client_generation", function_body(self.app, "ws_event"))
        self.assertIn("e->ws_client_generation != atomic_load(&s_ws_client_generation)",
                      function_body(self.app, "ws_handle"))

    def test_websocket_uri_build_checks_truncation(self) -> None:
        body = function_body(self.app, "ws_start")
        self.assertIn("(size_t)n >= sizeof(s_ws_uri)", body)
        self.assertIn("ESP_ERR_INVALID_SIZE", body)

    def test_stage_c_captures_16khz_mono_and_sends_binary_frames(self) -> None:
        ensure = function_body(self.app, "audio_ensure_ready")
        self.assertIn("bsp_audio_init()", ensure)
        self.assertIn("bsp_audio_set_volume(s_volume)", ensure)
        self.assertLess(ensure.index("bsp_audio_set_volume(s_volume)"),
                        ensure.index("audio_set_format_locked(16000)"))
        self.assertIn("audio_set_format_locked(16000)", ensure)
        self.assertIn("PV_AUDIO_QUEUE_DEPTH", ensure)
        capture = function_body(self.app, "capture_task")
        self.assertIn("bsp_audio_read", capture)
        self.assertIn("xQueueSend(s_pcm_queue, &frame, 0)", capture)
        send = function_body(self.app, "send_task")
        self.assertIn("ws_send_bin_locked", send)
        binary = function_body(self.app, "ws_send_bin_locked")
        self.assertIn("esp_websocket_client_send_bin", binary)

    def test_websocket_180_clean_close_is_mapped_to_closed(self) -> None:
        # esp_websocket_client 1.8.0 emits CLOSED (not DISCONNECTED) when the
        # peer closes cleanly; ignoring it would stay READY and never retry.
        self.assertIn('esp_websocket_client: "^1.8.0"',
                      read("main/idf_component.yml"))
        body = function_body(self.app, "ws_event")
        self.assertIn("WEBSOCKET_EVENT_CLOSED", body)
        self.assertIn("WEBSOCKET_EVENT_FINISH", body)
        self.assertIn("pv_ws_classify_event", body)
        self.assertIn("PV_WS_KIND_CLOSED", body)
        protocol = read("main/passport_ws.c")
        classify = function_body(protocol, "pv_ws_classify_event")
        self.assertIn("ids->closed", classify)
        self.assertIn("ids->finish", classify)

    def test_send_success_requires_exact_byte_count(self) -> None:
        for name in ("ws_send_text_locked", "ws_send_bin_locked"):
            self.assertIn("pv_audio_send_ok(sent, len)", function_body(self.app, name))
        audio = function_body(read("main/passport_audio.c"), "pv_audio_send_ok")
        self.assertIn("(size_t)sent == len", audio)

    def test_audio_stop_completion_returns_to_idle(self) -> None:
        body = function_body(self.app, "audio_stop_complete")
        self.assertIn('audio_send_listen("stop")', body)
        self.assertIn("PV_AUDIO_STOPPED", body)
        self.assertIn("atomic_store(&s_stop_sent, true)", body)
        state = function_body(read("main/passport_audio.c"), "pv_audio_event")
        self.assertIn("case PV_AUDIO_STOPPED", state)
        self.assertIn("m->state = PV_AUDIO_IDLE", state)

    def test_critical_events_survive_a_full_queue(self) -> None:
        # Release, stop completion, and disconnect are latched before the
        # best-effort queue send and polled by the owner.
        key = function_body(self.app, "passport_app_key")
        self.assertIn("PV_AUDIO_PENDING_RELEASE", key)
        send = function_body(self.app, "send_task")
        self.assertIn("PV_AUDIO_PENDING_STOP_SENT", send)
        event = function_body(self.app, "ws_event")
        self.assertIn("PV_AUDIO_PENDING_WS_LOST", event)
        owner = function_body(self.app, "owner")
        for flag in ("PV_AUDIO_PENDING_WS_LOST", "PV_AUDIO_PENDING_STOP_SENT",
                     "PV_AUDIO_PENDING_RELEASE"):
            self.assertIn(f"pv_audio_take_pending(&s_pending,{flag})", owner)

    def test_clean_close_and_finish_have_behavioral_host_coverage(self) -> None:
        # The mapping itself is a pure function with a host test; this keeps
        # the test wired into the gate as well as in the source.
        tests = read("tests/test_passport_ws.c")
        self.assertIn("test_transport_event_classification", tests)
        self.assertIn("PV_WS_KIND_CLOSED", tests)
        audio_tests = read("tests/test_passport_audio.c")
        self.assertIn("test_critical_pending_burst", audio_tests)

    def test_binary_downlink_never_reaches_the_json_parser(self) -> None:
        body = function_body(self.app, "ws_event")
        binary = body[body.index("PV_WS_KIND_DATA"):]
        self.assertIn("op_code == 0x2", binary)
        self.assertIn("ws_binary_chunk(d)", binary)
        self.assertLess(binary.index("op_code == 0x2"),
                        binary.index("op_code != 0x1"))
        helper = function_body(self.app, "ws_binary_chunk")
        self.assertNotIn("pv_parse_server_hello", helper)
        self.assertNotIn("pv_parse_tts_state", helper)

    def test_downlink_audio_params_are_validated_before_playback(self) -> None:
        body = function_body(self.app, "process")
        self.assertIn("pv_server_audio_valid(&e->hello)", body)
        self.assertIn("PV_EV_HELLO_INVALID", body)
        self.assertIn("atomic_store(&s_down_audio_ok,false)", body)
        validate = function_body(read("main/passport_ws.c"),
                                 "pv_server_audio_valid")
        for needle in ("\"pcm\"", "PV_DOWN_SAMPLE_RATE", "channels == 1",
                       "PV_DOWN_FRAME_DURATION_MS"):
            self.assertIn(needle, validate)

    def test_downlink_is_framed_bounded_and_played_by_an_independent_task(self) -> None:
        helper = function_body(self.app, "ws_binary_chunk")
        self.assertIn("pv_down_rx_append", helper)
        self.assertIn("xQueueSend(s_down_queue, &frame, 0)", helper)
        self.assertIn("pv_audio_queue_frame", helper)
        play = function_body(self.app, "play_task")
        self.assertIn("bsp_audio_write", play)
        self.assertIn("s_audio_io_lock", play)
        self.assertIn("PV_DOWN_DRAIN_GRACE_MS", play)
        self.assertIn("AUDIO_PLAY_DONE", play)
        self.assertIn("AUDIO_PLAY_FAILED", play)
        ensure = function_body(self.app, "audio_ensure_ready")
        self.assertIn("play_task", ensure)
        startup = function_body(self.app, "passport_app_start")
        self.assertIn("PV_DOWN_QUEUE_DEPTH", startup)

    def test_downlink_switches_24k_and_restores_16k_under_the_io_lock(self) -> None:
        start = function_body(self.app, "audio_downlink_start")
        self.assertIn("PV_DOWN_SAMPLE_RATE", start)
        self.assertIn("audio_runtime_reset()", start)
        self.assertIn("audio_set_format_locked", start)
        finish = function_body(self.app, "audio_downlink_finish")
        self.assertIn("audio_set_format_locked(16000)", finish)
        reset = function_body(self.app, "audio_downlink_reset")
        self.assertIn("audio_set_format_locked(16000)", reset)
        switch = function_body(self.app, "audio_set_format_locked")
        self.assertIn("bsp_audio_set_format(hz, 16, 1)", switch)
        capture = function_body(self.app, "capture_task")
        self.assertIn("s_audio_io_lock", capture)
        self.assertIn("bsp_audio_read", capture)

    def test_tts_states_drive_speaking_and_half_duplex(self) -> None:
        callback = function_body(self.app, "ws_event")
        self.assertIn("pv_ws_classify_text", callback)
        self.assertIn("PV_AUDIO_PENDING_TTS_START", callback)
        self.assertIn("PV_AUDIO_PENDING_TTS_STOP", callback)
        self.assertIn("pv_down_rx_arm", callback)
        self.assertIn("pv_down_rx_disarm", callback)
        handle = function_body(self.app, "process")
        self.assertIn("PV_TEXT_TTS_START", handle)
        self.assertIn("PV_TEXT_TTS_STOP", handle)
        self.assertIn("audio_downlink_start()", handle)
        self.assertIn("audio_downlink_stop()", handle)
        press = function_body(self.app, "audio_press")
        self.assertIn("pv_down_can_listen(&s_down)", press)
        ui = function_body(self.app, "show_online")
        self.assertIn('"Speaking..."', ui)
        stop = function_body(self.app, "audio_downlink_stop")
        self.assertIn("PV_DOWN_EV_STOP", stop)
        self.assertIn("s_down_stop_pending", stop)
        finish = function_body(self.app, "audio_downlink_finish")
        self.assertIn("PV_DOWN_EV_DRAINED", finish)

    def test_downlink_resets_on_loss_and_playback_error(self) -> None:
        lost = function_body(self.app, "audio_ws_lost")
        self.assertIn("audio_downlink_reset(false)", lost)
        self.assertIn("s_down_audio_ok, false", lost)
        process = function_body(self.app, "process")
        self.assertIn("AUDIO_PLAY_FAILED", process)
        self.assertIn("audio_downlink_finish(true)", process)
        broadcast = function_body(self.app, "owner")
        self.assertIn("audio_ws_lost();ws_destroy();", broadcast)

    def test_stage_c_rejects_press_until_the_websocket_is_ready(self) -> None:
        press = function_body(self.app, "audio_press")
        self.assertIn("s_ws.state != PV_WS_READY", press)
        self.assertIn("!atomic_load(&s_stop_sent)", press)
        self.assertIn("PV_AUDIO_PRESS", press)
        # listen.start precedes capture in the owner, never in a callback.
        self.assertLess(press.index("audio_send_listen(\"start\")"),
                        press.index("s_capture_run"))
        release = function_body(self.app, "audio_release")
        self.assertIn("PV_AUDIO_RELEASE", release)
        self.assertIn("s_capture_run", release)
        self.assertLess(release.index("s_capture_run"),
                        release.index("s_stop_pending"))
        self.assertIn("atomic_store(&s_stop_sent, false)", release)
        send = function_body(self.app, "send_task")
        self.assertIn("uxQueueMessagesWaiting(s_pcm_queue) == 0", send)
        self.assertIn("AUDIO_STOP_SENT", send)
        process = function_body(self.app, "process")
        self.assertIn("audio_stop_complete()", process)
        self.assertIn('audio_send_listen("stop")',
                      function_body(self.app, "audio_stop_complete"))

    def test_stage_c_stops_recording_on_websocket_or_wifi_loss(self) -> None:
        body = function_body(self.app, "process")
        down = body[body.index("e->kind==EV_DOWN"):]
        self.assertIn("audio_ws_lost()", down)
        closed = function_body(self.app, "ws_handle")
        self.assertIn("audio_ws_sync()", closed)
        restart = function_body(self.app, "ws_restart")
        self.assertIn("audio_ws_lost()", restart)

    def test_stage_c_queue_is_bounded_and_never_blocks_network(self) -> None:
        # A full PCM queue evicts the oldest frame instead of blocking the
        # capture path; the send task owns all network writes.
        capture = function_body(self.app, "capture_task")
        self.assertIn("xQueueSend(s_pcm_queue, &frame, 0)", capture)
        self.assertIn("pv_audio_queue_frame", capture)
        self.assertNotIn("xQueueSend(s_pcm_queue, &frame, pdMS_TO_TICKS", capture)
        send = function_body(self.app, "send_task")
        self.assertNotIn("s_audio.", send)  # owner-owned model stays owner-only
        self.assertNotIn("s_ws.", send)

    def test_back_to_back_text_is_self_contained_and_sticky(self) -> None:
        callback = function_body(self.app, "ws_event")
        self.assertIn("pv_ws_classify_text(s_ws_rx, total, &hello)", callback)
        self.assertIn("PV_TEXT_TTS_START", callback)
        self.assertIn("PV_TEXT_TTS_STOP", callback)
        self.assertIn("pv_audio_note_pending", callback)
        owner = function_body(self.app, "owner")
        for flag in ("PV_AUDIO_PENDING_TTS_START", "PV_AUDIO_PENDING_TTS_STOP"):
            self.assertIn(f"pv_audio_take_pending(&s_pending,{flag})", owner)
        # Behavior: separate classify calls keep the start/sentence/stop order.
        tests = read("tests/test_passport_ws.c")
        self.assertIn("test_text_classification", tests)
        self.assertIn("Back-to-back messages are classified independently", tests)
        self.assertIn("broken_mcp", tests)  # malformed non-hello is ignored
        self.assertIn("emoji", tests)       # \uXXXX and surrogate pairs

    def test_playback_completion_uses_sticky_pending_bits(self) -> None:
        play = function_body(self.app, "play_task")
        for flag in ("PV_AUDIO_PENDING_PLAY_DONE", "PV_AUDIO_PENDING_PLAY_FAILED"):
            self.assertIn(flag, play)
        owner = function_body(self.app, "owner")
        for flag in ("PV_AUDIO_PENDING_PLAY_DONE", "PV_AUDIO_PENDING_PLAY_FAILED"):
            self.assertIn(f"pv_audio_take_pending(&s_pending,{flag})", owner)
        audio = read("tests/test_passport_audio.c")
        self.assertIn("test_critical_pending_burst", audio)
        self.assertIn("PV_AUDIO_PENDING_PLAY_DONE", audio)

    def test_binary_is_accepted_only_while_armed(self) -> None:
        helper = function_body(self.app, "ws_binary_chunk")
        self.assertIn("pv_down_rx_append(&s_down_rx", helper)
        callback = function_body(self.app, "ws_event")
        self.assertIn("pv_down_rx_arm(&s_down_rx)", callback)
        self.assertIn("pv_down_rx_disarm(&s_down_rx)", callback)
        down = read("tests/test_passport_downlink.c")
        self.assertIn("test_binary_rx_gate", down)
        self.assertIn("Idle: binary frames are ignored", down)

    def test_downlink_queue_exists_before_audio_init(self) -> None:
        startup = function_body(self.app, "passport_app_start")
        self.assertIn("s_down_queue=xQueueCreate(PV_DOWN_QUEUE_DEPTH", startup)
        ensure = function_body(self.app, "audio_ensure_ready")
        self.assertNotIn("s_down_queue = xQueueCreate", ensure)

    def test_format_switch_failure_clears_downlink_state(self) -> None:
        start = function_body(self.app, "audio_downlink_start")
        failure = start[start.index("!audio_set_format_locked(PV_DOWN_SAMPLE_RATE)"):]
        self.assertIn("audio_downlink_clear()", failure)
        self.assertIn("audio_uplink_idle()", failure)
        state = function_body(read("main/passport_downlink.c"), "pv_down_event")
        self.assertIn("PV_DOWN_EV_ERROR", state)
        self.assertIn("PV_DOWN_ACT_RESET", state)

    def test_duplicate_start_only_clears_a_draining_segment(self) -> None:
        start = function_body(self.app, "audio_downlink_start")
        self.assertIn("s_down.state == PV_DOWN_DRAINING", start)
        self.assertNotIn("pv_down_speaking(&s_down)) audio_downlink_clear", start)
        self.assertIn('audio_send_listen("stop")', start)  # reset gateway manual window

    def test_ws_callback_resources_and_continuations(self) -> None:
        ws = function_body(self.app, "ws_start")
        self.assertIn("cfg.task_stack = 6144", ws)
        callback = function_body(self.app, "ws_event")
        self.assertIn("ws_log_stack_watermark()", callback)
        self.assertIn("high-water",
                      function_body(self.app, "ws_log_stack_watermark"))
        self.assertIn("d->op_code != 0x0", callback)   # continuation
        helper = function_body(self.app, "ws_binary_chunk")
        self.assertIn("downlink queue full: dropped", helper)
        self.assertIn("dropped % 50", helper)

    def test_wifi_loss_survives_full_event_queue(self) -> None:
        import os
        import subprocess
        import tempfile
        source = r'''
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <assert.h>
typedef int esp_event_base_t;
typedef struct { int ip_info; } ip_event_got_ip_t;
typedef struct { unsigned char ssid[32],bssid[6]; int ssid_len; } wifi_event_sta_connected_t;
typedef struct { unsigned generation; int kind,ssid_len,ip; unsigned char ssid[32],bssid[6]; } app_event_t;
enum {WIFI_EVENT=1,IP_EVENT,WIFI_EVENT_STA_STOP,WIFI_EVENT_STA_DISCONNECTED,WIFI_EVENT_STA_CONNECTED,IP_EVENT_STA_GOT_IP,EV_STOP,EV_DOWN,EV_LINK,EV_IP};
static atomic_uint s_generation,s_wifi_loss;
static int s_queue, attempted;
static int xQueueSend(int q,app_event_t *e,int wait){(void)q;(void)e;(void)wait;++attempted;return 0;}
'''
        source += 'static void network_event(void *arg,esp_event_base_t base,int32_t id,void *data){' + function_body(self.app,'network_event') + '}\n'
        source += r'''
int main(void){
 atomic_store(&s_generation,7);
 network_event(0,WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,0);
 assert(attempted==1 && atomic_load(&s_wifi_loss)==8);
 unsigned expected=8;
 assert(atomic_compare_exchange_strong(&s_wifi_loss,&expected,0));
 expected=8;assert(!atomic_compare_exchange_strong(&s_wifi_loss,&expected,0));
 atomic_store(&s_generation,8);
 network_event(0,WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,0);
 expected=8;assert(!atomic_compare_exchange_strong(&s_wifi_loss,&expected,0));
 assert(atomic_load(&s_wifi_loss)==9);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'loss.c';path.write_text(source)
            binary=Path(directory)/'loss'
            subprocess.run([os.getenv('CC','cc'),'-std=c11',str(path),'-o',str(binary)],check=True,capture_output=True)
            subprocess.run([str(binary)],check=True)
        owner=function_body(self.app,'owner')
        self.assertIn('loss-1==s_model.generation',owner)
        self.assertIn('process(&down)',owner)
        self.assertIn('s_wifi_retry_at=esp_timer_get_time()',function_body(self.app,'failed'))

    def test_stage_e_volume_persistence_keeps_config_v1(self) -> None:
        load = function_body(self.app, 'load_saved')
        self.assertIn('nvs_get_u8(h,"volume"', load)
        self.assertIn('nvs_get_blob(h,"config_v1"', load)
        owner = function_body(self.app, 'owner')
        self.assertIn('pv_volume_save_due', owner)
        self.assertIn('nvs_set_u8(h,"volume"', owner)
        self.assertIn('s_wait_reply || pv_down_speaking', owner)
        process = function_body(self.app, 'process')
        self.assertIn('s_up_long)return', process)
        self.assertIn('s_wifi_retry_at=0', owner)
        self.assertIn('s_backend!=1', process)
        self.assertIn('20000000', owner)
        self.assertIn('s_voice_notice', owner)
        self.assertIn('s_voice_available', self.app)
        self.assertIn('s_voice_available', function_body(self.app,'ws_event'))
        self.assertIn('s_voice_available', function_body(self.app,'ws_destroy'))
        self.assertIn('PV_TEXT_TTS_START && !atomic_load(&s_voice_available)', function_body(self.app,'ws_event'))
        self.assertIn('s_backend!=1 || !atomic_load(&s_down_audio_ok)', function_body(self.app,'audio_downlink_start'))

    def test_gateway_is_only_used_by_the_websocket_client(self) -> None:
        # Stage B stores and opens the gateway as a WebSocket URI; it must not
        # add HTTP polling to the gateway.
        self.assertNotIn("esp_http_client", self.app)
        self.assertIn("esp_websocket_client_config_t", self.app)
        self.assertIn("cfg.uri = s_ws_uri", self.app)

    def test_face_logic_stays_out_of_the_renderer(self) -> None:
        # Stage F: animation decisions are pure logic; the app only maps them
        # onto LVGL objects. The face module must not touch LVGL or audio.
        face = read("main/passport_face.c") + read("main/passport_face.h")
        self.assertNotIn("lvgl", face)
        self.assertNotIn("bsp_", face)
        self.assertIn('"passport_face.c"', self.cmake)
        self.assertIn("pv_face_tick", self.app)
        self.assertIn("s_mouth_level", function_body(self.app, "play_task"))

    def test_ok_during_playback_cancels_locally_and_upstream(self) -> None:
        # Stage G: pressing OK while the reply plays stops playback at once and
        # tells the gateway to cancel, so the tail cannot leak into next turn.
        press = function_body(self.app, "process")
        self.assertIn("audio_playback_cancel();return;", press)
        cancel = function_body(self.app, "audio_playback_cancel")
        self.assertIn("audio_downlink_clear()", cancel)
        self.assertIn('"{\\"type\\":\\"abort\\"}"', cancel)
        self.assertNotIn("audio_press", cancel)

    def test_gateway_token_goes_to_the_header_only(self) -> None:
        # Stage G: the token rides the WebSocket handshake and is never logged.
        start = function_body(self.app, "ws_start")
        self.assertIn("Authorization: Bearer", start)
        self.assertIn("cfg.headers", start)
        for line in start.splitlines():
            if "ESP_LOG" in line:
                self.assertNotIn("auth_token", line)
        self.assertIn("pv_config_from_blob", function_body(self.app, "load_saved"))
        self.assertIn('name=auth', function_body(self.app, "get_form"))

    def test_boot_diagnostic_is_stored_not_logged(self) -> None:
        # Stage G: an abnormal restart is remembered in its own NVS key and
        # shown on every subsequent boot until a key hides it for that boot.
        # The text must not be logged; the stored config blob is untouched.
        diag = read("main/passport_diag.c") + read("main/passport_diag.h")
        self.assertNotIn("lvgl", diag)
        self.assertNotIn("bsp_", diag)
        self.assertNotIn("nvs", diag)
        self.assertIn('"passport_diag.c"', self.cmake)
        owner = function_body(self.app, "owner")
        self.assertIn('nvs_set_blob(h,"diag"', owner)
        self.assertIn("pv_diag_abnormal", owner)
        for line in owner.splitlines():
            if "ESP_LOG" in line:
                self.assertNotIn("s_diag_text", line)
        self.assertIn('nvs_get_blob(h,"diag"', function_body(self.app, "load_saved"))
        self.assertNotIn("diag", function_body(self.app, "save_active"))


class StageGBehaviorTest(unittest.TestCase):
    def run_c(self, source, *modules):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'diag.c'
            path.write_text(source)
            binary = Path(directory) / 'diag'
            subprocess.run([os.getenv('CC', 'cc'), '-std=c11', '-I' + str(ROOT / 'main'),
                            str(path), *(str(ROOT / m) for m in modules),
                            '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_first_key_refreshes_details_before_early_returns(self):
        app = read('main/passport_app.c')
        # Compile the actual key branch and details renderer with recording UI
        # doubles, as in the existing network-event host test above.
        renderer = function_body(app, 'refresh_details')
        branch = function_body(app, 'process')
        branch = branch[branch.index('    if(e->kind==EV_KEY)'):branch.index('    if(e->kind==EV_FORM)')]
        source = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include "passport_config.h"
#include "passport_ws.h"
#include "passport_downlink.h"
enum {EV_KEY, BSP_BTN_UP, BSP_BTN_DOWN, BSP_BTN_OK, BSP_BTN_PRESS, BSP_BTN_LONG, BSP_BTN_CLICK, BSP_BTN_RELEASE, ESP_OK, PV_AUDIO_PENDING_RELEASE};
typedef struct {int kind,key,key_event;} app_event_t;
static bool s_setup,s_diag_shown,s_up_long,s_volume_dirty,s_bsp_audio_inited,s_attempt_pending,s_close_pending,s_sta_enabled,s_barrier;
static atomic_bool s_form_pending;
static atomic_uint s_notify_id;
static int s_backend=1,s_audio_io_lock,s_deadline,s_last_input,s_volume_changed;
static unsigned char s_volume=50;
static pv_model_t s_model;
static pv_config_t s_saved,s_active;
static pv_ws_model_t s_ws;
static pv_down_model_t s_down;
static int s_pending,locked,writes,presses,releases,cancels;
static char detail[256],s_diag_text[64]="diagnostic",s_ip_text[24]="192.0.2.1",s_ap_name[25]="AP",s_ap_password[17]="password";
static int s_details;
static void lv_label_set_text(int o,const char *t){(void)o;assert(locked);++writes;snprintf(detail,sizeof(detail),"%s",t);}
static void lv_label_set_text_fmt(int o,const char *fmt,...){char b[256];va_list a;va_start(a,fmt);vsnprintf(b,sizeof(b),fmt,a);va_end(a);lv_label_set_text(o,b);}
static int bsp_lvgl_lock(int ms){(void)ms;assert(!locked);locked=1;return 1;}
static void bsp_lvgl_unlock(void){assert(locked);locked=0;}
static int esp_timer_get_time(void){return 123;}
static void bsp_display_backlight(int v){(void)v;}
#define pdMS_TO_TICKS(x) (x)
#define pdTRUE 1
#define WIFI_MODE_AP 1
static int xSemaphoreTake(int a,int b){(void)a;(void)b;return 1;}
static void xSemaphoreGive(int a){(void)a;}
static void bsp_audio_set_volume(int a){(void)a;}
static void esp_wifi_set_mode(int a){(void)a;}
static void show_online(void){}
static void show(const char*a,const char*b){(void)a;(void)b;}
static void audio_press(void){++presses;}
static void audio_release(void){++releases;}
static void audio_playback_cancel(void){++cancels;}
static void request_attempt(const pv_config_t*c,bool b){(void)c;(void)b;}
static void ws_restart(void){}
static int pv_audio_take_pending(int*p,int f){(void)p;(void)f;return 0;}
static void save_and_finish(void){}
static int ap_start(void){return 0;}
static int setup_close(void){return 0;}
static void failed(const char*t){(void)t;}
'''
        source += 'static void refresh_details(void){' + renderer + '}\n'
        source += 'static void key(const app_event_t *e){' + branch + '}\n'
        source += r'''
static void check(int button,int event){
 s_diag_shown=true;strcpy(detail,s_diag_text);writes=0;
 app_event_t e={EV_KEY,button,event};key(&e);
 assert(!s_diag_shown && !locked && writes==1);
 assert(!strcmp(detail,"192.0.2.1\nSession session"));
 assert(s_model.state==PV_ONLINE && s_volume==50);
}
int main(void){
 s_ws.state=PV_WS_READY;strcpy(s_ws.session_id,"session");s_model.state=PV_ONLINE;
 check(BSP_BTN_DOWN,BSP_BTN_PRESS); /* no later show */
 s_up_long=true;check(BSP_BTN_UP,BSP_BTN_CLICK); /* immediate return */
 check(BSP_BTN_OK,BSP_BTN_RELEASE);assert(releases==0);
 check(BSP_BTN_OK,BSP_BTN_PRESS);assert(presses==1);
 s_down.state=PV_DOWN_SPEAKING;check(BSP_BTN_OK,BSP_BTN_PRESS);assert(cancels==1);
 s_setup=true;s_diag_shown=true;app_event_t e={EV_KEY,BSP_BTN_DOWN,BSP_BTN_PRESS};key(&e);
 assert(strstr(detail,"Join Wi-Fi\nAP\nPassword\npassword") && !s_diag_shown);
}
'''
        self.run_c(source, 'main/passport_config.c')

    def test_notify_owner_lease_cancel_and_completion(self):
        app = read('main/passport_app.c')
        body = function_body(app, 'process')
        start = body.index('        if(e->text==PV_TEXT_NOTIFY_START ||')
        end = body.index('        } else if(e->text==PV_TEXT_TTS_START)', start)
        branch = body[start:end] + '        }'
        source = r'''
#include <assert.h>
#include <stdio.h>
#include <stdatomic.h>
#include "passport_ws.h"
#include "passport_audio.h"
#include "passport_downlink.h"
static atomic_uint s_notify_id;
static atomic_bool s_notify_active,s_notify_flash,s_stop_sent;
static atomic_uchar s_notify_blink;
static long long s_notify_deadline,s_last_input,s_notify_idle_at;
static bool s_setup,s_wait_reply;
static int s_backend=1,shown,cancelled,acks;
static pv_ws_model_t s_ws;
static pv_audio_model_t s_audio;
static pv_down_model_t s_down;
#define NOTIFY_BLINK_TICKS 25
static void notify_ack(unsigned id,const char*state){assert(id);++acks;(void)state;}
static int ws_send_text_locked(const char*p,size_t n){(void)p;(void)n;return 1;}
static void show_online(void){++shown;}
'''
        source += 'static void notify_clear(const char *outcome){' + function_body(app, 'notify_clear') + '}\n'
        source += 'static void audio_playback_cancel(void){++cancelled;notify_clear("cancelled");}\n'
        source += 'typedef struct {int text;unsigned notify_id;bool notify_flash,notify_cancel;} event;\n'
        source += 'static void apply(event *e,long long now){' + branch + '}\n'
        source += r'''
int main(void){
 s_ws.state=PV_WS_READY;s_audio.state=PV_AUDIO_IDLE;atomic_store(&s_stop_sent,true);
 event e={PV_TEXT_NOTIFY_START,7,true,false};
 s_wait_reply=true;apply(&e,20000000);assert(!s_notify_id); /* >10s still busy */
 s_wait_reply=false;apply(&e,20000000);assert(s_notify_id==7 && s_notify_active && s_notify_blink==25);
 e.text=PV_TEXT_NOTIFY_STOP;apply(&e,20000001);assert(s_notify_id==7 && s_notify_active); /* cloud stop */
 e.notify_id=6;e.notify_cancel=true;apply(&e,20000002);assert(!cancelled);
 e.notify_id=7;apply(&e,20000003);assert(cancelled==1 && !s_notify_id && !s_notify_active && !s_notify_blink);
 e=(event){PV_TEXT_NOTIFY_START,8,false,false};apply(&e,22000000);assert(s_notify_id==8);
 notify_clear("done");assert(!s_notify_id && !s_notify_active);
 e.notify_id=9;apply(&e,24000000);notify_clear(NULL);assert(!s_notify_id && !s_notify_blink); /* disconnect */
 s_setup=true;e.notify_id=10;apply(&e,26000000);assert(!s_notify_id);
}
'''
        self.run_c(source, 'main/passport_downlink.c')

    def test_notice_blink_restores_hidden_status_without_face(self):
        app = read('main/passport_app.c')
        body = function_body(app, 'face_timer_cb')
        block = body[body.index('    if (s_status)'):body.index('    if (!s_face_box')]
        source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdatomic.h>
static int s_status=1;
static bool hidden;
static atomic_uchar s_notify_blink;
#define LV_OBJ_FLAG_HIDDEN 1
static void lv_obj_add_flag(int o,int f){(void)o;(void)f;hidden=true;}
static void lv_obj_remove_flag(int o,int f){(void)o;(void)f;hidden=false;}
static bool lv_obj_has_flag(int o,int f){(void)o;(void)f;return hidden;}
'''
        source += 'static void tick(void){' + block + '}\n'
        source += 'int main(void){s_notify_blink=25;for(int i=0;i<26;i++)tick();assert(!hidden);hidden=true;s_notify_blink=0;tick();assert(!hidden);}'
        self.run_c(source)

    def test_diagnostic_fits_actual_montserrat14(self):
        font_path = ROOT / 'managed_components/lvgl__lvgl/src/font/lv_font_montserrat_14.c'
        if not font_path.exists():
            self.skipTest('actual Managed Component font requires existing firmware dependencies')
        font = font_path.read_text()
        advances = re.findall(r'\.adv_w = (\d+)', font)
        def array(name):
            return re.search(r'\b' + name + r'\[\]\s*=\s*\{(.*?)\};', font, re.S).group(1)
        right_count = re.search(r'\.right_class_cnt\s*=\s*(\d+)', font).group(1)
        height = int(re.search(r'\.line_height = (\d+)', font).group(1))
        self.assertLessEqual(2 * height + 2, 34)  # default theme line spacing
        source = '#include <assert.h>\n#include <string.h>\n#include "passport_diag.h"\n'
        source += 'static const int adv[]={' + ','.join(advances) + '};\n'
        for name in ('kern_left_class_mapping', 'kern_right_class_mapping', 'kern_class_values'):
            source += 'static const int ' + name + '[]={' + array(name) + '};\n'
        source += r'''
static void fits(int reason,int mv){
 char b[PV_DIAG_TEXT_MAX];assert(pv_diag_format(b,sizeof(b),reason,mv));
 int width=0,lines=1;
 for(unsigned i=0;b[i];++i){
  if(b[i]=='\n'){assert(width<=204);width=0;++lines;continue;}
  assert(b[i]>=32 && b[i]<=126);
  int g=b[i]-31,next=(b[i+1]>=32 && b[i+1]<=126)?b[i+1]-31:0;
  int l=kern_left_class_mapping[g],r=kern_right_class_mapping[next];
  int kern=(l && r)?kern_class_values[(l-1)*RIGHT_COUNT+r-1]:0;
  width+=(adv[g]+kern+8)>>4;
 }
 assert(width<=204 && lines==2);
}
int main(void){
 for(int r=0;r<=65535;++r){fits(r,-1);fits(r,3310);fits(r,65534);}
 for(int mv=0;mv<=65534;++mv)fits(5,mv);
 fits(-2147483647-1,2147483647);fits(2147483647,-1);
}
'''.replace('RIGHT_COUNT', right_count)
        self.run_c(source, 'main/passport_diag.c')

    def test_boot_storage_order_and_failure_isolation(self):
        app = read('main/passport_app.c')
        boot = function_body(app, 'owner').split('    if(network_init()')[0]
        source = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "passport_config.h"
#include "passport_diag.h"
typedef int esp_err_t;
typedef int nvs_handle_t;
enum {ESP_OK,ESP_ERR_NVS_NOT_FOUND,ESP_ERR_INVALID_STATE,NVS_READONLY,NVS_READWRITE};
#define ESP_LOGW(...) ((void)0)
static bool s_nvs_ready,s_diag_shown;
static int s_volume,s_last_input,reason=5,mv=3310,failure,step,read_step,write_step,closes;
static pv_config_t s_saved,config;
static pv_model_t s_model;
static char s_diag_text[PV_DIAG_TEXT_MAX];
static uint16_t stored[2]={4,4001},pending[2];
static int nvs_flash_init(void){return ESP_OK;}
static int nvs_open(const char*n,int mode,int*h){assert(!strcmp(n,"passport"));if(mode==NVS_READWRITE && failure==1)return 99;*h=mode;return 0;}
static int nvs_get_u8(int h,const char*k,uint8_t*v){(void)h;assert(!strcmp(k,"volume"));*v=60;return 0;}
static int nvs_get_blob(int h,const char*k,void*out,size_t*n){
 (void)h;
 if(!strcmp(k,"diag")){read_step=++step;assert(*n==sizeof(stored));memcpy(out,stored,*n);return 0;}
 assert(!strcmp(k,"config_v1"));if(out)memcpy(out,&config,sizeof(config));*n=sizeof(config);return 0;
}
static int nvs_set_blob(int h,const char*k,const void*p,size_t n){
 assert(h==NVS_READWRITE && !strcmp(k,"diag") && n==sizeof(stored));
 write_step=++step;assert(read_step && read_step<write_step);
 if(failure==2)return 99;memcpy(pending,p,n);return 0;
}
static int nvs_commit(int h){assert(h==NVS_READWRITE);if(failure==3)return 99;memcpy(stored,pending,sizeof(stored));return 0;}
static void nvs_close(int h){(void)h;++closes;}
static int bsp_battery_init(void){return 0;}
static int bsp_battery_mv(void){return mv;}
static int esp_reset_reason(void){return reason;}
static int esp_timer_get_time(void){return 123;}
static void show(const char*a,const char*b){(void)a;(void)b;assert(0);}
static void vTaskDelete(void*p){(void)p;assert(0);}
'''
        source += 'static esp_err_t load_saved(void){' + function_body(app, 'load_saved') + '}\n'
        source += 'static void boot(void *arg){' + boot + '}\n'
        source += r'''
static void reset_ram(void){s_diag_shown=false;memset(&s_saved,0,sizeof(s_saved));memset(&s_model,0,sizeof(s_model));step=read_step=write_step=0;closes=0;}
int main(void){
 config.version=PV_CONFIG_VERSION;strcpy(config.ssid,"saved-network");strcpy(config.password,"saved-password");strcpy(config.gateway,"ws://example.com:8765");
 pv_config_t original=config;
 boot(NULL);assert(strstr(s_diag_text,"panic") && strstr(s_diag_text,"4000"));
 assert(stored[0]==5 && stored[1]==3311 && s_diag_shown && closes==2);
 assert(!memcmp(&config,&original,sizeof(config)) && !memcmp(&s_saved,&original,sizeof(config)));
 reset_ram();reason=1;boot(NULL);assert(strstr(s_diag_text,"interrupt") && strstr(s_diag_text,"3310"));assert(write_step==0);
 /* Reading/hiding does not consume history; another ordinary boot reads it. */
 s_diag_shown=false;reset_ram();boot(NULL);assert(s_diag_shown && stored[0]==5);
 for(failure=1;failure<=3;++failure){reset_ram();reason=9;boot(NULL);
  assert(s_diag_shown && strstr(s_diag_text,"interrupt") && stored[0]==5 && stored[1]==3311);
  assert(s_model.has_saved && s_volume==60 && !memcmp(&s_saved,&original,sizeof(original)) && !memcmp(&config,&original,sizeof(original)));
 }
}
'''
        self.run_c(source, 'main/passport_config.c', 'main/passport_diag.c')


if __name__ == "__main__":
    unittest.main()
