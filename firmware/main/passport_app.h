#pragma once
/* Lifetime-long application owner. No page teardown while producers are active. */
#include "bsp_button.h"
void passport_app_start(void);
void passport_app_key(bsp_btn_t key, bsp_btn_ev_t event);
