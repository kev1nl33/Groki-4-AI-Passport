#include "passport_downlink.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_state(void)
{
    pv_down_model_t m;
    pv_down_init(&m);
    assert(m.state == PV_DOWN_IDLE);
    assert(pv_down_can_listen(&m));
    assert(!pv_down_speaking(&m));

    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_START_AUDIO);
    assert(m.state == PV_DOWN_SPEAKING);
    assert(!pv_down_can_listen(&m)); /* half-duplex: hold ignored */
    assert(pv_down_speaking(&m));
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_NONE);
    assert(m.state == PV_DOWN_SPEAKING);

    /* tts.stop only starts draining; audio is never truncated. */
    assert(pv_down_event(&m, PV_DOWN_EV_STOP) == PV_DOWN_ACT_NONE);
    assert(m.state == PV_DOWN_DRAINING);
    assert(pv_down_speaking(&m));
    assert(!pv_down_can_listen(&m));
    assert(pv_down_event(&m, PV_DOWN_EV_STOP) == PV_DOWN_ACT_NONE);
    /* A new segment may replace a draining one, but not an active one. */
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_START_AUDIO);
    assert(m.state == PV_DOWN_SPEAKING);
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_NONE);
    assert(pv_down_event(&m, PV_DOWN_EV_STOP) == PV_DOWN_ACT_NONE);

    assert(pv_down_event(&m, PV_DOWN_EV_DRAINED) == PV_DOWN_ACT_STOP_AUDIO);
    assert(m.state == PV_DOWN_IDLE);
    assert(pv_down_can_listen(&m));

    /* Idle stop and drained events are no-ops. */
    assert(pv_down_event(&m, PV_DOWN_EV_STOP) == PV_DOWN_ACT_NONE);
    assert(pv_down_event(&m, PV_DOWN_EV_DRAINED) == PV_DOWN_ACT_NONE);

    /* A missing tts.stop is recovered by the playback idle timeout. */
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_START_AUDIO);
    assert(pv_down_event(&m, PV_DOWN_EV_DRAINED) == PV_DOWN_ACT_STOP_AUDIO);
    assert(m.state == PV_DOWN_IDLE);

    /* Loss or a codec error always resets, even mid-drain. */
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_START_AUDIO);
    assert(pv_down_event(&m, PV_DOWN_EV_LOST) == PV_DOWN_ACT_RESET);
    assert(m.state == PV_DOWN_IDLE && !pv_down_speaking(&m));
    assert(pv_down_event(&m, PV_DOWN_EV_START) == PV_DOWN_ACT_START_AUDIO);
    assert(pv_down_event(&m, PV_DOWN_EV_STOP) == PV_DOWN_ACT_NONE);
    assert(pv_down_event(&m, PV_DOWN_EV_ERROR) == PV_DOWN_ACT_RESET);
    assert(m.state == PV_DOWN_IDLE);
    assert(pv_down_event(&m, PV_DOWN_EV_LOST) == PV_DOWN_ACT_NONE);
}

static bool append_all(pv_down_accum_t *a, const uint8_t *data, size_t len,
                       uint8_t out[PV_DOWN_FRAME_BYTES], size_t *frames)
{
    size_t off = 0;
    *frames = 0;
    uint8_t scratch[PV_DOWN_FRAME_BYTES];
    while (off < len) {
        size_t used = 0;
        bool full = pv_down_append(a, data + off, len - off, &used, scratch);
        assert(used > 0);
        off += used;
        if (full) {
            ++*frames;
            if (*frames == 1) memcpy(out, scratch, sizeof(scratch));
        }
    }
    return true;
}

static void test_binary_rx_gate(void)
{
    pv_down_rx_t rx;
    pv_down_rx_init(&rx);
    uint8_t out[PV_DOWN_FRAME_BYTES];
    uint8_t data[PV_DOWN_FRAME_BYTES];
    size_t used = 99;
    memset(data, 0x77, sizeof(data));

    /* Idle: binary frames are ignored and never accumulated. */
    assert(!pv_down_rx_append(&rx, data, 480, &used, out));
    assert(used == 0 && rx.accum.used == 0);
    assert(!pv_down_rx_append(&rx, data, 960, &used, out));
    assert(!pv_down_rx_append(&rx, data, 960, &used, out));
    assert(used == 0 && rx.accum.used == 0);

    pv_down_rx_arm(&rx);
    assert(!pv_down_rx_append(&rx, data, 480, &used, out));
    assert(used == 480 && rx.accum.used == 480);
    /* A duplicate start keeps the partial frame. */
    pv_down_rx_arm(&rx);
    assert(rx.accum.used == 480);
    assert(pv_down_rx_append(&rx, data, 480, &used, out));
    assert(used == 480 && rx.accum.used == 0);

    /* tts.stop disarms and clears any partial frame. */
    assert(!pv_down_rx_append(&rx, data, 100, &used, out));
    assert(rx.accum.used == 100);
    pv_down_rx_disarm(&rx);
    assert(rx.accum.used == 0);
    assert(!pv_down_rx_append(&rx, data, 960, &used, out));
    assert(used == 0 && rx.accum.used == 0);

    /* A later segment re-arms cleanly. */
    pv_down_rx_arm(&rx);
    assert(pv_down_rx_append(&rx, data, 960, &used, out));
    assert(used == 960 && rx.accum.used == 0);
}

static void test_accumulator(void)
{
    pv_down_accum_t a;
    pv_down_accum_reset(&a);
    uint8_t out[PV_DOWN_FRAME_BYTES];
    uint8_t data[PV_DOWN_FRAME_BYTES * 2];
    size_t frames = 0;

    memset(data, 0x11, sizeof(data));
    append_all(&a, data, PV_DOWN_FRAME_BYTES, out, &frames);
    assert(frames == 1 && out[0] == 0x11 && out[959] == 0x11);
    assert(a.used == 0);

    /* One frame split across WS fragments. */
    memset(data, 0x22, sizeof(data));
    append_all(&a, data, 480, out, &frames);
    assert(frames == 0 && a.used == 480);
    append_all(&a, data + 480, 480, out, &frames);
    assert(frames == 1 && out[0] == 0x22 && out[479] == 0x22);

    /* Message carrying more than one frame plus a partial tail. */
    memset(data, 0x33, sizeof(data));
    append_all(&a, data, 1000, out, &frames);
    assert(frames == 1 && a.used == 40);
    assert(a.data[0] == 0x33 && a.data[39] == 0x33);
    memset(data, 0x44, sizeof(data));
    append_all(&a, data, 920, out, &frames);
    assert(frames == 1 && a.used == 0);
    assert(out[0] == 0x33 && out[39] == 0x33);
    assert(out[40] == 0x44 && out[959] == 0x44);

    /* Two complete frames in one chunk. */
    append_all(&a, data, PV_DOWN_FRAME_BYTES * 2, out, &frames);
    assert(frames == 2 && a.used == 0);

    memset(data, 0x55, sizeof(data));
    append_all(&a, data, 17, out, &frames);
    assert(a.used == 17);
    pv_down_accum_reset(&a);
    assert(a.used == 0);

    size_t used = 123;
    assert(!pv_down_append(&a, data, 0, &used, out));
    assert(used == 0);
}

int main(void)
{
    test_state();
    test_accumulator();
    test_binary_rx_gate();
    puts("passport downlink state and framing tests: PASS");
    return 0;
}
