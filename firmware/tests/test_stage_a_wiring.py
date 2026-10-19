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


if __name__ == "__main__":
    unittest.main()
