#include "usb_stream.h"

#include <stdint.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#include "led.h"

static const char TAG[] = "USB_STREAM";
static bool s_ready;

void usb_stream_init(void)
{
    if (s_ready) return;

    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = 4096;
    cfg.rx_buffer_size = 256;

    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err == ESP_OK || err == ESP_ERR_INVALID_STATE) {
        s_ready = true;
    } else {
        ESP_LOGE(TAG, "usb_serial_jtag_driver_install failed: %s", esp_err_to_name(err));
    }
}

void usb_stream_publish_packet(const uint8_t *payload, uint16_t len, uint32_t sec, uint32_t usec)
{
    if (!s_ready) usb_stream_init();
    if (!s_ready) return;

    uint8_t hdr[14] = {
        'I', 'T', 'S', '5',
        (uint8_t)(sec  & 0xff), (uint8_t)((sec  >>  8) & 0xff),
        (uint8_t)((sec >> 16) & 0xff), (uint8_t)((sec >> 24) & 0xff),
        (uint8_t)(usec & 0xff), (uint8_t)((usec >>  8) & 0xff),
        (uint8_t)((usec >> 16) & 0xff), (uint8_t)((usec >> 24) & 0xff),
        (uint8_t)(len  & 0xff), (uint8_t)((len  >>  8) & 0xff),
    };

    usb_serial_jtag_write_bytes(hdr, sizeof(hdr), pdMS_TO_TICKS(50));
    if (len > 0) {
        usb_serial_jtag_write_bytes(payload, len, pdMS_TO_TICKS(50));
    }
    led_pulse_usb_tx();
}

void usb_stream_send_test_frame(void)
{
    static const uint8_t test_payload[16] = {
        0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02, 0x03, 0x04,
        0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C
    };
    usb_stream_publish_packet(test_payload, sizeof(test_payload), 1234567890U, 999U);
}
