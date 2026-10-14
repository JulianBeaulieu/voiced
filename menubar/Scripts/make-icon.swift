// Draws the app icon, the source of AppIcon.icns: the `waveform` SF Symbol in white on a
// rounded square with a blue gradient, in the sizes an .iconset needs. bundle.sh runs it and
// turns the result into an .icns with iconutil.
//
//     swift Scripts/make-icon.swift <output.iconset>

import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "AppIcon.iconset")
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func draw(pixels: Int) -> Data? {
    let size = CGFloat(pixels)
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)
    else { return nil }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    defer { NSGraphicsContext.restoreGraphicsState() }

    // The macOS icon grid: an 824/1024 square with a 185/1024 corner radius.
    let inset = size * 100 / 1024
    let tile = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
    let shape = NSBezierPath(roundedRect: tile, xRadius: size * 185 / 1024, yRadius: size * 185 / 1024)
    NSGradient(
        starting: NSColor(srgbRed: 0.30, green: 0.62, blue: 1.00, alpha: 1),
        ending: NSColor(srgbRed: 0.10, green: 0.32, blue: 0.86, alpha: 1)
    )?.draw(in: shape, angle: -90)

    let configuration = NSImage.SymbolConfiguration(pointSize: size * 0.42, weight: .semibold)
        .applying(.init(paletteColors: [.white]))
    if let symbol = NSImage(systemSymbolName: "waveform", accessibilityDescription: nil)?
        .withSymbolConfiguration(configuration)
    {
        let box = symbol.size
        symbol.draw(in: NSRect(x: (size - box.width) / 2, y: (size - box.height) / 2, width: box.width, height: box.height))
    }
    return rep.representation(using: .png, properties: [:])
}

for points in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let name = scale == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
        guard let data = draw(pixels: points * scale) else { fatalError("cannot draw \(name)") }
        try data.write(to: output.appendingPathComponent(name))
    }
}
