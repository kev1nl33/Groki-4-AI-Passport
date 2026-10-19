#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define PV_CONFIG_VERSION 2u
#define PV_CONFIG_VERSION_V1 1u
#define PV_FORM_MAX 1024u
#define PV_GATEWAY_MAX 191u
#define PV_AUTH_TOKEN_MAX 64u
typedef struct {
    uint32_t version;
    char ssid[33];
    char password[65];
    char gateway[PV_GATEWAY_MAX + 1];
    char auth_token[PV_AUTH_TOKEN_MAX + 1]; /* v2; empty means no gateway auth */
} pv_config_t;
typedef enum { PV_VALID, PV_BAD_FORM, PV_BAD_TOKEN, PV_BAD_SSID, PV_BAD_PASSWORD, PV_BAD_GATEWAY } pv_validation_t;
typedef enum { PV_SETUP, PV_CONNECTING, PV_SAVING, PV_ONLINE, PV_FAILED, PV_SAVE_FAILED } pv_state_t;
typedef struct {
    pv_state_t state;
    unsigned generation;
    bool has_saved;
    bool candidate;
    bool linked;
} pv_model_t;
pv_validation_t pv_parse_form(const char *body, const char *token, pv_config_t *out);
bool pv_gateway_valid(const char *url);
bool pv_config_valid(const pv_config_t *config);
/* A version-1 blob (no auth token) stays usable: the token reads back empty.
   Anything else is rejected so the caller keeps the stored blob untouched. */
bool pv_config_from_blob(const void *blob, size_t len, pv_config_t *out);
void pv_begin(pv_model_t *model, bool has_saved, bool candidate);
bool pv_link(pv_model_t *model, unsigned generation);
bool pv_got_ip(pv_model_t *model, unsigned generation);
void pv_saved(pv_model_t *model, bool success);
void pv_failed(pv_model_t *model);
uint8_t pv_volume_step(uint8_t volume, int delta);
bool pv_volume_save_due(bool dirty, bool busy, int64_t now_ms, int64_t changed_ms);
bool pv_cancel(pv_model_t *model);
