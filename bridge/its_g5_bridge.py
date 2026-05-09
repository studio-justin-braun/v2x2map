"""
ITS-G5 Receiver USB-to-MQTT bridge.

Reads framed packets from an ESP32-C5 connected via USB-Serial-JTAG and
republishes them to the OpenTrafficMap MQTT server, mimicking what the
on-device ethernet+MQTT path would do if the receiver had a wired uplink.

Frame format on the wire (little-endian, no padding):
    magic[4]  = b"ITS5"
    sec       u32   epoch seconds (or arbitrary timestamp from rx_ctrl)
    usec      u32   microseconds
    len       u16   payload length in bytes
    payload   <len> raw 802.11 MAC frame

Run:  python its_g5_bridge.py --port COM7 --node-id d2cf13ed6293
"""

import argparse
import logging
import ssl
import struct
import sys
import threading
import time

import paho.mqtt.client as mqtt
import serial

import dashboard_server

MAGIC = b"ITS5"
HEADER_FMT = "<4sIIH"
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 14

DEFAULT_BROKER_HOST = "cits1.opentrafficmap.org"
DEFAULT_BROKER_PORT = 8883


def sniff_ether_type(payload: bytes) -> int | None:
    """
    Try to extract the EtherType from an 802.11 MAC frame so the dashboard
    can colour ITS-G5 (0x8947) differently from regular WLAN noise.

    Layout: [802.11 MAC header (24 or 26 B for QoS)] [LLC/SNAP (8 B): AA AA 03 00 00 00 <ET hi> <ET lo>] ...

    Returns the EtherType as an int, or None if we can't find a SNAP header.
    """
    for hdr_len in (24, 26, 30, 32):  # base / +QoS / +4-addr / both
        if len(payload) < hdr_len + 8:
            continue
        snap = payload[hdr_len:hdr_len + 8]
        if snap[:6] == b"\xaa\xaa\x03\x00\x00\x00":
            return (snap[6] << 8) | snap[7]
    return None


# ETSI TS 102 894-2 §A.74 StationType
STATION_TYPES = {
    0: "unknown",
    1: "pedestrian",
    2: "cyclist",
    3: "moped",
    4: "motorcycle",
    5: "passenger car",
    6: "bus",
    7: "light truck",
    8: "heavy truck",
    9: "trailer",
    10: "special vehicle",
    11: "tram",
    15: "RSU",
}


def _looks_like_gn_common_header(p: bytes, off: int) -> bool:
    """
    Heuristic: does the 8-byte window at `off` look like a GN common header
    (ETSI EN 302 636-4-1 §9.7)? Used to find the common header even when the
    frame is wrapped in an IEEE 1609.2 SecuredData envelope.
    """
    if off + 8 > len(p):
        return False
    if (p[off] & 0x0F) != 0:        # reserved low nibble of NH byte
        return False
    ht = p[off + 1] >> 4
    if ht < 1 or ht > 6:            # 1=BEACON, 2=GUC, 3=GAC, 4=GBC, 5=TSB, 6=LS
        return False
    if p[off + 2] > 7:               # traffic class
        return False
    pl = (p[off + 4] << 8) | p[off + 5]
    if pl < 4 or pl > 1500:          # payload length
        return False
    if p[off + 6] < 1 or p[off + 6] > 10:  # max hop limit
        return False
    return p[off + 7] == 0           # reserved


def decode_geonet_source(payload: bytes) -> dict | None:
    """
    Best-effort decode of the GeoNetworking source long-position vector
    (ETSI EN 302 636-4-1 §9.5) from an ITS-G5 frame.

    Frame layout on the wire:
        [802.11 MAC header   24 / 26 / 30 / 32 B]
        [LLC/SNAP             8 B: AA AA 03 00 00 00 89 47]
        [GN basic header      4 B  — NH=1 unsecured, NH=2 secured (1609.2)]
        [either GN common header directly, or an IEEE 1609.2 SignedData
         envelope whose `unsecuredData` octet string carries the common
         header.]

    Because signed CAMs are the norm, the common header is *not* always at
    a fixed offset. We scan forward from after the basic header for the
    common-header byte signature; the source long-position vector then
    lives at common_off+8.

    Source long-position vector (24 B):
        GnAddress  8 B  (M|ST|country|MAC48)
        timestamp  4 B  (BE)
        latitude   4 B  (int32 BE, 1/10 µdeg)
        longitude  4 B  (int32 BE, 1/10 µdeg)
        PAI/spd/hdg 4 B (1+15+16 bits BE)
    """
    for hdr_len in (24, 26, 30, 32):
        if len(payload) < hdr_len + 12 + 32:
            continue
        if payload[hdr_len:hdr_len + 6] != b"\xaa\xaa\x03\x00\x00\x00":
            continue
        ether_type = (payload[hdr_len + 6] << 8) | payload[hdr_len + 7]
        if ether_type != 0x8947:
            continue
        scan_start = hdr_len + 8 + 4   # past 802.11 + SNAP + GN basic header
        scan_end = len(payload) - 32   # need 8 (common hdr) + 24 (SO_PV)
        for off in range(scan_start, scan_end + 1):
            if not _looks_like_gn_common_header(payload, off):
                continue
            so_pv = off + 8
            gn_addr = payload[so_pv:so_pv + 8]
            st = (gn_addr[0] >> 2) & 0x1F
            mac = gn_addr[2:8]
            ts = int.from_bytes(payload[so_pv + 8:so_pv + 12], "big")
            lat_raw = int.from_bytes(payload[so_pv + 12:so_pv + 16], "big", signed=True)
            lon_raw = int.from_bytes(payload[so_pv + 16:so_pv + 20], "big", signed=True)
            spd_hdg = int.from_bytes(payload[so_pv + 20:so_pv + 24], "big")
            lat = lat_raw / 1e7
            lon = lon_raw / 1e7
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            speed_raw = (spd_hdg >> 16) & 0x7FFF
            if speed_raw & 0x4000:  # sign-extend 15-bit two's complement
                speed_raw -= 0x8000
            return {
                "gn_addr": ":".join(f"{b:02x}" for b in mac),
                "station_type": st,
                "station_type_label": STATION_TYPES.get(st, f"type {st}"),
                "lat": lat,
                "lon": lon,
                "speed_mps": speed_raw * 0.01,
                "heading_deg": (spd_hdg & 0xFFFF) * 0.1,
                "pai": (spd_hdg >> 31) & 0x1,
                "gn_ts": ts,
            }
    return None


class FrameReader:
    """Stateful resync parser: scans a byte stream for ITS5 frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        self._buf.extend(chunk)
        while True:
            idx = self._buf.find(MAGIC)
            if idx < 0:
                # No magic at all — drop everything except the trailing 3 bytes
                # (a magic could be split across reads).
                if len(self._buf) > 3:
                    del self._buf[: len(self._buf) - 3]
                return
            if idx > 0:
                del self._buf[:idx]  # drop pre-magic noise (logs, ROM output)
            if len(self._buf) < HEADER_LEN:
                return  # not enough for header yet
            _, sec, usec, plen = struct.unpack_from(HEADER_FMT, self._buf, 0)
            if plen > 4096:
                # Implausible length — false-positive magic. Skip past it.
                del self._buf[:4]
                continue
            if len(self._buf) < HEADER_LEN + plen:
                return  # wait for full payload
            payload = bytes(self._buf[HEADER_LEN : HEADER_LEN + plen])
            del self._buf[: HEADER_LEN + plen]
            yield sec, usec, payload


def build_mqtt_client(node_id: str, host: str, port: int, status_topic: str):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"its-g5-bridge-{node_id}",
        protocol=mqtt.MQTTv311,
    )
    client.will_set(status_topic, payload=b"offline", qos=1, retain=True)
    ctx = ssl.create_default_context()
    client.tls_set_context(ctx)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            logging.info("MQTT connected to %s:%d", host, port)
            c.publish(status_topic, b"online", qos=1, retain=True)
        else:
            logging.error("MQTT connect failed: %s", reason_code)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties=None):
        logging.warning("MQTT disconnected: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(host, port, keepalive=60)
    client.loop_start()
    return client


def _maybe_run_wizard():
    """If launched without args (typical double-click of the frozen exe) or
    with --setup, show the GUI wizard and translate its result back into
    sys.argv so the rest of main() runs unchanged."""
    argv = sys.argv[1:]
    wants_wizard = ("--setup" in argv) or (not argv and getattr(sys, "frozen", False))
    if not wants_wizard:
        return
    try:
        from setup_wizard import run_wizard
    except Exception as e:
        logging.error("setup wizard not loadable: %s", e)
        return
    result = run_wizard()
    if not result:
        sys.exit(0)
    sys.argv = [sys.argv[0],
                "--port", result["port"],
                "--node-id", result["node_id"],
                "--reset-on-start",
                "--open-browser",
                "--verbose"]


def main():
    _maybe_run_wizard()

    p = argparse.ArgumentParser(description="ITS-G5 USB->MQTT bridge")
    p.add_argument("--port", default="COM7", help="serial port (e.g. COM7)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--node-id", required=True, help="device MAC without colons, e.g. d2cf13ed6293")
    p.add_argument("--setup", action="store_true", help="run the GUI setup wizard (also implied when the exe is launched without arguments)")
    p.add_argument("--broker-host", default=DEFAULT_BROKER_HOST)
    p.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    p.add_argument("--no-mqtt", action="store_true", help="parse only, don't publish (for testing)")
    p.add_argument("--reset-on-start", action="store_true", help="pulse RTS to reboot the device before reading")
    p.add_argument("--exit-after", type=float, default=0, help="exit after N seconds (0 = run forever)")
    p.add_argument("--dashboard-port", type=int, default=8080,
                   help="local HTTP port for the live dashboard (0 to disable)")
    p.add_argument("--open-browser", action="store_true",
                   help="open the dashboard URL in the default browser on start")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    packet_topic = f"its/{args.node_id}/packet"
    status_topic = f"its/{args.node_id}/status"

    client = None
    if not args.no_mqtt:
        client = build_mqtt_client(args.node_id, args.broker_host, args.broker_port, status_topic)
    dashboard_server.set_mqtt_state(enabled=not args.no_mqtt, available=client is not None)

    if args.dashboard_port:
        dashboard_server.start(args.dashboard_port, node_id=args.node_id)
        url = f"http://127.0.0.1:{args.dashboard_port}"
        logging.info("open the dashboard:  %s", url)
        if args.open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception as e:
                logging.warning("could not open browser: %s", e)

    logging.info("Opening %s @ %d baud", args.port, args.baud)
    ser = serial.Serial(args.port, args.baud, timeout=0.5)
    # pyserial sets DTR=RTS=True on open; on ESP32 USB-Serial-JTAG that puts
    # the chip into bootloader/download mode and silences app output. Force
    # both low so the app keeps running.
    ser.dtr = False
    ser.rts = False
    time.sleep(0.05)

    if args.reset_on_start:
        logging.info("Pulsing RTS to reset device")
        ser.rts = True
        time.sleep(0.1)
        ser.rts = False
        time.sleep(0.05)
        ser.reset_input_buffer()

    reader = FrameReader()
    n_frames = 0
    last_log = time.monotonic()
    deadline = time.monotonic() + args.exit_after if args.exit_after > 0 else None

    try:
        while True:
            if deadline and time.monotonic() >= deadline:
                logging.info("exit-after deadline reached")
                break
            chunk = ser.read(4096)
            if not chunk:
                continue
            logging.debug("read %d bytes: %s", len(chunk), chunk[:32].hex())
            for sec, usec, payload in reader.feed(chunk):
                n_frames += 1
                ether_type = sniff_ether_type(payload)
                gn = decode_geonet_source(payload) if ether_type == 0x8947 else None
                logging.debug(
                    "Frame #%d  sec=%d usec=%d len=%d  ET=%s  first8=%s%s",
                    n_frames, sec, usec, len(payload),
                    f"0x{ether_type:04x}" if ether_type is not None else "?",
                    payload[:8].hex(),
                    f"  pos={gn['lat']:.6f},{gn['lon']:.6f} v={gn['speed_mps']*3.6:.1f}km/h h={gn['heading_deg']:.0f}° src={gn['gn_addr']}" if gn else "",
                )
                if args.dashboard_port:
                    evt = {
                        "n": n_frames,
                        "sec": sec,
                        "usec": usec,
                        "len": len(payload),
                        "ether_type": ether_type,
                        "hex": payload[:48].hex(),
                    }
                    if gn is not None:
                        evt.update(gn)
                    dashboard_server.broadcast_frame(evt)
                if client is not None and dashboard_server.is_mqtt_enabled():
                    info = client.publish(packet_topic, payload, qos=0, retain=False)
                    logging.info("published frame #%d to %s (rc=%s, mid=%s, len=%d)",
                                 n_frames, packet_topic, info.rc, info.mid, len(payload))
            now = time.monotonic()
            if now - last_log >= 5.0:
                logging.info("frames seen so far: %d", n_frames)
                last_log = now
    except KeyboardInterrupt:
        logging.info("Stopping (frames=%d)", n_frames)
    finally:
        if client is not None:
            client.publish(status_topic, b"offline", qos=1, retain=True)
            client.loop_stop()
            client.disconnect()
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
