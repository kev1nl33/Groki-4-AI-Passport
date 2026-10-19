#ifndef PASSPORT_FACE_H
#define PASSPORT_FACE_H

#include <stdbool.h>
#include <stdint.h>

/* Stage F avatar: pure animation logic, host-testable, no LVGL includes.
   The app layer maps the 0..100 outputs onto basic lv_obj geometry. */

typedef enum {
    PV_FACE_IDLE = 0,   /* Starting / Connecting / Ready / informational */
    PV_FACE_LISTENING,  /* capture active: eyes wide */
    PV_FACE_THINKING,   /* waiting for the reply: pupils sweep */
    PV_FACE_SPEAKING,   /* downlink playback: mouth follows the level */
    PV_FACE_SEARCHING,  /* gateway retry: slower, calmer sweep */
    PV_FACE_ERROR,      /* audio/send/voice failure: drooping flat face */
    PV_FACE_STATE_COUNT
} pv_face_state_t;

#define PV_FACE_MOUTH_NONE 255u /* no fresh measurement this tick */
#define PV_FACE_PUPIL_MIN_OPEN 25u /* hide pupils below this openness */

#define PV_FACE_EYE_IDLE 78u
#define PV_FACE_EYE_BLINK 8u

/* Geometry in 0..100 units; 50 on the pupil axes means centered. */
typedef struct {
    uint8_t eye_open;
    uint8_t pupil_dx;
    uint8_t pupil_dy;
    uint8_t mouth_open;
} pv_face_out_t;

typedef struct {
    uint32_t last_ms;
    uint32_t blink_due_ms;
    uint32_t blink_until_ms;
    bool blinking;
    uint16_t mouth_smoothed; /* quarter-units for decay resolution */
} pv_face_model_t;

void pv_face_init(pv_face_model_t *m, uint32_t now_ms, uint32_t rand);
void pv_face_tick(pv_face_model_t *m, uint32_t now_ms, uint32_t rand,
                  pv_face_state_t state, uint8_t mouth_level,
                  pv_face_out_t *out);
pv_face_state_t pv_face_state_for_status(const char *status);

#endif
