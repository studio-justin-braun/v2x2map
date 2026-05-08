package org.opentrafficmap.receiver

import android.content.Context
import android.util.Log
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Append-only file recorder for received frames. Writes the same wire format
 * as the firmware (`magic[4] + sec/usec/len + payload`) so a captured file
 * can be replayed by `bridge/its_g5_bridge.py:FrameReader` or by the app's
 * own [FrameReader] for testing.
 *
 * One file per recording session, in `getExternalFilesDir(null)` so no extra
 * permission is needed and the user can pull it via USB / file manager from
 * `Android/data/org.opentrafficmap.receiver/files/`.
 */
class FrameRecorder(private val context: Context) {

    private val tag = "FrameRecorder"
    private var stream: BufferedOutputStream? = null
    var file: File? = null
        private set
    var frameCount: Int = 0
        private set

    val isRecording: Boolean
        get() = stream != null

    /** Open a new file. Returns the file or null on failure. */
    fun start(): File? {
        if (stream != null) return file
        val dir = context.getExternalFilesDir(null) ?: context.filesDir
        if (!dir.exists()) dir.mkdirs()
        val ts = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        val f = File(dir, "frames-$ts.itsg5")
        return try {
            stream = BufferedOutputStream(FileOutputStream(f))
            file = f
            frameCount = 0
            Log.i(tag, "recording to ${f.absolutePath}")
            f
        } catch (e: Exception) {
            Log.w(tag, "open failed", e)
            null
        }
    }

    fun stop(): File? {
        val s = stream ?: return null
        return try {
            s.flush()
            s.close()
            stream = null
            val out = file
            Log.i(tag, "stopped after $frameCount frames")
            out
        } catch (e: Exception) {
            Log.w(tag, "close failed", e)
            null
        }
    }

    @Synchronized
    fun append(frame: Frame) {
        val s = stream ?: return
        try {
            val len = frame.payload.size
            val hdr = ByteBuffer.allocate(14).order(ByteOrder.LITTLE_ENDIAN)
            hdr.put('I'.code.toByte()); hdr.put('T'.code.toByte())
            hdr.put('S'.code.toByte()); hdr.put('5'.code.toByte())
            hdr.putInt(frame.sec.toInt())
            hdr.putInt(frame.usec.toInt())
            hdr.putShort(len.toShort())
            s.write(hdr.array())
            s.write(frame.payload)
            frameCount++
        } catch (e: Exception) {
            Log.w(tag, "write failed, closing", e)
            stop()
        }
    }
}
