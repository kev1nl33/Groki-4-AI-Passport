#include "passport_face.h"
#include <string.h>

/* Blink timing. Randomised in 2..5 s so the face does not look metronomic. */
#define BLINK_MIN_MS 2000u
#define BLINK_SPAN_MS 3000u
#define BLINK_LEN_MS 120u

/* Thinking / searching pupil sweep periods. Slow enough for a 100 ms tick. */
#define THINK_PERIOD_MS 1250u
#define SEARCH_PERIOD_MS 3000u

/* Mouth decay in 1/1000 units per ms: a full-open mouth closes in ~500 ms. */
#define MOUTH_DECAY_PER_MS 2u

static uint8_t sweep(uint32_t now_ms, uint32_t period_ms)
{
    /* Triangle wave 10..90 with 50 as the centre. */
    const uint32_t half = period_ms / 2u;
    const uint32_t phase = now_ms % period_ms;
    const uint32_t pos = phase < half ? phase : period_ms - phase;
    return (uint8_t)(10u + (80u * pos) / (half ? half : 1u));
}

void pv_face_init(pv_face_model_t *m, uint32_t now_ms, uint32_t rand)
{
    if (!m) return;
    memset(m, 0, sizeof(*m));
    m->last_ms = now_ms;
    m->blink_due_ms = now_ms + BLINK_MIN_MS + rand % BLINK_SPAN_MS;
}

void pv_face_tick(pv_face_model_t *m, uint32_t now_ms, uint32_t rand,
                  pv_face_state_t state, uint8_t mouth_level,
                  pv_face_out_t *out)
{
    if (!m || !out) return;

    /* Elapsed time, tolerant of a caller that rewinds the clock. */
    const uint32_t elapsed = now_ms >= m->last_ms ? now_ms - m->last_ms : 0;
    m->last_ms = now_ms;

    /* Blink scheduling. Only idle-like states blink, so Listening keeps the
       wide-eyed look and Speaking never closes the eyes mid-word. */
    const bool blink_allowed = state == PV_FACE_IDLE;
    if (!blink_allowed) {
        /* A state change must never freeze the face mid-blink: drop the
           in-progress blink and start a fresh interval on return. */
        if (m->blinking) {
            m->blinking = false;
            m->blink_due_ms = now_ms + BLINK_MIN_MS + rand % BLINK_SPAN_MS;
        }
    } else if (m->blinking) {
        if (now_ms >= m->blink_until_ms) {
            m->blinking = false;
            m->blink_due_ms = now_ms + BLINK_MIN_MS + rand % BLINK_SPAN_MS;
        }
    } else if (now_ms >= m->blink_due_ms) {
        m->blinking = true;
        m->blink_until_ms = now_ms + BLINK_LEN_MS;
    }

    uint8_t eye;
    uint8_t dx = 50;
    uint8_t dy = 50;
    switch (state) {
    case PV_FACE_LISTENING:
        eye = 100;
        break;
    case PV_FACE_THINKING:
        eye = 90;
        dx = sweep(now_ms, THINK_PERIOD_MS);
        dy = 44;
        break;
    case PV_FACE_SEARCHING:
        eye = 58;
        dx = sweep(now_ms, SEARCH_PERIOD_MS);
        dy = 46;
        break;
    case PV_FACE_ERROR:
        eye = 40;
        dy = 66;
        break;
    case PV_FACE_SPEAKING:
        eye = 90;
        break;
    case PV_FACE_IDLE:
    default:
        eye = m->blinking ? PV_FACE_EYE_BLINK : PV_FACE_EYE_IDLE;
        break;
    }

    /* Mouth: immediate attack, exponential-ish decay toward zero. The
       model stores 1/1000 units so a per-ms decay is finer than the 0..100
       output the renderer sees. */
    uint32_t smoothed = m->mouth_smoothed;
    if (state == PV_FACE_SPEAKING) {
        uint32_t target;
        if (mouth_level == PV_FACE_MOUTH_NONE) {
            target = 0;
        } else {
            uint32_t level = mouth_level > 100u ? 100u : mouth_level;
            target = level * 10u;
        }
        if (target > smoothed) smoothed = target;
        else {
            uint32_t decay = (uint32_t)MOUTH_DECAY_PER_MS * elapsed;
            smoothed = decay >= smoothed ? 0 : smoothed - decay;
        }
    } else {
        smoothed = 0;
    }
    if (smoothed > 1000u) smoothed = 1000u;
    m->mouth_smoothed = (uint16_t)smoothed;

    out->eye_open = eye;
    out->pupil_dx = dx;
    out->pupil_dy = dy;
    out->mouth_open = (uint8_t)(smoothed / 10u);
}

pv_face_state_t pv_face_state_for_status(const char *status)
{
    if (!status) return PV_FACE_IDLE;
    if (!strcmp(status, "Listening...")) return PV_FACE_LISTENING;
    if (!strcmp(status, "Thinking...")) return PV_FACE_THINKING;
    if (!strcmp(status, "Speaking...")) return PV_FACE_SPEAKING;
    if (!strcmp(status, "Notice...")) return PV_FACE_SPEAKING; /* Stage H */
    if (!strcmp(status, "Retrying...")) return PV_FACE_SEARCHING;
    if (!strcmp(status, "Audio error") || !strcmp(status, "Send failed") ||
        !strcmp(status, "Voice unavailable") || !strcmp(status, "Task error") ||
        !strcmp(status, "Network error") || !strcmp(status, "Storage error") ||
        !strcmp(status, "Save failed"))
        return PV_FACE_ERROR;
    return PV_FACE_IDLE;
}
