#ifndef PASSPORT_DIAG_H
#define PASSPORT_DIAG_H

#include <stdbool.h>
#include <stddef.h>

/* Stage G boot diagnostic: pure formatting, host-testable, no IDF includes.
   The app loads the last abnormal record before storing this boot's reset
   reason and boot-time battery voltage. Reading never consumes the record. */

#define PV_DIAG_TEXT_MAX 64u

/* Routine restarts (power-on, software, deep sleep) are not recorded; a
   crash, watchdog, brownout or power glitch is. */
bool pv_diag_abnormal(int reset_reason);

/* Writes two lines: "Restart: panic\nBoot battery: 3310 mV". Voltage is
   sampled at boot, not at the instant of a crash. Values outside the stored
   0..65534 mV range render as unknown; unknown uint16_t reasons retain their
   number, other reason values render as unknown. Returns false on truncation. */
bool pv_diag_format(char *out, size_t capacity, int reset_reason, int battery_mv);

#endif
