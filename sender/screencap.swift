// screencap.swift - tile capture helper for Device Cast ("tiles" mode).
//
// Captures one display with ScreenCaptureKit, which reports the changed regions of each frame.
// On request it returns only those regions, merged on a 64 px grid and JPEG-encoded on the GPU.
// Requests arrive only when the receiver is ready, so on a slow network changes coalesce into a
// single update instead of queueing up, and latency does not grow.
//
// Commands (stdin, one per line):   next | next N | full | probe | quit
//   N is the motion level 0-3 chosen by the server; higher levels downscale moving regions more.
// Response on stdout, binary, big-endian:
//   f64 sender time in ms | u16 width | u16 height | u32 tile count
//   per tile: u16 x | u16 y | u16 w | u16 h | u32 JPEG bytes | JPEG
// With no changes within 1 s the response is the header only. A trailing tile with 0 JPEG bytes
// marks the region detected as video playback. "probe" returns the header plus that tile, if any.
//
// Usage: screencap --display N [--width W --height H] [--fps F] [--quality Q] [--full-quality Q]
//                  [--no-downscale]   (always send moving regions at full resolution)
//                  [--cursor-fd FD]
//        screencap --test | --test-scroll | --test-video    (synthetic source, no screen capture)
import AppKit
import CoreGraphics
import CoreImage
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import ScreenCaptureKit

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write("screencap: \(msg)\n".data(using: .utf8)!)
    exit(1)
}

func note(_ msg: String) {
    FileHandle.standardError.write("screencap: \(msg)\n".data(using: .utf8)!)
}

// MARK: - Arguments

var displayIndex = 0
var outW = 0, outH = 0
var fps = 30
var tileQuality = 0.8, fullQuality = 0.6
var testMode = false
var cursorFD: Int32 = -1     // cursor position and shape are written here when set
var testScroll = false   // whole screen changes every frame, like scrolling
var testVideo = false    // a fixed region changes every frame, like a video in a window
var downscaleMotion = true   // send large moving regions downscaled, then sharp once still
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    func val() -> String {
        if args.isEmpty { fail("missing value for \(a)") }
        return args.removeFirst()
    }
    switch a {
    case "--display": displayIndex = Int(val()) ?? 0
    case "--width": outW = Int(val()) ?? 0
    case "--height": outH = Int(val()) ?? 0
    case "--fps": fps = Int(val()) ?? 30
    case "--quality": tileQuality = Double(val()) ?? 0.8
    case "--full-quality": fullQuality = Double(val()) ?? 0.6
    case "--no-downscale": downscaleMotion = false
    case "--test": testMode = true
    case "--cursor-fd": cursorFD = Int32(val()) ?? -1
    case "--test-scroll": testMode = true; testScroll = true
    case "--test-video": testMode = true; testVideo = true
    default: fail("unknown argument \(a)")
    }
}

// MARK: - Shared state

final class State {
    let cond = NSCondition()
    var framesIn = 0, framesNoRects = 0, framesEmptyRects = 0, rectsIn = 0, commands = 0, tilesOut = 0
    var halfResOut = 0, refinesOut = 0
    var level = 0   // 0-3, set by the server from network conditions
    var refineRects: [CGRect] = []   // regions sent downscaled, resent sharp once they stop changing
    var needsRefine: Bool { !refineRects.isEmpty }
    var latest: CVPixelBuffer?
    var dirty: [CGRect] = []
    var width = 0, height = 0
    var failed: String?
    // Video detection
    var heat: [Float] = []          // per cell: moving average of "changed in this frame"
    var heatCols = 0, heatRows = 0
    var frameTimes: [Double] = []   // timestamps of recent frames with changes
    var videoRect: CGRect?

    func updateVideoDetection(_ rects: [CGRect]?) {
        let cols = (width + 63) / 64, rows = (height + 63) / 64
        if cols != heatCols || rows != heatRows { heat = [Float](repeating: 0, count: cols * rows); heatCols = cols; heatRows = rows }
        var changed = [Bool](repeating: rects == nil, count: cols * rows)
        if let rs = rects {
            for r in rs {
                let x0 = max(0, Int(r.minX) / 64), x1 = min(cols - 1, (Int(r.maxX.rounded(.up)) - 1) / 64)
                let y0 = max(0, Int(r.minY) / 64), y1 = min(rows - 1, (Int(r.maxY.rounded(.up)) - 1) / 64)
                if x1 < x0 || y1 < y0 { continue }
                for y in y0...y1 { for x in x0...x1 { changed[y * cols + x] = true } }
            }
        }
        for i in 0..<heat.count { heat[i] = heat[i] * 0.96 + (changed[i] ? 0.04 : 0) }
        let now = Date().timeIntervalSince1970
        frameTimes.append(now)
        frameTimes.removeAll { now - $0 > 2 }
        // Video: at least 12 fps sustained for 2 s, and a compact block of cells that change almost every frame.
        var hot = 0, minX = cols, minY = rows, maxX = -1, maxY = -1
        for y in 0..<rows { for x in 0..<cols where heat[y * cols + x] > 0.75 {
            hot += 1; minX = min(minX, x); minY = min(minY, y); maxX = max(maxX, x); maxY = max(maxY, y)
        } }
        if frameTimes.count >= 24 && hot * 100 >= cols * rows * 4 && maxX >= minX {
            let box = (maxX - minX + 1) * (maxY - minY + 1)
            if hot * 2 >= box {
                videoRect = CGRect(x: minX * 64, y: minY * 64, width: (maxX - minX + 1) * 64, height: (maxY - minY + 1) * 64)
                    .intersection(CGRect(x: 0, y: 0, width: width, height: height))
                return
            }
        }
        videoRect = nil
    }

    func push(_ pb: CVPixelBuffer, dirty rects: [CGRect]?) {
        cond.lock()
        framesIn += 1
        if rects == nil { framesNoRects += 1 } else if rects!.isEmpty { framesEmptyRects += 1 }
        rectsIn += rects?.count ?? 0
        latest = pb
        width = CVPixelBufferGetWidth(pb)
        height = CVPixelBufferGetHeight(pb)
        // No dirty-rect list (or an unknown format): treat the whole frame as changed.
        if let r = rects { dirty.append(contentsOf: r) } else { dirty.append(CGRect(x: 0, y: 0, width: width, height: height)) }
        // Nobody may be asking for updates (receiver gone): keep the pending list bounded.
        if dirty.count > 256 { dirty = [CGRect(x: 0, y: 0, width: width, height: height)] }
        updateVideoDetection(rects)
        cond.signal()
        cond.unlock()
    }
}
let state = State()

// MARK: - Encoding

let ciContext = CIContext(options: [.useSoftwareRenderer: false, .cacheIntermediates: false])
let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
let TILE = 64

/// Merges changed rects on a grid: runs of adjacent cells per row, then identical runs on consecutive rows.
func mergeDirty(_ rects: [CGRect], w: Int, h: Int) -> [CGRect] {
    let cols = (w + TILE - 1) / TILE, rows = (h + TILE - 1) / TILE
    var mark = [Bool](repeating: false, count: cols * rows)
    var count = 0
    let bounds = CGRect(x: 0, y: 0, width: w, height: h)
    for r in rects {
        let c = r.intersection(bounds)
        if c.isNull || c.isEmpty { continue }
        let x0 = Int(c.minX) / TILE, x1 = min(cols - 1, (Int(c.maxX.rounded(.up)) - 1) / TILE)
        let y0 = Int(c.minY) / TILE, y1 = min(rows - 1, (Int(c.maxY.rounded(.up)) - 1) / TILE)
        if x1 < x0 || y1 < y0 { continue }
        for y in y0...y1 { for x in x0...x1 where !mark[y * cols + x] { mark[y * cols + x] = true; count += 1 } }
    }
    if count == 0 { return [] }
    if count * 2 > cols * rows { return [bounds] }
    var out: [CGRect] = []
    var open: [String: Int] = [:]   // "x0-x1" -> index in out of the rect that reaches the previous row
    for y in 0..<rows {
        var next: [String: Int] = [:]
        var x = 0
        while x < cols {
            if !mark[y * cols + x] { x += 1; continue }
            let start = x
            while x < cols && mark[y * cols + x] { x += 1 }
            let key = "\(start)-\(x)"
            if let i = open[key] {
                out[i].size.height += CGFloat(TILE)
                next[key] = i
            } else {
                out.append(CGRect(x: start * TILE, y: y * TILE, width: (x - start) * TILE, height: TILE))
                next[key] = out.count - 1
            }
        }
        open = next
    }
    return out.map { $0.intersection(bounds) }
}

/// Splits large rects into pieces of at most 480x360 so the receiver can decode them in parallel
/// (a full 1080p JPEG takes 80-100 ms on a TV, twelve 480x360 pieces far less).
let MAXW = 480, MAXH = 360
func split(_ rects: [CGRect]) -> [CGRect] {
    var out: [CGRect] = []
    for r in rects {
        if Int(r.width) <= MAXW && Int(r.height) <= MAXH { out.append(r); continue }
        var y = r.minY
        while y < r.maxY {
            let h = min(CGFloat(MAXH), r.maxY - y)
            var x = r.minX
            while x < r.maxX {
                let w = min(CGFloat(MAXW), r.maxX - x)
                out.append(CGRect(x: x, y: y, width: w, height: h))
                x += w
            }
            y += h
        }
    }
    return out
}

func jpeg(_ pb: CVPixelBuffer, _ r: CGRect, quality: Double, scale: CGFloat = 1) -> Data? {
    let h = CGFloat(CVPixelBufferGetHeight(pb))
    // CIImage has a bottom-left origin.
    let ciRect = CGRect(x: r.minX, y: h - r.maxY, width: r.width, height: r.height)
    var img = CIImage(cvPixelBuffer: pb).cropped(to: ciRect)
        .transformed(by: CGAffineTransform(translationX: -ciRect.minX, y: -ciRect.minY))
    if scale != 1 { img = img.transformed(by: CGAffineTransform(scaleX: scale, y: scale)) }
    let opts: [CIImageRepresentationOption: Any] =
        [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): quality]
    // Core Image returns autoreleased objects; worker threads would otherwise keep them alive.
    return autoreleasepool { ciContext.jpegRepresentation(of: img, colorSpace: srgb, options: opts) }
}

func be16(_ v: Int) -> [UInt8] { [UInt8((v >> 8) & 0xff), UInt8(v & 0xff)] }
func be32(_ v: Int) -> [UInt8] { [UInt8((v >> 24) & 0xff), UInt8((v >> 16) & 0xff), UInt8((v >> 8) & 0xff), UInt8(v & 0xff)] }

let out = FileHandle.standardOutput

/// Trailing metadata tile: a zero-length tile marks the region detected as video playback.
func videoMeta() -> [UInt8] {
    state.cond.lock()
    // No frames for over 1 s: playback has ended.
    if let last = state.frameTimes.last, Date().timeIntervalSince1970 - last > 1 { state.videoRect = nil }
    let v = state.videoRect
    state.cond.unlock()
    guard let r = v else { return [] }
    return be16(Int(r.minX)) + be16(Int(r.minY)) + be16(Int(r.width)) + be16(Int(r.height)) + be32(0)
}

/// Status only, no images (used while the receiver shows the H.264 stream): drops pending changes
/// and reports whether a video region is active.
func probe() {
    state.cond.lock()
    state.dirty.removeAll()
    let w = state.width, h = state.height
    state.cond.unlock()
    var data = Data()
    var ts = (Date().timeIntervalSince1970 * 1000).bitPattern.bigEndian
    data.append(Data(bytes: &ts, count: 8))
    data.append(contentsOf: be16(w) + be16(h))
    let meta = videoMeta()
    data.append(contentsOf: be32(meta.isEmpty ? 0 : 1))
    data.append(contentsOf: meta)
    out.write(data)
}

func respond(full: Bool) {
    state.cond.lock()
    var refine = false
    if !full {
        // After a downscaled update, send the sharp version once the screen has been still for 150 ms.
        let deadline = Date().addingTimeInterval(state.needsRefine ? 0.15 : 1)
        while state.dirty.isEmpty && state.failed == nil && Date() < deadline { _ = state.cond.wait(until: deadline) }
        if state.dirty.isEmpty && state.needsRefine { refine = true }
    } else {
        let deadline = Date().addingTimeInterval(5)
        while state.latest == nil && state.failed == nil && Date() < deadline { _ = state.cond.wait(until: deadline) }
    }
    if let f = state.failed { state.cond.unlock(); fail(f) }
    let pb = state.latest
    let w = state.width, h = state.height
    let rects = full ? [CGRect(x: 0, y: 0, width: w, height: h)] : (refine ? state.refineRects : state.dirty)
    if refine || full { state.refineRects.removeAll() }
    state.dirty.removeAll()
    state.cond.unlock()

    var data = Data()
    var ts = (Date().timeIntervalSince1970 * 1000).bitPattern.bigEndian
    data.append(Data(bytes: &ts, count: 8))
    data.append(contentsOf: be16(w) + be16(h))
    guard let buf = pb, w > 0 else {
        data.append(contentsOf: be32(0)); out.write(data); return
    }
    let merged = mergeDirty(rects, w: w, h: h)
    let area = merged.reduce(0.0) { $0 + $1.width * $1.height }
    // Large moving regions (video, dragged window, scrolling) go out downscaled as a single piece:
    // a fraction of the data and no seams. Small regions (text, pointer) always stay sharp.
    let motion = downscaleMotion && !full && !refine && area > CGFloat(w * h) * 0.08
    state.cond.lock(); let lvl = max(0, min(3, state.level)); state.cond.unlock()
    let motionScale: [CGFloat] = [0.5, 0.4, 0.33, 0.25]
    let motionQuality: [Double] = [fullQuality, fullQuality - 0.05, fullQuality - 0.08, fullQuality - 0.1]
    var jobs: [(CGRect, CGFloat)] = []
    var halfRes: [CGRect] = []
    for r in merged {
        if motion && r.width * r.height >= 160 * 160 {
            jobs.append((r, motionScale[lvl])); halfRes.append(r)
        } else {
            for piece in split([r]) { jobs.append((piece, 1)) }
        }
    }
    if !halfRes.isEmpty {
        state.cond.lock()
        state.refineRects.append(contentsOf: halfRes)
        if state.refineRects.count > 64 { state.refineRects = [CGRect(x: 0, y: 0, width: w, height: h)] }
        state.halfResOut += 1
        state.cond.unlock()
    }
    var tiles = [(CGRect, Data)?](repeating: nil, count: jobs.count)
    tiles.withUnsafeMutableBufferPointer { slots in
        DispatchQueue.concurrentPerform(iterations: jobs.count) { i in
            let (r, sc) = jobs[i]
            if let j = jpeg(buf, r, quality: sc < 1 ? motionQuality[lvl] : tileQuality, scale: sc) { slots[i] = (r, j) }
        }
    }
    let ready = tiles.compactMap { $0 }
    if refine { state.cond.lock(); state.refinesOut += 1; state.cond.unlock() }
    state.cond.lock(); state.commands += 1; state.tilesOut += ready.count; state.cond.unlock()
    let meta = videoMeta()
    data.append(contentsOf: be32(ready.count + (meta.isEmpty ? 0 : 1)))
    for (r, j) in ready {
        data.append(contentsOf: be16(Int(r.minX)) + be16(Int(r.minY)) + be16(Int(r.width)) + be16(Int(r.height)) + be32(j.count))
        data.append(j)
    }
    data.append(contentsOf: meta)
    out.write(data)
}

// MARK: - Cursor channel
// Text lines, in pixels of the transmitted image:
//   c X Y                      position, sent at up to 60 Hz when it changes (-1 -1 = off screen)
//   C VER HX HY W H BASE64PNG  shape: hotspot and size, sent when it changes
var capDisplay: CGDirectDisplayID = CGMainDisplayID()
var capOutW = 0, capOutH = 0

func cursorWrite(_ line: String) {
    guard cursorFD >= 0, let d = (line + "\n").data(using: .utf8) else { return }
    _ = d.withUnsafeBytes { write(cursorFD, $0.baseAddress, $0.count) }
}

var lastPos = (-2, -2)
var lastShape = Data()
var shapeVersion = 0
var cursorTick = 0

func cursorStep() {
    guard capOutW > 0 else { return }
    let b = CGDisplayBounds(capDisplay)
    let k = CGFloat(capOutW) / b.width     // output pixels per screen point
    if let loc = CGEvent(source: nil)?.location {
        var x = -1, y = -1
        if b.contains(loc) {
            x = Int(((loc.x - b.minX) * k).rounded()); y = Int(((loc.y - b.minY) * k).rounded())
        }
        if (x, y) != lastPos { lastPos = (x, y); cursorWrite("c \(x) \(y)") }
    }
    cursorTick += 1
    if cursorTick % 6 != 1 { return }        // check the shape at 10 Hz
    guard let cur = NSCursor.currentSystem, let tiff = cur.image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff), let png = rep.representation(using: .png, properties: [:]) else { return }
    if png == lastShape { return }
    lastShape = png
    shapeVersion += 1
    let size = cur.image.size, hs = cur.hotSpot
    cursorWrite("C \(shapeVersion) \(Int((hs.x * k).rounded())) \(Int((hs.y * k).rounded())) " +
                "\(Int((size.width * k).rounded())) \(Int((size.height * k).rounded())) \(png.base64EncodedString())")
}

// MARK: - Frame copy
// ScreenCaptureKit buffers come from a small pool that macOS reuses. If anything retains them
// (e.g. the Core Image cache) macOS stops delivering frames, so each frame is copied into our own
// buffer right away and the original is released.
var copyPool: CVPixelBufferPool?
var copyW = 0, copyH = 0

func copyFrame(_ src: CVPixelBuffer) -> CVPixelBuffer? {
    let w = CVPixelBufferGetWidth(src), h = CVPixelBufferGetHeight(src)
    if copyPool == nil || w != copyW || h != copyH {
        let attrs: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                    kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
                                    kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
        copyPool = nil
        CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &copyPool)
        copyW = w; copyH = h
    }
    var dst: CVPixelBuffer?
    guard let pool = copyPool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &dst) == kCVReturnSuccess, let out = dst else { return nil }
    CVPixelBufferLockBaseAddress(src, .readOnly)
    CVPixelBufferLockBaseAddress(out, [])
    let sb = CVPixelBufferGetBytesPerRow(src), db = CVPixelBufferGetBytesPerRow(out)
    if let s = CVPixelBufferGetBaseAddress(src), let d = CVPixelBufferGetBaseAddress(out) {
        if sb == db {
            memcpy(d, s, sb * h)
        } else {
            for y in 0..<h { memcpy(d + y * db, s + y * sb, min(sb, db)) }
        }
    }
    CVPixelBufferUnlockBaseAddress(out, [])
    CVPixelBufferUnlockBaseAddress(src, .readOnly)
    return out
}

// MARK: - Source: screen capture

final class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?

    func start() {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16)
        var n: UInt32 = 0
        CGGetActiveDisplayList(16, &ids, &n)
        guard displayIndex < Int(n) else { fail("display \(displayIndex) does not exist (\(n) active)") }
        let wanted = ids[displayIndex]
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { content, error in
            guard let content = content else {
                state.cond.lock()
                state.failed = "Screen Recording permission denied or capture unavailable (\(error?.localizedDescription ?? "?"))"
                state.cond.signal(); state.cond.unlock()
                return
            }
            guard let display = content.displays.first(where: { $0.displayID == wanted }) else {
                state.cond.lock(); state.failed = "display not found"; state.cond.signal(); state.cond.unlock(); return
            }
            // Fit the display inside the requested size (default 1920x1080), keeping the aspect ratio.
            let boxW = Double(outW > 0 ? outW : 1920), boxH = Double(outH > 0 ? outH : 1080)
            let pw = Double(CGDisplayPixelsWide(wanted)), ph = Double(CGDisplayPixelsHigh(wanted))
            let sc = min(boxW / pw, boxH / ph)
            let w = Int((pw * sc).rounded()) & ~1, h = Int((ph * sc).rounded()) & ~1
            capDisplay = wanted; capOutW = w; capOutH = h
            let cfg = SCStreamConfiguration()
            cfg.width = w; cfg.height = h
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
            cfg.pixelFormat = kCVPixelFormatType_32BGRA
            cfg.showsCursor = cursorFD < 0   // with the cursor channel the receiver draws the pointer itself
            cfg.queueDepth = 6
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let s = SCStream(filter: filter, configuration: cfg, delegate: self)
            do {
                try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: DispatchQueue(label: "screencap", autoreleaseFrequency: .workItem))
            } catch {
                state.cond.lock(); state.failed = "addStreamOutput: \(error)"; state.cond.signal(); state.cond.unlock(); return
            }
            s.startCapture { err in
                if let err = err {
                    state.cond.lock(); state.failed = "capture failed to start: \(err.localizedDescription)"; state.cond.signal(); state.cond.unlock()
                } else {
                    note("capture started: display \(displayIndex), \(w)x\(h), \(fps) fps")
                }
            }
            self.stream = s
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid, let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        guard let arr = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let att = arr.first,
              let raw = att[.status] as? Int, let status = SCFrameStatus(rawValue: raw), status == .complete else { return }
        var rects: [CGRect]? = nil
        if let list = att[.dirtyRects] as? [Any] {
            var parsed: [CGRect] = []
            var unknown = false
            for item in list {
                if let d = item as? NSDictionary, let r = CGRect(dictionaryRepresentation: d as CFDictionary) {
                    parsed.append(r)
                } else if let v = item as? NSValue {
                    parsed.append(v.rectValue)
                } else {
                    unknown = true
                }
            }
            rects = unknown ? nil : parsed
        }
        if let r = rects, r.isEmpty {
            state.cond.lock(); state.framesEmptyRects += 1; state.cond.unlock()
            return
        }
        guard let mine = copyFrame(pb) else { return }
        state.push(mine, dirty: rects)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        state.cond.lock(); state.failed = "capture stopped: \(error.localizedDescription)"; state.cond.signal(); state.cond.unlock()
    }
}

// MARK: - Source: synthetic (testing)

func startTestSource() {
    let w = outW > 0 ? outW : 1920, h = outH > 0 ? outH : 1080
    capDisplay = CGMainDisplayID(); capOutW = w; capOutH = h   // cursor channel tracks the main display
    var pool: CVPixelBufferPool?
    let attrs: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
                                kCVPixelBufferIOSurfacePropertiesKey as String: [:]]
    CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &pool)
    var frame = 0
    var lastBox = CGRect.zero
    let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "test", autoreleaseFrequency: .workItem))
    timer.schedule(deadline: .now(), repeating: 1.0 / Double(fps))
    timer.setEventHandler {
        var pbo: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, pool!, &pbo)
        guard let pb = pbo else { return }
        CVPixelBufferLockBaseAddress(pb, [])
        let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb), width: w, height: h, bitsPerComponent: 8,
                            bytesPerRow: CVPixelBufferGetBytesPerRow(pb), space: srgb,
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
        // Thin lines that resemble text, plus a moving box.
        ctx.setFillColor(CGColor(red: 0.07, green: 0.05, blue: 0.13, alpha: 1)); ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        ctx.setFillColor(CGColor(red: 0.8, green: 0.8, blue: 0.85, alpha: 1))
        let shift = testScroll ? (frame * 8) % 24 : 0
        for y in stride(from: 40 - shift, to: h - 40, by: 24) where y > 0 {
            ctx.fill(CGRect(x: 60, y: y, width: ((y + frame * (testScroll ? 8 : 0)) * 7) % (w - 400) + 200, height: 3))
        }
        if testVideo {
            if frame > fps * 12 { CVPixelBufferUnlockBaseAddress(pb, []); return }   // "video" ends after 12 s
            let v = CGRect(x: 200, y: 200, width: 640, height: 360)
            for i in 0..<16 {
                let t = CGFloat((frame * 7 + i * 37) % 255) / 255
                ctx.setFillColor(CGColor(red: t, green: 1 - t, blue: 0.5, alpha: 1))
                ctx.fill(CGRect(x: v.minX + CGFloat(i % 4) * 160, y: v.minY + CGFloat(i / 4) * 90, width: 160, height: 90))
            }
            CVPixelBufferUnlockBaseAddress(pb, [])
            state.push(pb, dirty: frame == 0 ? nil : [CGRect(x: 200, y: CGFloat(h) - 560, width: 640, height: 360)])
            frame += 1
            return
        }
        let x = CGFloat((frame * 12) % (w - 200)), y = CGFloat(h / 2 - 100)
        let box = CGRect(x: x, y: y, width: 200, height: 200)
        ctx.setFillColor(CGColor(red: 1, green: 0.48, blue: 0.4, alpha: 1)); ctx.fill(box)
        CVPixelBufferUnlockBaseAddress(pb, [])
        // CGContext has a bottom-left origin; dirty rects are top-left.
        let top = CGRect(x: x, y: CGFloat(h) - y - 200, width: 200, height: 200)
        state.push(pb, dirty: (frame == 0 || testScroll) ? nil : [lastBox, top])
        lastBox = top
        frame += 1
    }
    timer.resume()
    objc_setAssociatedObject(state, "timer", timer, .OBJC_ASSOCIATION_RETAIN)
    note("synthetic source: \(w)x\(h), \(fps) fps")
}

// MARK: - Stats (every 5 s, forwarded to the server log)

let statsTimer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "stats", autoreleaseFrequency: .workItem))
statsTimer.schedule(deadline: .now() + 5, repeating: 5)
statsTimer.setEventHandler {
    state.cond.lock()
    let msg = "last 5 s: \(state.framesIn) frames with changes (\(state.framesNoRects) without dirty rects), " +
              "\(state.framesEmptyRects) empty, \(state.rectsIn) rects, \(state.commands) updates, \(state.tilesOut) tiles, " +
              "\(state.halfResOut) downscaled, \(state.refinesOut) refines"
    state.framesIn = 0; state.framesNoRects = 0; state.framesEmptyRects = 0; state.rectsIn = 0
    state.commands = 0; state.tilesOut = 0; state.halfResOut = 0; state.refinesOut = 0
    state.cond.unlock()
    note(msg)
}
statsTimer.resume()

// MARK: - Main

signal(SIGPIPE, SIG_IGN)
if cursorFD >= 0 {
    let ct = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "cursor", autoreleaseFrequency: .workItem))
    ct.schedule(deadline: .now(), repeating: 1.0 / 60)
    ct.setEventHandler { cursorStep() }
    ct.resume()
    objc_setAssociatedObject(state, "cursorTimer", ct, .OBJC_ASSOCIATION_RETAIN)
}
let capturer = Capturer()
if testMode { startTestSource() } else { capturer.start() }

func handle(_ line: String) {
    let parts = line.trimmingCharacters(in: .whitespaces).split(separator: " ")
    if parts.count == 2, parts[0] == "next", let l = Int(parts[1]) {
        state.cond.lock(); state.level = l; state.cond.unlock()
        respond(full: false)
        return
    }
    switch line.trimmingCharacters(in: .whitespaces) {
    case "next": respond(full: false)
    case "full": respond(full: true)
    case "probe": probe()
    case "quit": exit(0)
    default: break
    }
}

Thread {
    // A plain Thread has no autorelease pool of its own: drain one per command.
    while let line = readLine() {
        autoreleasepool { handle(line) }
    }
    exit(0)   // stdin closed: the server has stopped
}.start()

dispatchMain()
