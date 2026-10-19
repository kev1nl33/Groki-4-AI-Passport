// main/main.c —— Passport Voice Stage A: connectivity-first application entry.
// The baseline demo menu was replaced by this application's own status page;
// audio and BLE are deferred out of Stage A to keep network headroom.
#include "bsp_display.h"
#include "bsp_button.h"
#include "bsp_pins.h"
#include "passport_app.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "main";

static void on_key(bsp_btn_t btn, bsp_btn_ev_t ev, void *user)
{
    // Button callbacks run on the shared esp_timer task: forward and return.
    (void)user;
    passport_app_key(btn, ev);
}

void app_main(void)
{
    ESP_LOGI(TAG, "Passport Voice Stage A start");
    if (bsp_display_init() != ESP_OK || !bsp_lvgl_init()) {
        ESP_LOGE(TAG, "Display/LVGL init failed (MOSI=%d SCLK=%d CS=%d DC=%d BL=%d)",
                 BSP_LCD_MOSI, BSP_LCD_SCLK, BSP_LCD_CS, BSP_LCD_DC, BSP_LCD_BL);
        return;
    }
    bsp_display_backlight(70);
    if (bsp_button_init(on_key, NULL) != ESP_OK) {
        ESP_LOGE(TAG, "Button init failed; application continues read-only");
    }
    // I2C stays owned by the battery reads inside the owner task. Audio codec
    // and BLE are intentionally not initialized in Stage A.
    passport_app_start();
}
