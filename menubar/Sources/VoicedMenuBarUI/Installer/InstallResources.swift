import CryptoKit
import Foundation

/// What the app pins for an install (ADR 0018): the voiced version, the Python patch version,
/// the lock and the wheel it ships with their hashes, and the models with their revisions and
/// sizes. bundle.sh writes it with `scripts/app_manifest.py`.
public struct InstallManifest: Codable, Sendable, Equatable {
    public struct Wheel: Codable, Sendable, Equatable {
        public var name: String
        public var sha256: String

        public init(name: String, sha256: String) {
            self.name = name
            self.sha256 = sha256
        }
    }

    public struct Model: Codable, Sendable, Equatable {
        public var repo: String
        public var revision: String
        public var bytes: Int64

        public init(repo: String, revision: String, bytes: Int64) {
            self.repo = repo
            self.revision = revision
            self.bytes = bytes
        }
    }

    /// The voiced version of the wheel, as `install.json` records it once installed.
    public var version: String
    public var python: String
    public var lockSHA256: String
    public var wheel: Wheel
    /// The total download size of the locked wheels.
    public var wheelsBytes: Int64
    public var models: [Model]

    public init(
        version: String, python: String, lockSHA256: String, wheel: Wheel, wheelsBytes: Int64, models: [Model]
    ) {
        self.version = version
        self.python = python
        self.lockSHA256 = lockSHA256
        self.wheel = wheel
        self.wheelsBytes = wheelsBytes
        self.models = models
    }

    enum CodingKeys: String, CodingKey {
        case version, python, wheel, models
        case lockSHA256 = "lock_sha256"
        case wheelsBytes = "wheels_bytes"
    }

    public var downloadBytes: Int64 { wheelsBytes + models.reduce(0) { $0 + $1.bytes } }

    /// The manual fallback a failed download names (ADR 0018, "Download goes wrong").
    var manualDownload: String {
        let pinned = models.map { "\($0.repo) at revision \($0.revision)" }.joined(separator: " and ")
        return "To download the models by hand, fetch \(pinned) into the Hugging Face cache, then try again."
    }
}

/// The files the app carries for an install: the bundled uv, the lock, the wheel and the
/// manifest.
public struct InstallResources: Sendable, Equatable {
    public var uv: URL
    public var lock: URL
    public var wheel: URL
    public var manifest: InstallManifest

    public init(uv: URL, lock: URL, wheel: URL, manifest: InstallManifest) {
        self.uv = uv
        self.lock = lock
        self.wheel = wheel
        self.manifest = manifest
    }

    /// The resources in the bundle: `Contents/Helpers/uv`, and the manifest, the lock and the
    /// wheel it names in `Contents/Resources`. Nil when any is missing, as under `swift run`,
    /// which keeps pointing at the installation guide.
    public static func inBundle(_ paths: VoicedPaths = VoicedPaths()) -> InstallResources? {
        guard let data = try? Data(contentsOf: paths.installManifest),
            let manifest = try? JSONDecoder().decode(InstallManifest.self, from: data)
        else { return nil }
        let found = InstallResources(
            uv: paths.uv, lock: paths.bundledLock,
            wheel: paths.resources.appendingPathComponent(manifest.wheel.name), manifest: manifest)
        let files = [found.uv, found.lock, found.wheel]
        return files.allSatisfy { FileManager.default.fileExists(atPath: $0.path) } ? found : nil
    }

    /// Why the lock or the wheel is not the one the manifest pins, or nil when both match.
    func mismatch() -> String? {
        for (file, expected) in [(lock, manifest.lockSHA256), (wheel, manifest.wheel.sha256)] {
            guard let digest = Self.sha256(of: file) else { return "Cannot read \(file.lastPathComponent)." }
            if digest != expected.lowercased() {
                return "\(file.lastPathComponent) does not match the app's manifest. Download the app again."
            }
        }
        return nil
    }

    static func sha256(of url: URL) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        var hash = SHA256()
        while let chunk = try? handle.read(upToCount: 1 << 20), !chunk.isEmpty { hash.update(data: chunk) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
