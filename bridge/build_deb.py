"""
Builds v2x2map-bridge_<version>_all.deb for Debian/Ubuntu.

A .deb is an ar(1) archive containing three members:
  debian-binary   plain text "2.0\\n"
  control.tar.gz  DEBIAN/ metadata
  data.tar.gz     the files to install

No external tools required — pure Python (tarfile + gzip).

Run from V2X2MAP/bridge:
  python build_deb.py
Output: dist/v2x2map-bridge_0.2.1_all.deb
"""

import gzip
import hashlib
import io
import os
import stat
import tarfile
import time

VERSION = "0.2.1"
PACKAGE = "v2x2map-bridge"
ARCH    = "all"

# ── ar archive ───────────────────────────────────────────────────────────────

def _ar_header(name: str, size: int) -> bytes:
    ts = str(int(time.time()))
    return (
        name.encode().ljust(16)[:16]
        + ts.encode().ljust(12)[:12]
        + b"0     "        # uid
        + b"0     "        # gid
        + b"100644  "      # mode
        + str(size).encode().ljust(10)[:10]
        + b"`\n"
    )

def ar_pack(members: list[tuple[str, bytes]]) -> bytes:
    buf = bytearray(b"!<arch>\n")
    for name, data in members:
        buf += _ar_header(name, len(data))
        buf += data
        if len(data) % 2:
            buf += b"\n"   # ar entries must start on even offsets
    return bytes(buf)


# ── tar.gz builder ────────────────────────────────────────────────────────────

def make_targz(entries: list) -> bytes:
    """
    entries is a list of:
      ("./path/to/dir/",   None,          0o755)   → directory
      ("./path/to/file",   bytes_content, 0o644)   → regular file
    """
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        tf = tarfile.open(fileobj=gz, mode="w:")
        for path, content, mode in entries:
            info          = tarfile.TarInfo(name=path)
            info.mtime    = 0
            info.uid      = 0
            info.gid      = 0
            info.uname    = "root"
            info.gname    = "root"
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = mode
                tf.addfile(info)
            else:
                info.mode = mode
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        tf.close()
    return raw.getvalue()


# ── file content helpers ───────────────────────────────────────────────────────

def _read(rel: str) -> bytes:
    with open(rel, "rb") as f:
        return f.read()

def _txt(s: str) -> bytes:
    return s.encode("utf-8")


# ── package metadata ──────────────────────────────────────────────────────────

WRAPPER = _txt("""\
#!/bin/bash
exec python3 /usr/lib/v2x2map/its_g5_bridge.py "$@"
""")

CONTROL = _txt(f"""\
Package: {PACKAGE}
Version: {VERSION}
Architecture: {ARCH}
Maintainer: pit711
Installed-Size: 250
Depends: python3 (>= 3.10), python3-serial, python3-paho-mqtt
Description: V2X2MAP ITS-G5 USB-to-MQTT bridge and live dashboard
 Captures IEEE 802.11p / ITS-G5 frames from an ESP32-C5 via USB serial
 and republishes them to one or more MQTT brokers.
 .
 Includes a live web dashboard (http://127.0.0.1:8080) with:
  - Map view with type-specific vehicle/RSU markers
  - Grouped frame log with filter chips
  - Statistics tab (type breakdown, sparkline, broker status)
  - MAC forensics tab (first/last seen, frame count, CSV export)
  - Runtime PCAP recording
 .
 Config  : ~/.config/v2x2map/v2x2map.cfg  (JSON, created on first run)
 Records : ~/v2x2map/recordings/its5_*.pcap
 .
 Quick start:
   v2x2map-bridge --node-id V2X2MAP:d2cf13ed6293
 .
 Subsequent runs (reads config automatically):
   v2x2map-bridge
""")

POSTINST = _txt("""\
#!/bin/bash
set -e
echo ""
echo "  V2X2MAP ITS-G5 bridge installed."
echo "  Usage:  v2x2map-bridge --node-id V2X2MAP:<12-hex-mac>"
echo "          Dashboard opens at http://127.0.0.1:8080"
echo ""
echo "  Serial port access: add your user to the 'dialout' group:"
echo "    sudo usermod -aG dialout $USER   (re-login required)"
echo ""
""")

PRERM = _txt("""\
#!/bin/bash
set -e
""")

COPYRIGHT = _txt(f"""\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {PACKAGE}
Source: https://github.com/pit711/v2x2map

Files: *
Copyright: 2026 pit711
License: MIT
""")


# ── md5sums ───────────────────────────────────────────────────────────────────

def md5sums(file_entries: list[tuple[str, bytes]]) -> bytes:
    lines = []
    for path, content, _ in file_entries:
        if content is not None:
            h = hashlib.md5(content).hexdigest()
            lines.append(f"{h}  {path.lstrip('./')}")
    return _txt("\n".join(lines) + "\n")


# ── build ─────────────────────────────────────────────────────────────────────

def build():
    os.makedirs("dist", exist_ok=True)

    bridge_py = _read("its_g5_bridge.py")
    server_py = _read("dashboard_server.py")
    dash_html = _read("dashboard.html")

    # Files that will be installed (used for md5sums too)
    data_files = [
        ("./usr/lib/v2x2map/its_g5_bridge.py",   bridge_py, 0o644),
        ("./usr/lib/v2x2map/dashboard_server.py", server_py, 0o644),
        ("./usr/lib/v2x2map/dashboard.html",      dash_html, 0o644),
        ("./usr/bin/v2x2map-bridge",              WRAPPER,   0o755),
        ("./usr/share/doc/v2x2map-bridge/copyright", COPYRIGHT, 0o644),
    ]

    data_entries = [
        ("./",                                   None, 0o755),
        ("./usr/",                               None, 0o755),
        ("./usr/lib/",                           None, 0o755),
        ("./usr/lib/v2x2map/",                   None, 0o755),
        ("./usr/bin/",                           None, 0o755),
        ("./usr/share/",                         None, 0o755),
        ("./usr/share/doc/",                     None, 0o755),
        ("./usr/share/doc/v2x2map-bridge/",      None, 0o755),
    ] + data_files

    ctrl_entries = [
        ("./",          None,    0o755),
        ("./control",   CONTROL, 0o644),
        ("./md5sums",   md5sums(data_files), 0o644),
        ("./postinst",  POSTINST, 0o755),
        ("./prerm",     PRERM,    0o755),
    ]

    control_tgz = make_targz(ctrl_entries)
    data_tgz    = make_targz(data_entries)

    deb = ar_pack([
        ("debian-binary",  b"2.0\n"),
        ("control.tar.gz", control_tgz),
        ("data.tar.gz",    data_tgz),
    ])

    name = f"{PACKAGE}_{VERSION}_{ARCH}.deb"
    out  = os.path.join("dist", name)
    with open(out, "wb") as f:
        f.write(deb)

    kb = len(deb) // 1024
    print(f"Built: {out}  ({kb} KB)")
    print(f"Install with:")
    print(f"  sudo apt install ./{name}")
    print(f"  -- or --")
    print(f"  sudo dpkg -i {name}")


if __name__ == "__main__":
    build()
