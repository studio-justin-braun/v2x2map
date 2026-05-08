package org.opentrafficmap.receiver

import android.content.Context
import android.graphics.drawable.Drawable
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.DrawableCompat
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.Marker

/**
 * Owns the per-frame markers on the OSM map. Markers are coloured by ITS
 * message type and rotated by GeoNetworking heading; CAMs get an arrow,
 * stationary message types get a dot.
 *
 * Markers auto-expire so a long capture session doesn't fill the map with
 * thousands of overlapping pins. CAMs fade fastest (the vehicle has long
 * moved on); DENMs and intersection geometry stick around longer.
 */
class MarkerLayer(private val map: MapView, private val context: Context) {

    private data class Entry(
        val marker: Marker,
        val msgType: ItsG5Decoder.MsgType,
        val createdMs: Long,
    )

    private val entries = ArrayDeque<Entry>()

    fun add(f: Frame) {
        val ll = f.latLon ?: return
        val drawable = makeDrawable(f.msgType, f.headingDeg)
        val m = Marker(map).apply {
            position = GeoPoint(ll.first, ll.second)
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_CENTER)
            icon = drawable
            f.headingDeg?.let { rotation = it.toFloat() }
            title = buildTitle(f)
            snippet = buildSnippet(f)
        }
        map.overlays.add(m)
        entries.addLast(Entry(m, f.msgType, System.currentTimeMillis()))
        prune()
    }

    /** Drop markers that have aged out, plus a hard cap on total count. */
    fun prune() {
        val now = System.currentTimeMillis()
        var changed = false
        while (entries.isNotEmpty()) {
            val e = entries.first()
            val ttl = ttlFor(e.msgType)
            if (now - e.createdMs <= ttl) break
            map.overlays.remove(e.marker)
            entries.removeFirst()
            changed = true
        }
        while (entries.size > MAX_MARKERS) {
            val e = entries.removeFirst()
            map.overlays.remove(e.marker)
            changed = true
        }
        if (changed) map.invalidate()
    }

    fun clear() {
        for (e in entries) map.overlays.remove(e.marker)
        entries.clear()
        map.invalidate()
    }

    private fun ttlFor(t: ItsG5Decoder.MsgType): Long = when (t) {
        ItsG5Decoder.MsgType.CAM    -> 60_000L              // 1 min — vehicle moves
        ItsG5Decoder.MsgType.DENM   -> 5L * 60_000L         // 5 min — hazards stick
        ItsG5Decoder.MsgType.SPATEM,
        ItsG5Decoder.MsgType.MAPEM  -> 10L * 60_000L        // 10 min — geometry rarely changes
        ItsG5Decoder.MsgType.UNKNOWN -> 30_000L
        else                        -> 2L * 60_000L
    }

    private fun makeDrawable(t: ItsG5Decoder.MsgType, heading: Double?): Drawable {
        // Arrow when we have a heading (CAM-like), dot otherwise.
        val resId = if (heading != null && t == ItsG5Decoder.MsgType.CAM)
            R.drawable.ic_marker_arrow
        else
            R.drawable.ic_marker_dot
        val d = ContextCompat.getDrawable(context, resId)!!.mutate()
        DrawableCompat.setTint(d, t.color)
        return d
    }

    private fun buildTitle(f: Frame): String {
        val type = f.msgType.short
        val sid = f.stationId?.let {
            // Take the lower 4 bytes as a short station ID so the popup stays compact.
            "#%08x".format(it.toInt())
        } ?: ""
        return if (sid.isEmpty()) "$type frame #${f.seq}" else "$type $sid"
    }

    private fun buildSnippet(f: Frame): String {
        val (lat, lon) = f.latLon ?: return "len=${f.len}"
        val sb = StringBuilder()
        sb.append("lat=%.6f  lon=%.6f\n".format(lat, lon))
        f.speedMps?.takeIf { it > 0.5 }?.let {
            sb.append("speed=%.1f km/h\n".format(it * 3.6))
        }
        f.headingDeg?.let { sb.append("hdg=%.0f°\n".format(it)) }
        sb.append("len=${f.len}")
        return sb.toString()
    }

    companion object {
        private const val MAX_MARKERS = 500
    }
}
