#include "passport_audio.h"
#include <assert.h>
#include <stdio.h>

static void test_framing(void)
{
    assert(pv_audio_frame_valid(PV_AUDIO_FRAME_BYTES));
    const size_t bad[] = {0, 2, 639, 641, 1280, 4096};
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        assert(!pv_audio_frame_valid(bad[i]));
    }
}

static void test_state(void)
{
    pv_audio_model_t m;
    pv_audio_init(&m);
    assert(m.state == PV_AUDIO_IDLE && !m.ws_ready);

    /* Not READY: presses and releases are ignored. */
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_NONE);
    assert(m.state == PV_AUDIO_IDLE);
    assert(pv_audio_event(&m, PV_AUDIO_RELEASE) == PV_AUDIO_ACT_NONE);

    assert(pv_audio_event(&m, PV_AUDIO_WS_READY) == PV_AUDIO_ACT_NONE);
    assert(m.ws_ready);

    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
    assert(m.state == PV_AUDIO_LISTENING);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_NONE);
    assert(pv_audio_event(&m, PV_AUDIO_FRAME_SENT) == PV_AUDIO_ACT_NONE);
    assert(m.frames_sent == 1);

    assert(pv_audio_event(&m, PV_AUDIO_RELEASE) == PV_AUDIO_ACT_STOP);
    assert(m.state == PV_AUDIO_THINKING);
    assert(pv_audio_event(&m, PV_AUDIO_RELEASE) == PV_AUDIO_ACT_NONE);

    /* A new hold starts a new utterance from Thinking. */
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
    assert(m.state == PV_AUDIO_LISTENING);

    /* A send failure aborts the utterance and reports an error. */
    assert(pv_audio_event(&m, PV_AUDIO_SEND_FAILED) == PV_AUDIO_ACT_RESET);
    assert(m.state == PV_AUDIO_ERROR);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
    assert(m.state == PV_AUDIO_LISTENING);

    /* Losing the WebSocket resets immediately and revokes Ready. */
    assert(pv_audio_event(&m, PV_AUDIO_WS_LOST) == PV_AUDIO_ACT_RESET);
    assert(m.state == PV_AUDIO_IDLE && !m.ws_ready);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_NONE);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_NONE);

    /* Reconnect restores Ready, then a press works again. */
    assert(pv_audio_event(&m, PV_AUDIO_WS_READY) == PV_AUDIO_ACT_NONE);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
    assert(pv_audio_event(&m, PV_AUDIO_RELEASE) == PV_AUDIO_ACT_STOP);
    assert(pv_audio_event(&m, PV_AUDIO_WS_LOST) == PV_AUDIO_ACT_RESET);
    assert(m.state == PV_AUDIO_IDLE);

    /* An error while idle is ignored; a duplicate Ready changes nothing. */
    assert(pv_audio_event(&m, PV_AUDIO_SEND_FAILED) == PV_AUDIO_ACT_NONE);
    assert(pv_audio_event(&m, PV_AUDIO_WS_READY) == PV_AUDIO_ACT_NONE);
    assert(pv_audio_event(&m, PV_AUDIO_WS_READY) == PV_AUDIO_ACT_NONE);
}

static void test_send_result_and_stop(void)
{
    assert(pv_audio_send_ok(640, 640));
    assert(!pv_audio_send_ok(639, 640));
    assert(!pv_audio_send_ok(641, 640));
    assert(!pv_audio_send_ok(0, 640));   /* zero bytes is not success */
    assert(!pv_audio_send_ok(-1, 640));
    assert(!pv_audio_send_ok(0, 0));

    pv_audio_model_t m;
    pv_audio_init(&m);
    (void)pv_audio_event(&m, PV_AUDIO_WS_READY);
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
    assert(pv_audio_event(&m, PV_AUDIO_RELEASE) == PV_AUDIO_ACT_STOP);
    assert(pv_audio_event(&m, PV_AUDIO_STOPPED) == PV_AUDIO_ACT_NONE);
    assert(m.state == PV_AUDIO_IDLE);
    assert(pv_audio_event(&m, PV_AUDIO_STOPPED) == PV_AUDIO_ACT_NONE);
    assert(m.state == PV_AUDIO_IDLE);
    /* A completed stop leaves Ready available for the next hold. */
    assert(pv_audio_event(&m, PV_AUDIO_PRESS) == PV_AUDIO_ACT_START);
}

static void test_critical_pending_burst(void)
{
    atomic_uint bits = 0;
    for (int i = 0; i < 1000; ++i)
        pv_audio_note_pending(&bits, PV_AUDIO_PENDING_RELEASE);
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_RELEASE));
    assert(!pv_audio_take_pending(&bits, PV_AUDIO_PENDING_RELEASE));

    pv_audio_note_pending(&bits, PV_AUDIO_PENDING_RELEASE | PV_AUDIO_PENDING_WS_LOST);
    for (int i = 0; i < 1000; ++i)
        pv_audio_note_pending(&bits, PV_AUDIO_PENDING_PLAY_DONE |
                                       PV_AUDIO_PENDING_PLAY_FAILED);
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_PLAY_DONE));
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_PLAY_FAILED));
    assert(!pv_audio_take_pending(&bits, PV_AUDIO_PENDING_PLAY_FAILED));
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_STOP_SENT) == false);
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_RELEASE));
    assert(pv_audio_take_pending(&bits, PV_AUDIO_PENDING_WS_LOST));
    assert(!pv_audio_take_pending(&bits, PV_AUDIO_PENDING_WS_LOST));
    assert(!pv_audio_take_pending(NULL, PV_AUDIO_PENDING_RELEASE));
}

static void test_queue_policy(void)
{
    pv_audio_stats_t stats = {0};
    assert(!pv_audio_queue_frame(&stats, true));
    assert(stats.dropped == 0);
    assert(pv_audio_queue_frame(&stats, false)); /* full: evict oldest */
    assert(stats.dropped == 1);
    assert(pv_audio_queue_frame(&stats, false));
    assert(stats.dropped == 2);
    assert(!pv_audio_queue_frame(&stats, true));
    assert(stats.dropped == 2);
}

int main(void)
{
    test_framing();
    test_state();
    test_send_result_and_stop();
    test_critical_pending_burst();
    test_queue_policy();
    puts("passport audio state and queue tests: PASS");
    return 0;
}
