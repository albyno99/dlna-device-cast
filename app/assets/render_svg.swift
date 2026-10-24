// render_svg.swift - renders an SVG to PNG with AppKit (no external dependencies).
// Usage: render_svg <input.svg> <output.png> <width> <height>
import AppKit

let a = CommandLine.arguments
guard a.count == 5, let w = Int(a[3]), let h = Int(a[4]) else {
    FileHandle.standardError.write("usage: render_svg in.svg out.png width height\n".data(using: .utf8)!)
    exit(2)
}
guard let data = FileManager.default.contents(atPath: a[1]), let img = NSImage(data: data) else {
    FileHandle.standardError.write("cannot read \(a[1]) as an image\n".data(using: .utf8)!)
    exit(1)
}
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
rep.size = NSSize(width: w, height: h)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSGraphicsContext.current?.imageInterpolation = .high
img.draw(in: NSRect(x: 0, y: 0, width: w, height: h), from: .zero, operation: .copy, fraction: 1)
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: a[2]))
