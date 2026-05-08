package org.opentrafficmap.receiver

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import androidx.core.content.ContextCompat
import java.util.UUID

/**
 * BLE counterpart of [UsbSerialController]. Scans for the firmware's GATT service,
 * subscribes to the notify characteristic, and feeds the raw byte stream into the
 * existing [FrameReader] via `onBytes` — the wire format is identical to USB.
 *
 * UUIDs must match `main/bt_stream.c` on the device:
 *   Service        b6e57e90-12d8-4a47-9b21-3f0000000001
 *   Notify chr     b6e57e90-12d8-4a47-9b21-3f0000000002
 *   Config chr     b6e57e90-12d8-4a47-9b21-3f0000000003 (4 × u16 LE)
 */
class BluetoothController(
    private val context: Context,
    private val onBytes: (ByteArray) -> Unit,
    private val onState: (State, String?) -> Unit,
    private val onConfig: ((CycleConfig) -> Unit)? = null,
) {
    enum class State { IDLE, SCANNING, CONNECTING, CONNECTED, ERROR }

    /** Sniffer/BLE time-slice in milliseconds for both link states. */
    data class CycleConfig(
        val discSniffMs: Int,
        val discBleMs:   Int,
        val connSniffMs: Int,
        val connBleMs:   Int,
    )

    private val tag = "BluetoothController"
    private val mainHandler = Handler(Looper.getMainLooper())

    private val btManager =
        context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
    private val adapter: BluetoothAdapter? = btManager.adapter

    private var gatt: BluetoothGatt? = null
    @Volatile private var scanning = false
    @Volatile private var stopped = false  // user pressed Disconnect — don't auto-retry

    /** Public entry point. Caller must have the relevant runtime permissions. */
    @SuppressLint("MissingPermission")
    fun start() {
        stopped = false
        if (adapter == null || !adapter.isEnabled) {
            onState(State.ERROR, "Bluetooth unavailable or disabled")
            return
        }
        if (!hasScanPermission()) {
            onState(State.ERROR, "BLE permission missing")
            return
        }
        val scanner = adapter.bluetoothLeScanner ?: run {
            onState(State.ERROR, "BLE scanner unavailable")
            return
        }

        // No hardware filter — Android's serviceUuid filter matches AD only on
        // older stacks (Galaxy S6 etc.), and our firmware places the 128-bit
        // UUID in the scan response to keep the AD under 31 B. We match in
        // software in onScanResult instead.
        // MATCH_MODE_AGGRESSIVE + MATCH_NUM_FEW: Samsung-S6-AOSP defaults are
        // "sticky/strong-only" and silently filter sporadic adv from anything
        // it hasn't seen for a while — ITS-G5-RX falls under that.
        val settingsBuilder = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            settingsBuilder
                .setMatchMode(ScanSettings.MATCH_MODE_AGGRESSIVE)
                .setNumOfMatches(ScanSettings.MATCH_NUM_FEW_ADVERTISEMENT)
                .setCallbackType(ScanSettings.CALLBACK_TYPE_ALL_MATCHES)
        }
        val settings = settingsBuilder.build()

        scanning = true
        onState(State.SCANNING, "Scanning for $DEVICE_NAME…")
        try {
            scanner.startScan(null, settings, scanCallback)
        } catch (e: SecurityException) {
            onState(State.ERROR, "Scan denied: ${e.message}")
            scanning = false
            return
        }

        // Stop scan after 15 s if nothing was found.
        mainHandler.postDelayed({
            if (scanning) {
                stopScan()
                onState(State.ERROR, "$DEVICE_NAME not found")
            }
        }, SCAN_TIMEOUT_MS)
    }

    @SuppressLint("MissingPermission")
    fun stop() {
        stopped = true
        stopScan()
        try {
            gatt?.disconnect()
            gatt?.close()
        } catch (_: Exception) {}
        gatt = null
        onState(State.IDLE, null)
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        try {
            adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        } catch (_: Exception) {}
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            if (!scanning) return
            val record = result.scanRecord
            val name = result.device.name ?: record?.deviceName
            val uuids = record?.serviceUuids?.map { it.uuid } ?: emptyList()
            val match = name == DEVICE_NAME || SERVICE_UUID in uuids
            if (!match) return
            stopScan()
            val device = result.device
            val label = name ?: device.address
            onState(State.CONNECTING, "Connecting to $label…")
            try {
                gatt = device.connectGatt(context, false, gattCallback,
                    BluetoothDevice.TRANSPORT_LE)
            } catch (e: SecurityException) {
                onState(State.ERROR, "Connect denied: ${e.message}")
            }
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            onState(State.ERROR, "Scan-Fehler $errorCode")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Log.i(tag, "GATT connected, requesting MTU")
                g.requestMtu(517)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                Log.i(tag, "GATT disconnected status=$status")
                try { g.close() } catch (_: Exception) {}
                gatt = null
                /* C5 firmware time-slices the radio with the Wi-Fi sniffer.
                 * Disconnects during sniffer windows are expected — auto-rescan
                 * unless the user explicitly hit Disconnect.
                 *
                 * 2 s delay before retrying: status=133 (GATT_ERROR) on the
                 * Android side is a classic race when connectGatt fires before
                 * the previous stack has finished tearing down. Plus the C5
                 * needs a moment to switch from sniff back to BLE adv. */
                if (stopped) {
                    onState(State.IDLE, null)
                } else {
                    onState(State.SCANNING, "Reconnect …")
                    mainHandler.postDelayed({ if (!stopped) start() }, 2000)
                }
            }
        }

        @SuppressLint("MissingPermission")
        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i(tag, "MTU=$mtu status=$status")
            g.discoverServices()
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                onState(State.ERROR, "Service-Discovery failed: $status")
                return
            }
            val service = g.getService(SERVICE_UUID) ?: run {
                onState(State.ERROR, "Service not found")
                return
            }
            val chr = service.getCharacteristic(NOTIFY_CHR_UUID) ?: run {
                onState(State.ERROR, "Notify characteristic missing")
                return
            }
            if (!g.setCharacteristicNotification(chr, true)) {
                onState(State.ERROR, "setCharacteristicNotification fehlgeschlagen")
                return
            }
            val cccd = chr.getDescriptor(CCCD_UUID) ?: run {
                onState(State.ERROR, "CCCD missing")
                return
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            } else {
                @Suppress("DEPRECATION")
                cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                @Suppress("DEPRECATION")
                g.writeDescriptor(cccd)
            }
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorWrite(
            g: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int
        ) {
            if (status == BluetoothGatt.GATT_SUCCESS &&
                descriptor.uuid == CCCD_UUID) {
                onState(State.CONNECTED, g.device.name ?: g.device.address)
                /* Pull current config so the settings dialog has fresh values
                 * when the user opens it. */
                val service = g.getService(SERVICE_UUID)
                val cfg = service?.getCharacteristic(CONFIG_CHR_UUID)
                if (cfg != null) g.readCharacteristic(cfg)
            } else if (status != BluetoothGatt.GATT_SUCCESS) {
                onState(State.ERROR, "CCCD write status=$status")
            }
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int
        ) {
            if (status == BluetoothGatt.GATT_SUCCESS && characteristic.uuid == CONFIG_CHR_UUID) {
                characteristic.value?.let { decodeConfig(it)?.let { c -> onConfig?.invoke(c) } }
            }
        }

        override fun onCharacteristicRead(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
            status: Int
        ) {
            if (status == BluetoothGatt.GATT_SUCCESS && characteristic.uuid == CONFIG_CHR_UUID) {
                decodeConfig(value)?.let { onConfig?.invoke(it) }
            }
        }

        override fun onCharacteristicWrite(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int
        ) {
            if (status != BluetoothGatt.GATT_SUCCESS && characteristic.uuid == CONFIG_CHR_UUID) {
                Log.w(tag, "config write failed: status=$status")
            }
        }

        // Android < 13 (Tiramisu) path
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic
        ) {
            if (characteristic.uuid == NOTIFY_CHR_UUID) {
                characteristic.value?.let { onBytes(it) }
            }
        }

        // Android 13+ path with explicit value
        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            if (characteristic.uuid == NOTIFY_CHR_UUID) {
                onBytes(value)
            }
        }
    }

    /** Push new cycle parameters to the device. Returns false if not connected. */
    @SuppressLint("MissingPermission")
    fun writeConfig(c: CycleConfig): Boolean {
        val g = gatt ?: return false
        val service = g.getService(SERVICE_UUID) ?: return false
        val chr = service.getCharacteristic(CONFIG_CHR_UUID) ?: return false
        val payload = ByteArray(8)
        fun pack(off: Int, v: Int) {
            val clamped = v.coerceIn(100, 60000)
            payload[off]     = (clamped and 0xff).toByte()
            payload[off + 1] = ((clamped shr 8) and 0xff).toByte()
        }
        pack(0, c.discSniffMs); pack(2, c.discBleMs)
        pack(4, c.connSniffMs); pack(6, c.connBleMs)
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                g.writeCharacteristic(chr, payload, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) ==
                    BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION") run {
                    chr.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                    chr.value = payload
                    g.writeCharacteristic(chr)
                }
            }
        } catch (_: SecurityException) { false }
    }

    private fun decodeConfig(bytes: ByteArray): CycleConfig? {
        if (bytes.size != 8) return null
        fun u16(off: Int) = (bytes[off].toInt() and 0xff) or
                            ((bytes[off + 1].toInt() and 0xff) shl 8)
        return CycleConfig(u16(0), u16(2), u16(4), u16(6))
    }

    private fun hasScanPermission(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_SCAN) ==
                PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.BLUETOOTH_CONNECT) ==
                PackageManager.PERMISSION_GRANTED
        } else {
            ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED
        }
    }

    companion object {
        const val DEVICE_NAME = "ITS-G5-RX"
        private const val SCAN_TIMEOUT_MS = 15_000L
        val SERVICE_UUID: UUID = UUID.fromString("b6e57e90-12d8-4a47-9b21-3f0000000001")
        val NOTIFY_CHR_UUID: UUID = UUID.fromString("b6e57e90-12d8-4a47-9b21-3f0000000002")
        val CONFIG_CHR_UUID: UUID = UUID.fromString("b6e57e90-12d8-4a47-9b21-3f0000000003")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        fun runtimePermissions(): Array<String> =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                arrayOf(
                    Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.BLUETOOTH_CONNECT,
                )
            } else {
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
            }
    }
}
