#pragma once

#include <stdint.h>

void usb_stream_init(void);
void usb_stream_publish_packet(const uint8_t *payload, uint16_t len, uint32_t sec, uint32_t usec);
void usb_stream_send_test_frame(void);
