#pragma once
/* Stage C: pure listen/capture state, framing, and bounded queue policy. */
#include <stdbool.h>
#include <stddef.h>
#include <stdatomic.h>

#define PV_AUDIO_FRAME_BYTES 640u /* 16 kHz * 20 ms * 16 bit mono */
#define PV_AUDIO_QUEUE_DEPTH 8u   /* 160 ms; full queue drops the oldest */

typedef enum {
    PV_AUDIO_IDLE,
    PV_AUDIO_LISTENING,
    PV_AUDIO_THINKING,
    PV_AUDIO_ERROR,
} pv_audio_state_t;

typedef enum {
    PV_AUDIO_PRESS,
    PV_AUDIO_RELEASE,
    PV_AUDIO_WS_READY,
    PV_AUDIO_WS_LOST,
    PV_AUDIO_SEND_FAILED,
    PV_AUDIO_STOPPED, /* listen.stop was sent; the utterance is complete */
    PV_AUDIO_FRAME_SENT,
} pv_audio_event_t;

typedef enum {
    PV_AUDIO_ACT_NONE,
    PV_AUDIO_ACT_START, /* send listen.start and start capture */
    PV_AUDIO_ACT_STOP,  /* stop capture, drain, then send listen.stop */
    PV_AUDIO_ACT_RESET, /* abort capture and clear queued frames */
} pv_audio_action_t;

typedef struct {
    pv_audio_state_t state;
    bool ws_ready;
    unsigned frames_sent;
    unsigned frames_dropped;
} pv_audio_model_t;

typedef struct {
    unsigned dropped;
} pv_audio_stats_t;

void pv_audio_init(pv_audio_model_t *model);
pv_audio_action_t pv_audio_event(pv_audio_model_t *model, pv_audio_event_t event);
bool pv_audio_frame_valid(size_t bytes);
/* A send is successful only when the transport reports exactly len bytes. */
bool pv_audio_send_ok(int sent, size_t len);
/* Returns true when the caller must evict the oldest frame before pushing
   the new one; the policy and its counter stay unit-testable. */
bool pv_audio_queue_frame(pv_audio_stats_t *stats, bool accepted);

/* Critical-event coalescing: callbacks set a bit before trying the queue;
   the owner clears it when the event is consumed, and polls leftover bits so
   a full queue can never lose a release, stop, or disconnect. */
#define PV_AUDIO_PENDING_RELEASE   (1u << 0)
#define PV_AUDIO_PENDING_STOP_SENT (1u << 1)
#define PV_AUDIO_PENDING_WS_LOST   (1u << 2)
#define PV_AUDIO_PENDING_TTS_START (1u << 3)
#define PV_AUDIO_PENDING_TTS_STOP  (1u << 4)
#define PV_AUDIO_PENDING_PLAY_DONE (1u << 5)
#define PV_AUDIO_PENDING_PLAY_FAILED (1u << 6)
void pv_audio_note_pending(atomic_uint *bits, unsigned flag);
bool pv_audio_take_pending(atomic_uint *bits, unsigned flag);
