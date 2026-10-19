#include "passport_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void forms(void)
{
    pv_config_t c = {0};
    assert(pv_parse_form("ssid=Home+WiFi&pass=12345678&gateway=ws%3A%2F%2F192.168.1.2%3A8765&token=abc", "abc", &c) == PV_VALID);
    assert(!strcmp(c.ssid, "Home WiFi"));
    assert(!strcmp(c.gateway, "ws://192.168.1.2:8765"));
    assert(c.version == PV_CONFIG_VERSION);
    assert(pv_parse_form("ssid=x&pass=&gateway=ws://host:8765&token=wrong", "abc", &c) == PV_BAD_TOKEN);
    assert(pv_parse_form("ssid=%00x&pass=&gateway=ws://host:8765&token=abc", "abc", &c) == PV_BAD_FORM);
    assert(pv_parse_form("ssid=%GG&pass=&gateway=ws://host:8765&token=abc", "abc", &c) == PV_BAD_FORM);
    assert(pv_parse_form("ssid=x&ssid=y&pass=&gateway=ws://host:8765&token=abc", "abc", &c) == PV_BAD_FORM);
    assert(pv_parse_form("ssid=x&pass=&gateway=ws://host:8765&token=abc&extra=x", "abc", &c) == PV_BAD_FORM);
    assert(pv_parse_form("ssid=x&pass=short&gateway=ws://host:8765&token=abc", "abc", &c) == PV_BAD_PASSWORD);
    assert(pv_parse_form("ssid=x&pass=&gateway=ws://host:8765", "abc", &c) == PV_BAD_FORM);
    char long_form[PV_FORM_MAX + 2];
    memset(long_form, 'a', sizeof(long_form) - 1); long_form[sizeof(long_form) - 1] = 0;
    assert(pv_parse_form(long_form, "abc", &c) == PV_BAD_FORM);
    const char *bad[] = {"http://host:8765", "wss://host:8765", "ws://", "ws://user@host:8765", "ws://host:0", "ws://host:65536", "ws://host:8765/#x", "ws://host:8765?x", "ws://host:8765/ x", "ws://host", "ws://host:abc", "ws://-host:8765", "ws://host..x:8765"};
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i) assert(!pv_gateway_valid(bad[i]));
    assert(pv_gateway_valid("ws://gateway.local:8765/pcm"));
    assert(pv_gateway_valid("ws://192.168.4.2:8765"));
    c = (pv_config_t){.version=PV_CONFIG_VERSION};
    strcpy(c.ssid, "x"); strcpy(c.gateway, "ws://host:8765");
    assert(pv_config_valid(&c));
    c.version++; assert(!pv_config_valid(&c));
    c.version=PV_CONFIG_VERSION; memset(c.ssid,'a',sizeof(c.ssid)); assert(!pv_config_valid(&c));
}

static void model(void)
{
    pv_model_t m = {0};
    pv_begin(&m, true, false);
    unsigned old = m.generation;
    assert(!pv_got_ip(&m, old)); /* DHCP alone cannot approve an old association. */
    pv_begin(&m, true, true);
    assert(m.has_saved && m.candidate);
    assert(!pv_link(&m, old));
    assert(!pv_got_ip(&m, old));
    assert(!pv_got_ip(&m, m.generation));
    assert(pv_link(&m, m.generation));
    assert(pv_got_ip(&m, m.generation));
    assert(m.state == PV_SAVING && m.has_saved);
    pv_saved(&m, false);
    assert(m.state == PV_SAVE_FAILED && m.has_saved && m.candidate);
    pv_saved(&m, true);
    assert(m.state == PV_ONLINE && !m.candidate && m.has_saved);
    pv_begin(&m, true, true); pv_failed(&m);
    assert(m.state == PV_FAILED && m.has_saved);
    assert(pv_cancel(&m));
    assert(!m.candidate && m.state == PV_CONNECTING);
    pv_begin(&m, false, true);
    assert(!pv_cancel(&m));
    assert(m.state == PV_SETUP && !m.has_saved);
}
int main(void) {
    assert(pv_volume_step(95, 10) == 100);
    assert(pv_volume_step(5, -10) == 0);
    assert(pv_volume_step(50, 10) == 60);
    assert(!pv_volume_save_due(true, true, 3000, 0));
    assert(!pv_volume_save_due(true, false, 1999, 0));
    assert(pv_volume_save_due(true, false, 2000, 0));
    forms(); model(); puts("passport configuration and state tests: PASS"); }
