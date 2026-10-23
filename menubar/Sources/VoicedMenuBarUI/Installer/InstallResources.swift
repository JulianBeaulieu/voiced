import CryptoKit
import Foundation
import VoicedControl

/// What the app pins for an install (ADR 0018): the voiced version, the Python patch version,
/// the lock and the wheel it ships with their hashes, the default models with their revisions
/// and sizes, and the catalogue the first-run setup chooses from. bundle.sh writes it with
/// `scripts/app_manifest.py`.
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
    /// The models a new install runs when nothing else is chosen.
    public var models: [Model]
    /// Every catalogued engine and model, as `voiced models list --json` shows them without
    /// the cache (`python -m voiced.models`): what the setup offers before voiced is
    /// installed. Empty from a manifest that has none.
    public var catalogue: ModelList

    public init(
        version: String, python: String, lockSHA256: String, wheel: Wheel, wheelsBytes: Int64, models: [Model],
        catalogue: ModelList = ModelList(cache: "", totalBytes: 0, engines: [])
    ) {
        self.version = version
        self.python = python
        self.lockSHA256 = lockSHA256
        self.wheel = wheel
        self.wheelsBytes = wheelsBytes
        self.models = models
        self.catalogue = catalogue
    }

    enum CodingKeys: String, CodingKey {
        case version, python, wheel, models, catalogue
        case lockSHA256 = "lock_sha256"
        case wheelsBytes = "wheels_bytes"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(String.self, forKey: .version)
        python = try c.decode(String.self, forKey: .python)
        lockSHA256 = try c.decode(String.self, forKey: .lockSHA256)
        wheel = try c.decode(Wheel.self, forKey: .wheel)
        wheelsBytes = try c.decode(Int64.self, forKey: .wheelsBytes)
        models = try c.decode([Model].self, forKey: .models)
        catalogue = try c.decodeIfPresent(ModelList.self, forKey: .catalogue) ?? ModelList(cache: "", totalBytes: 0, engines: [])
    }

    /// The manual fallback a failed download names (ADR 0018, "Download goes wrong"): each
    /// model the install fetches, at its pinned revision.
    static func manualDownload(_ pins: [(repo: String, revision: String)]) -> String {
        let pinned = pins.map { "\($0.repo) at revision \($0.revision)" }.joined(separator: " and ")
        return "To download the models by hand, fetch \(pinned) into the Hugging Face cache, then try again."
    }

    /// The fallback for the default models.
    var manualDownload: String { Self.manualDownload(models.map { ($0.repo, $0.revision) }) }
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
