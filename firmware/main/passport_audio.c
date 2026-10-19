#include "passport_audio.h"
#include <string.h>

void pv_audio_init(pv_audio_model_t *model)
{
    if (model) memset(model, 0, sizeof(*model));
}

bool pv_audio_frame_valid(size_t bytes)
{
    return bytes == PV_AUDIO_FRAME_BYTES;
}

bool pv_audio_send_ok(int sent, size_t len)
{
    return sent >= 0 && len > 0 && (size_t)sent == len;
}

bool pv_audio_queue_frame(pv_audio_stats_t *stats, bool accepted)
{
    if (!stats) return false;
    if (accepted) return false;
    ++stats->dropped;
    return true; /* caller evicts the oldest queued frame, then pushes */
}

pv_audio_action_t pv_audio_event(pv_audio_model_t *m, pv_audio_event_t event)
{
    if (!m) return PV_AUDIO_ACT_NONE;
    switch (event) {
    case PV_AUDIO_WS_READY:
        m->ws_ready = true;
        if (m->state == PV_AUDIO_ERROR) m->state = PV_AUDIO_IDLE;
        return PV_AUDIO_ACT_NONE;
    case PV_AUDIO_WS_LOST:
        m->ws_ready = false;
        if (m->state == PV_AUDIO_IDLE) return PV_AUDIO_ACT_NONE;
        m->state = PV_AUDIO_IDLE;
        return PV_AUDIO_ACT_RESET;
    case PV_AUDIO_PRESS:
        if (!m->ws_ready) return PV_AUDIO_ACT_NONE;
        if (m->state == PV_AUDIO_LISTENING) return PV_AUDIO_ACT_NONE;
        m->state = PV_AUDIO_LISTENING;
        return PV_AUDIO_ACT_START;
    case PV_AUDIO_RELEASE:
        if (m->state != PV_AUDIO_LISTENING) return PV_AUDIO_ACT_NONE;
        m->state = PV_AUDIO_THINKING;
        return PV_AUDIO_ACT_STOP;
    case PV_AUDIO_SEND_FAILED:
        if (m->state != PV_AUDIO_LISTENING && m->state != PV_AUDIO_THINKING)
            return PV_AUDIO_ACT_NONE;
        m->state = PV_AUDIO_ERROR;
        return PV_AUDIO_ACT_RESET;
    case PV_AUDIO_STOPPED:
        if (m->state != PV_AUDIO_THINKING) return PV_AUDIO_ACT_NONE;
        m->state = PV_AUDIO_IDLE;
        return PV_AUDIO_ACT_NONE;
    case PV_AUDIO_FRAME_SENT:
        if (m->state == PV_AUDIO_LISTENING || m->state == PV_AUDIO_THINKING)
            ++m->frames_sent;
        return PV_AUDIO_ACT_NONE;
    default:
        return PV_AUDIO_ACT_NONE;
    }
}

void pv_audio_note_pending(atomic_uint *bits, unsigned flag)
{
    if (bits) atomic_fetch_or(bits, flag);
}

bool pv_audio_take_pending(atomic_uint *bits, unsigned flag)
{
    if (!bits) return false;
    return (atomic_fetch_and(bits, ~flag) & flag) != 0;
}
