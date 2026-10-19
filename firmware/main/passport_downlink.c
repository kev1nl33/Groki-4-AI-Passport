#include "passport_downlink.h"
#include <string.h>

void pv_down_init(pv_down_model_t *model)
{
    if (model) memset(model, 0, sizeof(*model));
}

pv_down_action_t pv_down_event(pv_down_model_t *model, pv_down_event_t event)
{
    if (!model) return PV_DOWN_ACT_NONE;
    switch (event) {
    case PV_DOWN_EV_START:
        /* A new tts.start supersedes a draining previous segment. */
        if (model->state == PV_DOWN_SPEAKING) return PV_DOWN_ACT_NONE;
        model->state = PV_DOWN_SPEAKING;
        return PV_DOWN_ACT_START_AUDIO;
    case PV_DOWN_EV_STOP:
        if (model->state != PV_DOWN_SPEAKING) return PV_DOWN_ACT_NONE;
        model->state = PV_DOWN_DRAINING;
        return PV_DOWN_ACT_NONE;
    case PV_DOWN_EV_DRAINED:
        if (model->state == PV_DOWN_IDLE) return PV_DOWN_ACT_NONE;
        model->state = PV_DOWN_IDLE;
        return PV_DOWN_ACT_STOP_AUDIO;
    case PV_DOWN_EV_LOST:
    case PV_DOWN_EV_ERROR:
        if (model->state == PV_DOWN_IDLE) return PV_DOWN_ACT_NONE;
        model->state = PV_DOWN_IDLE;
        return PV_DOWN_ACT_RESET;
    default:
        return PV_DOWN_ACT_NONE;
    }
}

bool pv_down_can_listen(const pv_down_model_t *model)
{
    return model && model->state == PV_DOWN_IDLE;
}

bool pv_down_speaking(const pv_down_model_t *model)
{
    return model && model->state != PV_DOWN_IDLE;
}

void pv_down_accum_reset(pv_down_accum_t *accum)
{
    if (accum) accum->used = 0;
}

bool pv_down_append(pv_down_accum_t *accum, const uint8_t *data, size_t len,
                    size_t *consumed, uint8_t out[PV_DOWN_FRAME_BYTES])
{
    if (consumed) *consumed = 0;
    if (!accum || !data || !out || len == 0) return false;
    size_t room = PV_DOWN_FRAME_BYTES - accum->used;
    size_t take = len < room ? len : room;
    memcpy(accum->data + accum->used, data, take);
    accum->used += take;
    if (consumed) *consumed = take;
    if (accum->used < PV_DOWN_FRAME_BYTES) return false;
    memcpy(out, accum->data, PV_DOWN_FRAME_BYTES);
    accum->used = 0;
    return true;
}

void pv_down_rx_init(pv_down_rx_t *rx)
{
    if (!rx) return;
    atomic_store(&rx->armed, false);
    pv_down_accum_reset(&rx->accum);
}

void pv_down_rx_arm(pv_down_rx_t *rx)
{
    if (!rx) return;
    /* A duplicate start while already armed must not discard a partial
       frame from the active segment. */
    if (!atomic_exchange(&rx->armed, true))
        pv_down_accum_reset(&rx->accum);
}

void pv_down_rx_disarm(pv_down_rx_t *rx)
{
    if (!rx) return;
    atomic_store(&rx->armed, false);
    pv_down_accum_reset(&rx->accum);
}

bool pv_down_rx_append(pv_down_rx_t *rx, const uint8_t *data, size_t len,
                       size_t *consumed, uint8_t out[PV_DOWN_FRAME_BYTES])
{
    if (consumed) *consumed = 0;
    if (!rx || !atomic_load(&rx->armed)) {
        if (rx) pv_down_accum_reset(&rx->accum);
        return false;
    }
    return pv_down_append(&rx->accum, data, len, consumed, out);
}
