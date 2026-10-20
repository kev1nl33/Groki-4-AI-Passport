#include "passport_diag.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* Mirrors esp_reset_reason_t values so the test needs no IDF headers. */
#define RST_UNKNOWN 0
#define RST_POWERON 1
#define RST_EXT 2
#define RST_SW 3
#define RST_PANIC 4
#define RST_INT_WDT 5
#define RST_TASK_WDT 6
#define RST_WDT 7
#define RST_DEEPSLEEP 8
#define RST_BROWNOUT 9
#define RST_SDIO 10
#define RST_USB 11
#define RST_JTAG 12
#define RST_EFUSE 13
#define RST_PWR_GLITCH 14
#define RST_CPU_LOCKUP 15

static void abnormal(void)
{
    /* Power-on and software restarts are routine; everything else is worth
       keeping so the next boot can show why the device came back. */
    assert(!pv_diag_abnormal(RST_POWERON));
    assert(!pv_diag_abnormal(RST_SW));
    assert(!pv_diag_abnormal(RST_DEEPSLEEP));
    assert(pv_diag_abnormal(RST_PANIC));
    assert(pv_diag_abnormal(RST_INT_WDT));
    assert(pv_diag_abnormal(RST_TASK_WDT));
    assert(pv_diag_abnormal(RST_WDT));
    assert(pv_diag_abnormal(RST_BROWNOUT));
    assert(pv_diag_abnormal(RST_PWR_GLITCH));
    assert(pv_diag_abnormal(RST_CPU_LOCKUP));
    assert(pv_diag_abnormal(RST_UNKNOWN));
    assert(pv_diag_abnormal(RST_USB));
}

static void text(void)
{
    char buf[PV_DIAG_TEXT_MAX];
    /* A missing reading must stay visible as unknown, never as 0 mV. */
    assert(pv_diag_format(buf, sizeof(buf), RST_PANIC, -1));
    assert(!strcmp(buf, "Restart: panic\nBoot battery: unknown"));
    assert(pv_diag_format(buf, sizeof(buf), RST_BROWNOUT, 3310));
    assert(!strcmp(buf, "Restart: brownout\nBoot battery: 3310 mV"));
    assert(pv_diag_format(buf, sizeof(buf), 99, 4000));
    assert(!strcmp(buf, "Restart: reason 99\nBoot battery: 4000 mV"));
    /* The buffer is too small: report failure instead of a cut-off line. */
    assert(!pv_diag_format(buf, 8, RST_PANIC, -1));
}

int main(void)
{
    abnormal();
    text();
    puts("passport diag tests: PASS");
}
