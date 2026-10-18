import Foundation

/// The running app as its bundle describes it.
public enum AppInfo {
    /// `CFBundleShortVersionString`; a bare executable (`swift run`) has none.
    public static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development build"
    }

    /// The third-party notice bundle.sh copies into the bundle.
    public static var notice: URL? {
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/NOTICE")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}
