#include "passport_diag.h"
#include <stdio.h>

/* Values copied from esp_reset_reason_t. Kept as literals so this file stays
   free of IDF headers and links into the host tests. */
#define PV_RST_POWERON 1
#define PV_RST_SW 3
#define PV_RST_DEEPSLEEP 8

bool pv_diag_abnormal(int reset_reason)
{
    return reset_reason != PV_RST_POWERON && reset_reason != PV_RST_SW &&
           reset_reason != PV_RST_DEEPSLEEP;
}

static const char *reason_name(int reset_reason)
{
    switch (reset_reason) {
    case 0: return "unknown";
    case 1: return "power on";
    case 2: return "external pin";
    case 3: return "software";
    case 4: return "panic";
    case 5: return "interrupt WDT";
    case 6: return "task WDT";
    case 7: return "watchdog";
    case 8: return "deep sleep";
    case 9: return "brownout";
    case 10: return "sdio";
    case 11: return "usb";
    case 12: return "jtag";
    case 13: return "efuse";
    case 14: return "power glitch";
    case 15: return "cpu lockup";
    default: return NULL;
    }
}

bool pv_diag_format(char *out, size_t capacity, int reset_reason, int battery_mv)
{
    if (!out || !capacity) return false;
    const char *name = reason_name(reset_reason);
    char reason[24];
    if (!name) {
        /* Stored reasons are uint16_t; keep even an out-of-range caller short. */
        if (reset_reason >= 0 && reset_reason <= 65535)
            snprintf(reason, sizeof(reason), "reason %d", reset_reason);
        else
            snprintf(reason, sizeof(reason), "unknown");
        name = reason;
    }
    int n;
    if (battery_mv < 0 || battery_mv > 65534)
        n = snprintf(out, capacity, "Restart: %s\nBoot battery: unknown", name);
    else
        n = snprintf(out, capacity, "Restart: %s\nBoot battery: %d mV", name, battery_mv);
    return n > 0 && (size_t)n < capacity;
}
