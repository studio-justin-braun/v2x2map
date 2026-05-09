#pragma once

#include <stdint.h>

void bt_stream_init(void);
void bt_stream_publish_packet(const uint8_t *payload, uint16_t len, uint32_t sec, uint32_t usec);

/** Stop BLE advertising (frees the 2.4 GHz radio for WiFi scanning). */
void bt_stream_pause(void);
/** Restart BLE advertising after bt_stream_pause(). */
void bt_stream_resume(void);
