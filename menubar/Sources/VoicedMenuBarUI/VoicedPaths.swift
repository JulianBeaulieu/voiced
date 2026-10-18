import Foundation

/// Where voiced and the app keep things: the shared install layout in the home (ADR 0017,
/// with ADR 0018's additions under Application Support), and what the app bundle carries.
/// The installer, the launch checks and the Debug pane all name paths through this.
public struct VoicedPaths: Sendable, Equatable {
    public var home: URL
    public var bundle: URL

    public init(
        home: URL = FileManager.default.homeDirectoryForCurrentUser, bundle: URL = Bundle.main.bundleURL
    ) {
        self.home = home
        self.bundle = bundle
    }

    // MARK: - In the home

    /// `~/.voiced`, the state directory: environments, the entry point, `install.json`, the
    /// uv-managed Python and the control socket.
    public var stateDirectory: URL { home.appendingPathComponent(".voiced", isDirectory: true) }
    public var environments: URL { stateDirectory.appendingPathComponent("envs", isDirectory: true) }
    public var installRecord: URL { stateDirectory.appendingPathComponent("install.json") }
    public var entryPoint: URL { stateDirectory.appendingPathComponent("bin/voiced") }
    public var pythons: URL { stateDirectory.appendingPathComponent("python", isDirectory: true) }
    public var configFile: URL { home.appendingPathComponent(".config/voiced/config.toml") }
    public var logs: URL { home.appendingPathComponent("Library/Logs/voiced", isDirectory: true) }
    /// The app's own: what the cask's `zap` removes.
    public var applicationSupport: URL {
        home.appendingPathComponent("Library/Application Support/voiced", isDirectory: true)
    }
    public var uvCache: URL { applicationSupport.appendingPathComponent("uv-cache", isDirectory: true) }
    /// Where the development override's lock is unpacked.
    public var installSourceWork: URL {
        applicationSupport.appendingPathComponent("install-source", isDirectory: true)
    }

    // MARK: - In the app bundle

    public var uv: URL { bundle.appendingPathComponent("Contents/Helpers/uv") }
    public var resources: URL { bundle.appendingPathComponent("Contents/Resources", isDirectory: true) }
    public var installManifest: URL { resources.appendingPathComponent("install-manifest.json") }
    public var bundledLock: URL { resources.appendingPathComponent("requirements.lock") }
    public var notice: URL { resources.appendingPathComponent("NOTICE") }
    public var whatsNew: URL { resources.appendingPathComponent("WhatsNew.md") }

    /// `CFBundleShortVersionString`; a bare executable (`swift run`) has none.
    public static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development build"
    }
}
