#include "passport_config.h"
#include <ctype.h>
#include <stddef.h>
#include <string.h>

uint8_t pv_volume_step(uint8_t volume, int delta)
{
    int value = (int)volume + delta;
    return value < 0 ? 0 : value > 100 ? 100 : (uint8_t)value;
}
bool pv_volume_save_due(bool dirty, bool busy, int64_t now_ms, int64_t changed_ms)
{
    return dirty && !busy && now_ms - changed_ms >= 2000;
}

static bool terminated(const char *s, size_t n) { return memchr(s, 0, n) != NULL; }
static bool printable(const char *s)
{
    for (; *s; ++s) if ((unsigned char)*s < 32 || (unsigned char)*s == 127) return false;
    return true;
}
static int hex(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static bool decode(const char *s, size_t n, char *out, size_t capacity)
{
    size_t used = 0;
    for (size_t i = 0; i < n; ++i) {
        unsigned char c = (unsigned char)s[i];
        if (c == '%') {
            if (i + 2 >= n || hex(s[i+1]) < 0 || hex(s[i+2]) < 0) return false;
            c = (unsigned char)((hex(s[i+1]) << 4) | hex(s[i+2])); i += 2;
        } else if (c == '+') c = ' ';
        if (c < 32 || c == 127 || used + 1 >= capacity) return false;
        out[used++] = (char)c;
    }
    out[used] = 0;
    return true;
}
bool pv_gateway_valid(const char *u)
{
    if (!u || strlen(u) > PV_GATEWAY_MAX || strncmp(u, "ws://", 5)) return false;
    const char *p = u + 5, *host = p;
    bool label_start = true;
    for (; *p && *p != ':'; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '.') {
            if (label_start || p[-1] == '-') return false;
            label_start = true;
        } else {
            if (!(isalnum(c) && c < 128) && c != '-') return false;
            if (label_start && c == '-') return false;
            label_start = false;
        }
    }
    if (p == host || label_start || p[-1] == '-' || *p++ != ':') return false;
    unsigned port = 0, digits = 0;
    while (*p >= '0' && *p <= '9') {
        port = port * 10 + (unsigned)(*p++ - '0');
        if (++digits > 5 || port > 65535) return false;
    }
    if (!digits || !port || (*p && *p != '/')) return false;
    for (; *p; ++p) {
        unsigned char c = (unsigned char)*p;
        if (!(isalnum(c) && c < 128) && !strchr("/-._~", c)) return false;
    }
    return true;
}
static pv_validation_t validate(const pv_config_t *c)
{
    if (!terminated(c->ssid, sizeof(c->ssid)) || !c->ssid[0] || !printable(c->ssid)) return PV_BAD_SSID;
    if (!terminated(c->password, sizeof(c->password))) return PV_BAD_PASSWORD;
    size_t n = strlen(c->password);
    if (n && (n < 8 || n > 63 || !printable(c->password))) return PV_BAD_PASSWORD;
    if (!terminated(c->gateway, sizeof(c->gateway)) || !pv_gateway_valid(c->gateway)) return PV_BAD_GATEWAY;
    if (!terminated(c->auth_token, sizeof(c->auth_token)) || !printable(c->auth_token)) return PV_BAD_FORM;
    return PV_VALID;
}
bool pv_config_valid(const pv_config_t *c)
{
    return c && c->version == PV_CONFIG_VERSION && validate(c) == PV_VALID;
}
bool pv_config_from_blob(const void *blob, size_t len, pv_config_t *out)
{
    if (!blob || !out) return false;
    /* v1 predates the auth token: accept it and treat the token as empty. */
    const size_t v1 = offsetof(pv_config_t, auth_token);
    if (len == v1) {
        pv_config_t c;
        memset(&c, 0, sizeof(c));
        memcpy(&c, blob, v1);
        if (c.version != PV_CONFIG_VERSION_V1 || validate(&c) != PV_VALID) return false;
        c.version = PV_CONFIG_VERSION;
        *out = c;
        return true;
    }
    if (len != sizeof(pv_config_t)) return false;
    pv_config_t c;
    memcpy(&c, blob, sizeof(c));
    if (!pv_config_valid(&c)) return false;
    *out = c;
    return true;
}
pv_validation_t pv_parse_form(const char *body, const char *token, pv_config_t *out)
{
    if (!body || !token || !out || strlen(body) > PV_FORM_MAX) return PV_BAD_FORM;
    pv_config_t c = {.version = PV_CONFIG_VERSION};
    char supplied[33] = {0};
    unsigned seen = 0;
    const char *p = body;
    while (*p) {
        const char *end = strchr(p, '&');
        if (!end) end = p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        if (!eq) return PV_BAD_FORM;
        unsigned bit; char *dest; size_t cap;
        size_t key = (size_t)(eq - p);
        if (key == 4 && !memcmp(p,"ssid",4)) { bit=1; dest=c.ssid; cap=sizeof(c.ssid); }
        else if (key == 4 && !memcmp(p,"pass",4)) { bit=2; dest=c.password; cap=sizeof(c.password); }
        else if (key == 7 && !memcmp(p,"gateway",7)) { bit=4; dest=c.gateway; cap=sizeof(c.gateway); }
        else if (key == 4 && !memcmp(p,"auth",4)) { bit=16; dest=c.auth_token; cap=sizeof(c.auth_token); }
        else if (key == 5 && !memcmp(p,"token",5)) { bit=8; dest=supplied; cap=sizeof(supplied); }
        else return PV_BAD_FORM;
        if ((seen & bit) || !decode(eq+1,(size_t)(end-eq-1),dest,cap)) return PV_BAD_FORM;
        seen |= bit;
        p = *end ? end+1 : end;
        if (*end && !*p) return PV_BAD_FORM;
    }
    if ((seen & 15) != 15 || (seen & ~31u)) return PV_BAD_FORM;
    if (strcmp(token, supplied)) return PV_BAD_TOKEN;
    pv_validation_t result = validate(&c);
    if (result == PV_VALID) *out = c;
    memset(c.password, 0, sizeof(c.password));
    memset(c.auth_token, 0, sizeof(c.auth_token));
    return result;
}
void pv_begin(pv_model_t *m, bool h, bool c)
{
    ++m->generation; m->has_saved=h; m->candidate=c; m->linked=false; m->state=PV_CONNECTING;
}
bool pv_link(pv_model_t *m, unsigned g)
{
    if (g != m->generation || m->state != PV_CONNECTING) return false;
    m->linked=true; return true;
}
bool pv_got_ip(pv_model_t *m, unsigned g)
{
    if (g != m->generation || m->state != PV_CONNECTING || !m->linked) return false;
    m->state=m->candidate ? PV_SAVING : PV_ONLINE; return true;
}
void pv_saved(pv_model_t *m, bool success)
{
    if (m->state != PV_SAVING && m->state != PV_SAVE_FAILED) return;
    m->state=success ? PV_ONLINE : PV_SAVE_FAILED;
    if (success) { m->candidate=false; m->has_saved=true; }
}
void pv_failed(pv_model_t *m) { m->linked=false; m->state=PV_FAILED; }
bool pv_cancel(pv_model_t *m)
{
    ++m->generation; m->candidate=false; m->linked=false;
    m->state=m->has_saved ? PV_CONNECTING : PV_SETUP;
    return m->has_saved;
}
