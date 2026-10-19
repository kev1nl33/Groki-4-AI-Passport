#pragma once
/* Stage D: pure downlink framing and Speaking/half-duplex state. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>

#define PV_DOWN_FRAME_BYTES 960u /* 24 kHz * 20 ms * 16 bit mono */
#define PV_DOWN_QUEUE_DEPTH 8u
#define PV_DOWN_DRAIN_GRACE_MS 100u
#define PV_DOWN_IDLE_TIMEOUT_MS 5000u

typedef enum {
    PV_DOWN_IDLE,
    PV_DOWN_SPEAKING,
    PV_DOWN_DRAINING,
} pv_down_state_t;

typedef enum {
    PV_DOWN_EV_START,   /* tts.start */
    PV_DOWN_EV_STOP,    /* tts.stop: drain, never truncate */
    PV_DOWN_EV_DRAINED, /* playback queue emptied or idle timeout */
    PV_DOWN_EV_LOST,    /* WebSocket/Wi-Fi loss or send failure */
    PV_DOWN_EV_ERROR,   /* codec write or format switch failure */
} pv_down_event_t;

typedef enum {
    PV_DOWN_ACT_NONE,
    PV_DOWN_ACT_START_AUDIO, /* switch codec to 24 kHz */
    PV_DOWN_ACT_STOP_AUDIO,  /* switch codec back to 16 kHz */
    PV_DOWN_ACT_RESET,       /* drop queue and restore uplink format */
} pv_down_action_t;

typedef struct {
    pv_down_state_t state;
    unsigned frames_played;
    unsigned frames_dropped;
} pv_down_model_t;

void pv_down_init(pv_down_model_t *model);
pv_down_action_t pv_down_event(pv_down_model_t *model, pv_down_event_t event);
bool pv_down_can_listen(const pv_down_model_t *model);
bool pv_down_speaking(const pv_down_model_t *model);

typedef struct {
    size_t used;
    uint8_t data[PV_DOWN_FRAME_BYTES];
} pv_down_accum_t;

/* Binary acceptance gate. Frames are armed by tts.start in the WS callback
   and disarmed by tts.stop, loss, error, or completion. */
typedef struct {
    atomic_bool armed;
    pv_down_accum_t accum;
} pv_down_rx_t;

void pv_down_rx_init(pv_down_rx_t *rx);
void pv_down_rx_arm(pv_down_rx_t *rx);
void pv_down_rx_disarm(pv_down_rx_t *rx);
bool pv_down_rx_append(pv_down_rx_t *rx, const uint8_t *data, size_t len,
                       size_t *consumed, uint8_t out[PV_DOWN_FRAME_BYTES]);

void pv_down_accum_reset(pv_down_accum_t *accum);
/* Append a chunk. Returns true when out holds one complete frame; consumed is
   the number of input bytes taken (callers loop while consumed < len). */
bool pv_down_append(pv_down_accum_t *accum, const uint8_t *data, size_t len,
                    size_t *consumed, uint8_t out[PV_DOWN_FRAME_BYTES]);
