package org.opentrafficmap.receiver

import android.app.Dialog
import android.content.Context
import android.view.LayoutInflater
import android.widget.Button
import android.widget.EditText
import androidx.appcompat.app.AlertDialog

/**
 * Modal that lets the user tune the firmware's sniffer/BLE coex cycle.
 * Reads the current values when shown, writes back via [BluetoothController.writeConfig]
 * when the user hits Save.
 *
 * Presets:
 *   Wardriving:  8000 / 400  /  8000 / 400 — maximize sniffer airtime
 *   Stationary: 10000 / 2000 /  800 / 400  — default, easy to discover
 *   Pairing:    3000 / 1500 / 1500 / 1500  — extra adv visibility
 */
object CycleSettingsDialog {

    private val WARDRIVING = BluetoothController.CycleConfig(8000, 400, 8000, 400)
    private val STATIONARY = BluetoothController.CycleConfig(10000, 2000, 800, 400)
    private val PAIRING    = BluetoothController.CycleConfig(3000, 1500, 1500, 1500)

    fun show(
        context: Context,
        current: BluetoothController.CycleConfig?,
        onSave: (BluetoothController.CycleConfig) -> Unit,
    ): Dialog {
        val view = LayoutInflater.from(context).inflate(R.layout.dialog_cycle_settings, null)
        val edDiscSniff = view.findViewById<EditText>(R.id.edDiscSniff)
        val edDiscBle   = view.findViewById<EditText>(R.id.edDiscBle)
        val edConnSniff = view.findViewById<EditText>(R.id.edConnSniff)
        val edConnBle   = view.findViewById<EditText>(R.id.edConnBle)

        fun fill(c: BluetoothController.CycleConfig) {
            edDiscSniff.setText(c.discSniffMs.toString())
            edDiscBle.setText(c.discBleMs.toString())
            edConnSniff.setText(c.connSniffMs.toString())
            edConnBle.setText(c.connBleMs.toString())
        }
        fill(current ?: STATIONARY)

        view.findViewById<Button>(R.id.presetWardriving).setOnClickListener { fill(WARDRIVING) }
        view.findViewById<Button>(R.id.presetStationary).setOnClickListener { fill(STATIONARY) }
        view.findViewById<Button>(R.id.presetPairing).setOnClickListener   { fill(PAIRING) }

        return AlertDialog.Builder(context)
            .setTitle(R.string.cycle_settings)
            .setView(view)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                fun read(et: EditText): Int =
                    et.text.toString().toIntOrNull()?.coerceIn(100, 60000) ?: 1000
                onSave(BluetoothController.CycleConfig(
                    read(edDiscSniff), read(edDiscBle),
                    read(edConnSniff), read(edConnBle),
                ))
            }
            .show()
    }
}
