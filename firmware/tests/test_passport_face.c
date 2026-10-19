#include "passport_face.h"
#include <assert.h>
#include <stdio.h>

static void test_status_mapping(void)
{
    assert(pv_face_state_for_status("Listening...") == PV_FACE_LISTENING);
    assert(pv_face_state_for_status("Thinking...") == PV_FACE_THINKING);
    assert(pv_face_state_for_status("Speaking...") == PV_FACE_SPEAKING);
    assert(pv_face_state_for_status("Retrying...") == PV_FACE_SEARCHING);
    assert(pv_face_state_for_status("Audio error") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Send failed") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Voice unavailable") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Task error") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Network error") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Storage error") == PV_FACE_ERROR);
    assert(pv_face_state_for_status("Save failed") == PV_FACE_ERROR);
    /* Informational states fall back to the idle face. */
    assert(pv_face_state_for_status("Ready") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Starting...") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Connecting...") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Voice starting...") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Wi-Fi setup") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Hold OK to talk") == PV_FACE_IDLE);
    assert(pv_face_state_for_status("") == PV_FACE_IDLE);
    assert(pv_face_state_for_status(NULL) == PV_FACE_IDLE);
    assert(pv_face_state_for_status("Listening...x") == PV_FACE_IDLE);
}

static void test_idle_and_blink(void)
{
    pv_face_model_t m;
    pv_face_out_t o;
    pv_face_init(&m, 0, 0);
    assert(m.blink_due_ms >= 2000 && m.blink_due_ms <= 2000u + 2999u);

    /* Far from the next blink: eyes open, pupils centered, mouth closed. */
    pv_face_tick(&m, 1000, 0, PV_FACE_IDLE, PV_FACE_MOUTH_NONE, &o);
    assert(o.eye_open == PV_FACE_EYE_IDLE);
    assert(o.pupil_dx == 50 && o.pupil_dy == 50 && o.mouth_open == 0);

    /* At the due time the eyes close for one tick. */
    uint32_t due = m.blink_due_ms;
    pv_face_tick(&m, due, 500, PV_FACE_IDLE, PV_FACE_MOUTH_NONE, &o);
    assert(m.blinking);
    assert(m.blink_due_ms == due); /* unchanged until the blink finishes */
    assert(o.eye_open <= PV_FACE_EYE_BLINK);

    /* Blinking pauses while listening; the face stays wide open. */
    pv_face_tick(&m, due + 50, 0, PV_FACE_LISTENING, PV_FACE_MOUTH_NONE, &o);
    assert(!m.blinking);
    assert(o.eye_open > PV_FACE_EYE_IDLE);

    /* Back to idle past the blink window: reopened, next blink rescheduled. */
    pv_face_tick(&m, due + 200, 500, PV_FACE_IDLE, PV_FACE_MOUTH_NONE, &o);
    assert(!m.blinking);
    assert(o.eye_open == PV_FACE_EYE_IDLE);
    assert(m.blink_due_ms > due + 200);
    assert(m.blink_due_ms <= due + 200 + 2000u + 2999u);
}

static void test_states(void)
{
    pv_face_model_t m;
    pv_face_out_t a, b;
    pv_face_init(&m, 0, 0);

    pv_face_tick(&m, 5000, 0, PV_FACE_LISTENING, PV_FACE_MOUTH_NONE, &a);
    assert(a.eye_open > PV_FACE_EYE_IDLE);
    assert(a.pupil_dx == 50 && a.pupil_dy == 50);

    pv_face_tick(&m, 6000, 0, PV_FACE_ERROR, PV_FACE_MOUTH_NONE, &a);
    assert(a.eye_open < PV_FACE_EYE_IDLE);
    assert(a.pupil_dy > 50 && a.mouth_open == 0);

    /* Thinking: pupils sweep across the 10..90 range over time. */
    pv_face_init(&m, 0, 0);
    pv_face_tick(&m, 0, 0, PV_FACE_THINKING, PV_FACE_MOUTH_NONE, &a);
    pv_face_tick(&m, 312, 0, PV_FACE_THINKING, PV_FACE_MOUTH_NONE, &b);
    assert(a.pupil_dx != b.pupil_dx);
    assert(a.pupil_dx >= 10 && a.pupil_dx <= 90);
    assert(b.pupil_dx >= 10 && b.pupil_dx <= 90);
    pv_face_tick(&m, 625, 0, PV_FACE_THINKING, PV_FACE_MOUTH_NONE, &a);
    pv_face_tick(&m, 937, 0, PV_FACE_THINKING, PV_FACE_MOUTH_NONE, &b);
    assert(a.pupil_dx != b.pupil_dx);

    /* Searching: calmer eyes, slower sweep. */
    pv_face_init(&m, 0, 0);
    pv_face_tick(&m, 0, 0, PV_FACE_SEARCHING, PV_FACE_MOUTH_NONE, &a);
    pv_face_tick(&m, 750, 0, PV_FACE_SEARCHING, PV_FACE_MOUTH_NONE, &b);
    assert(a.pupil_dx != b.pupil_dx);
    assert(a.eye_open < PV_FACE_EYE_IDLE);
}

static void test_mouth(void)
{
    pv_face_model_t m;
    pv_face_out_t o;
    pv_face_init(&m, 0, 0);

    /* Attack is immediate. */
    pv_face_tick(&m, 0, 0, PV_FACE_SPEAKING, 100, &o);
    assert(o.mouth_open == 100);
    assert(o.eye_open > PV_FACE_EYE_BLINK); /* blinking still allowed */

    /* Decay is gradual: 100 ms after silence it is lower but not zero. */
    pv_face_tick(&m, 100, 0, PV_FACE_SPEAKING, PV_FACE_MOUTH_NONE, &o);
    assert(o.mouth_open > 0 && o.mouth_open < 100);

    /* And reaches zero within a second of silence. */
    pv_face_tick(&m, 1000, 0, PV_FACE_SPEAKING, 0, &o);
    assert(o.mouth_open == 0);

    /* Out-of-range input is clamped. */
    pv_face_tick(&m, 1100, 0, PV_FACE_SPEAKING, 200, &o);
    assert(o.mouth_open == 100);

    /* Only Speaking uses the measured level. */
    pv_face_tick(&m, 1200, 0, PV_FACE_IDLE, 90, &o);
    assert(o.mouth_open == 0);

    /* PV_FACE_MOUTH_NONE is treated as silence. */
    pv_face_tick(&m, 1300, 0, PV_FACE_SPEAKING, PV_FACE_MOUTH_NONE, &o);
    assert(o.mouth_open == 0);

    /* Time going backwards must not crash or blow up the decay. */
    pv_face_tick(&m, 100, 0, PV_FACE_SPEAKING, 100, &o);
    assert(o.mouth_open == 100);
}

int main(void)
{
    test_status_mapping();
    test_idle_and_blink();
    test_states();
    test_mouth();
    puts("passport face animation tests: PASS");
    return 0;
}
