import Foundation

/// ADR 0018's development override: in a debug build, the `installSource` user default names
/// a voiced wheel, which the installer installs instead of the wheel in the bundle, so a
/// development build of the app can install the matching, unpushed voiced:
///
///     uv build --wheel                  # in the checkout
///     defaults write io.github.julianbeaulieu.voiced.menubar installSource ~/src/voiced/dist/voiced-0.2.0-py3-none-any.whl
///
/// It exists only in debug builds (`swift build`, `swift run`). bundle.sh and the release
/// build in release configuration, which compiles it out, so no process can steer what a
/// shipped app installs by writing a default.
public enum DevelopmentOverride {
    public static let key = "installSource"

    public static func installSource(defaults: UserDefaults = .standard) -> URL? {
        #if DEBUG
            guard let path = defaults.string(forKey: key)?.trimmingCharacters(in: .whitespaces), !path.isEmpty
            else { return nil }
            return URL(fileURLWithPath: (path as NSString).expandingTildeInPath).standardizedFileURL
        #else
            return nil
        #endif
    }
}
