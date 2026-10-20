idf_component_register(
    SRCS "main.c"
         "passport_app.c"
         "passport_config.c"
         "passport_ws.c"
         "passport_audio.c"
         "passport_downlink.c"
         "passport_face.c"
         "passport_diag.c"
    INCLUDE_DIRS "."
    REQUIRES bsp esp_event esp_http_server esp_netif esp_timer esp_wifi
             esp_websocket_client nvs_flash
)
