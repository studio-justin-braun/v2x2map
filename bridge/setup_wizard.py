"""
Tkinter-based first-run setup wizard for the ITS-G5 Receiver bridge.

Steps:
  1. Device  - pick the COM port the C5 is attached to
  2. Flash   - write the bundled firmware via esptool (optional, can be skipped)
  3. Node-ID - read the chip's MAC and use it as the MQTT node-id

On Finish the wizard returns a dict {port, node_id} which the caller turns
into sys.argv for the normal CLI bridge flow.

The flash & MAC-read steps drive esptool.main() in a worker thread and
redirect stdout/stderr into the on-screen log box. esptool calls
sys.exit() at the end of every command, which we catch.
"""

from __future__ import annotations

import io
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import serial.tools.list_ports

# Offsets and filenames must match build/flasher_args.json (ESP32-C5 layout).
FIRMWARE_FILES: list[tuple[str, str]] = [
    ("0x2000",  "bootloader.bin"),
    ("0x8000",  "partition-table.bin"),
    ("0x1e000", "ota_data_initial.bin"),
    ("0x20000", "firmware.bin"),
]
FLASH_BAUD = "921600"
CHIP = "esp32c5"


def _resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


class _UiWriter:
    """File-like that pushes esptool stdout/stderr into a Tk Text widget."""

    def __init__(self, widget: tk.Text, root: tk.Tk):
        self._widget = widget
        self._root = root
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        if "\n" in self._buf or "\r" in self._buf:
            chunk, self._buf = self._buf, ""
            self._root.after(0, self._append, chunk)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            chunk, self._buf = self._buf, ""
            self._root.after(0, self._append, chunk)

    def _append(self, chunk: str) -> None:
        self._widget.insert("end", chunk)
        self._widget.see("end")


class SetupWizard:
    def __init__(self) -> None:
        self.result: dict | None = None

        self.root = tk.Tk()
        self.root.title("ITS-G5 Receiver - Setup")
        self.root.geometry("620x540")
        self.root.minsize(620, 540)

        self.port_var = tk.StringVar()
        self.node_id_var = tk.StringVar()
        self.skip_flash = tk.BooleanVar(value=False)

        self._port_label_to_device: dict[str, str] = {}
        self._busy = False

        self._build()

    # --- UI construction -------------------------------------------------

    def _build(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.nb = nb
        self._build_step1(nb)
        self._build_step2(nb)
        self._build_step3(nb)
        # Lock the tabs so users navigate via Buttons only.
        nb.bind("<<NotebookTabChanged>>", lambda e: None)

    def _build_step1(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=20)
        nb.add(f, text="1. Device")

        ttk.Label(f, text="ITS-G5 Receiver - Setup",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text="Connect the ESP32-C5 via USB and pick the COM port.\n"
                 "If the device is already programmed, you can skip the flash step.",
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(8, 16))

        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Label(row, text="COM port:").pack(side="left")
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var, state="readonly")
        self.port_combo.pack(side="left", padx=(8, 8), fill="x", expand=True)
        ttk.Button(row, text="Refresh", command=self._refresh_ports).pack(side="left")

        ttk.Checkbutton(
            f, text="Skip firmware flash (device is already flashed)",
            variable=self.skip_flash,
        ).pack(anchor="w", pady=(20, 0))

        bar = ttk.Frame(f); bar.pack(side="bottom", fill="x", pady=(20, 0))
        ttk.Button(bar, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(bar, text="Next >", command=self._goto_step2).pack(side="right", padx=(0, 8))

        self._refresh_ports()

    def _build_step2(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=20)
        nb.add(f, text="2. Flash")

        ttk.Label(f, text="Flash firmware", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text="Writes bootloader, partition table and application onto the C5. "
                 "Takes 30-60 seconds depending on the USB connection.",
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(4, 12))

        self.flash_status = ttk.Label(f, text="Ready.")
        self.flash_status.pack(anchor="w", pady=(0, 4))
        self.flash_progress = ttk.Progressbar(f, mode="determinate")
        self.flash_progress.pack(fill="x", pady=(0, 8))

        self.flash_log = scrolledtext.ScrolledText(f, height=12, font=("Consolas", 9), wrap="none")
        self.flash_log.pack(fill="both", expand=True)

        bar = ttk.Frame(f); bar.pack(side="bottom", fill="x", pady=(12, 0))
        self.flash_btn = ttk.Button(bar, text="Start flash", command=self._start_flash)
        self.flash_btn.pack(side="right", padx=(0, 8))
        self.flash_next_btn = ttk.Button(bar, text="Next >", command=self._goto_step3, state="disabled")
        self.flash_next_btn.pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="< Back", command=lambda: self.nb.select(0)).pack(side="right")

    def _build_step3(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=20)
        nb.add(f, text="3. Node-ID")

        ttk.Label(f, text="Set Node-ID", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text="The Node-ID identifies this device in the MQTT topic "
                 "its/<node-id>/packet. Default is the C5's MAC address without colons.",
            wraplength=540, justify="left",
        ).pack(anchor="w", pady=(4, 12))

        self.mac_status = ttk.Label(f, text="MAC not yet read.")
        self.mac_status.pack(anchor="w")
        ttk.Button(f, text="Read MAC from chip", command=self._read_mac).pack(anchor="w", pady=(8, 16))

        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Label(row, text="Node-ID:").pack(side="left")
        self.node_entry = ttk.Entry(row, textvariable=self.node_id_var, width=30)
        self.node_entry.pack(side="left", padx=(8, 0))

        self.mac_log = scrolledtext.ScrolledText(f, height=8, font=("Consolas", 9), wrap="none")
        self.mac_log.pack(fill="both", expand=True, pady=(16, 0))

        bar = ttk.Frame(f); bar.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(bar, text="Finish - start bridge", command=self._finish).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="< Back", command=lambda: self.nb.select(1)).pack(side="right")

    # --- Step navigation -------------------------------------------------

    def _goto_step2(self) -> None:
        if not self._selected_port():
            messagebox.showerror("Error", "Please select a COM port first.")
            return
        if self.skip_flash.get():
            self.nb.select(2)
        else:
            self.nb.select(1)

    def _goto_step3(self) -> None:
        self.nb.select(2)

    def _cancel(self) -> None:
        if self._busy:
            return
        self.result = None
        self.root.destroy()

    def _finish(self) -> None:
        port = self._selected_port()
        node = self.node_id_var.get().strip()
        if not port:
            messagebox.showerror("Error", "No COM port selected.")
            return
        if not re.fullmatch(r"[0-9a-fA-F]{12}", node):
            messagebox.showerror(
                "Error",
                "Node-ID must be a 12-digit hex MAC without colons "
                "(e.g. d2cf13ed6293)."
            )
            return
        self.result = {"port": port, "node_id": node.lower()}
        self.root.destroy()

    # --- COM port handling -----------------------------------------------

    def _refresh_ports(self) -> None:
        ports = serial.tools.list_ports.comports()
        labels: list[str] = []
        self._port_label_to_device.clear()
        for p in ports:
            label = f"{p.device}  -  {p.description}"
            labels.append(label)
            self._port_label_to_device[label] = p.device
        self.port_combo["values"] = labels
        if labels:
            if not self.port_var.get() or self.port_var.get() not in self._port_label_to_device:
                self.port_var.set(labels[0])
        else:
            self.port_var.set("")

    def _selected_port(self) -> str:
        label = self.port_var.get()
        return self._port_label_to_device.get(label, "")

    # --- Flash -----------------------------------------------------------

    def _start_flash(self) -> None:
        if self._busy:
            return
        port = self._selected_port()
        if not port:
            messagebox.showerror("Error", "No COM port selected.")
            return
        for _, name in FIRMWARE_FILES:
            path = _resource_path("firmware", name)
            if not os.path.isfile(path):
                messagebox.showerror(
                    "Error",
                    f"Firmware file missing from bundle: {name}\n({path})"
                )
                return

        self._busy = True
        self.flash_btn.configure(state="disabled")
        self.flash_next_btn.configure(state="disabled")
        self.flash_status.configure(text=f"Flashing firmware to {port} ...")
        self.flash_log.delete("1.0", "end")
        self.flash_progress.configure(mode="indeterminate")
        self.flash_progress.start(60)

        threading.Thread(target=self._flash_worker, args=(port,), daemon=True).start()

    def _flash_worker(self, port: str) -> None:
        args = [
            "--chip", CHIP,
            "--port", port,
            "--baud", FLASH_BAUD,
            "--before", "default-reset",
            "--after", "hard-reset",
            "write_flash",
            "--flash-mode", "dio",
            "--flash-size", "4MB",
            "--flash-freq", "80m",
        ]
        for off, name in FIRMWARE_FILES:
            args += [off, _resource_path("firmware", name)]

        ok, msg = self._run_esptool(args, self.flash_log)
        self.root.after(0, self._on_flash_done, ok, msg)

    def _on_flash_done(self, ok: bool, msg: str) -> None:
        self._busy = False
        self.flash_progress.stop()
        self.flash_progress.configure(mode="determinate", value=100 if ok else 0)
        self.flash_btn.configure(state="normal")
        if ok:
            self.flash_status.configure(text="Flash succeeded.")
            self.flash_next_btn.configure(state="normal")
            self.nb.select(2)
        else:
            self.flash_status.configure(text=f"Flash failed: {msg}")

    # --- Read MAC --------------------------------------------------------

    def _read_mac(self) -> None:
        if self._busy:
            return
        port = self._selected_port()
        if not port:
            messagebox.showerror("Error", "No COM port selected.")
            return
        self._busy = True
        self.mac_status.configure(text=f"Reading MAC from {port} ...")
        self.mac_log.delete("1.0", "end")
        threading.Thread(target=self._mac_worker, args=(port,), daemon=True).start()

    def _mac_worker(self, port: str) -> None:
        args = ["--chip", CHIP, "--port", port, "read_mac"]
        # capture both into the on-screen log AND a string so we can parse it
        buf = io.StringIO()

        class _Tee:
            def __init__(self, w):
                self.w = w
            def write(self, s):
                buf.write(s)
                return self.w.write(s)
            def flush(self):
                self.w.flush()

        ok, msg = self._run_esptool(args, self.mac_log, tee=_Tee)
        text = buf.getvalue()
        m = re.search(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", text)
        mac = m.group(1) if m else None
        self.root.after(0, self._on_mac_done, ok, msg, mac)

    def _on_mac_done(self, ok: bool, msg: str, mac: str | None) -> None:
        self._busy = False
        if ok and mac:
            self.mac_status.configure(text=f"MAC: {mac}")
            self.node_id_var.set(mac.replace(":", "").lower())
        elif ok:
            self.mac_status.configure(text="MAC not found in esptool output.")
        else:
            self.mac_status.configure(text=f"Reading MAC failed: {msg}")

    # --- esptool runner --------------------------------------------------

    def _run_esptool(self, args: list[str], log_widget: tk.Text, tee=None) -> tuple[bool, str]:
        """
        Drive esptool.main() in this thread, piping its stdout/stderr into
        `log_widget`. esptool always raises SystemExit at the end - we treat
        code 0 / None as success.
        """
        writer = _UiWriter(log_widget, self.root)
        sink = tee(writer) if tee else writer
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = sink
        try:
            try:
                import esptool
            except ImportError as e:
                return False, f"esptool not in bundle: {e}"
            try:
                esptool.main(args)
            except SystemExit as exc:
                code = exc.code
                if code in (0, None):
                    return True, ""
                return False, f"esptool exit {code}"
            except Exception as exc:  # esptool.FatalError, serial errors, etc.
                return False, str(exc)
            return True, ""
        finally:
            try:
                sink.flush()
            except Exception:
                pass
            sys.stdout, sys.stderr = old_out, old_err

    # --- run -------------------------------------------------------------

    def run(self) -> dict | None:
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.root.mainloop()
        return self.result


def run_wizard() -> dict | None:
    return SetupWizard().run()
