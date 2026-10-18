import Darwin
import Foundation

/// The rules for `~/.voiced` the app follows before building in it, the same as
/// `voiced.layout`'s: ADR 0017's install lock, and the claim that makes the folder voiced's.
enum StateDirectory {
    /// Files a state directory may hold before anyone claimed it: what a watchdog run before
    /// any install leaves, and Finder's (`layout.UNCLAIMED_OK`).
    static let unclaimedOK: Set<String> = ["control.sock", ".DS_Store"]

    struct NotVoiceds: LocalizedError {
        var reason: String
        var errorDescription: String? { reason }
    }

    /// Refuses a state directory that is a symbolic link, naming where it leads, as
    /// `layout.claim` does: uninstall would delete inside a target that is not voiced's.
    static func checkNotSymlink(_ state: URL) throws {
        var info = stat()
        guard lstat(state.path, &info) == 0 else { return }
        if info.st_mode & S_IFMT == S_IFLNK {
            let target = state.resolvingSymlinksInPath().path
            throw NotVoiceds(
                reason: "\(state.path) is a symbolic link to \(target). voiced needs a real folder there; move \(target) to \(state.path), then try again.")
        }
    }

    /// Claims `state` for the app (ADR 0018, Flow step 2), under the install lock and before
    /// anything is built in it: writes `install.json` with no environment yet, through a
    /// temporary file renamed into place, which `voiced install` then adopts. A folder that
    /// already has an `install.json`, or is voiced's legacy venv, is left as it is. Only an
    /// empty folder, or one holding only `unclaimedOK` files, is claimed; anything else is
    /// someone else's.
    static func claim(_ state: URL) throws {
        try checkNotSymlink(state)
        let fileManager = FileManager.default
        let manifest = state.appendingPathComponent("install.json")
        if fileManager.fileExists(atPath: manifest.path) || isVoicedsLegacyVenv(state) { return }
        let others = try fileManager.contentsOfDirectory(atPath: state.path)
            .filter { !unclaimedOK.contains($0) }.sorted()
        guard others.isEmpty else {
            let shown = others.prefix(5).joined(separator: ", ") + (others.count > 5 ? ", ..." : "")
            throw NotVoiceds(
                reason: "\(state.path) is not voiced's folder: it has no install.json and holds \(shown). Move those elsewhere, then try again.")
        }
        let temporary = state.appendingPathComponent(".install.json.tmp")
        try Data(#"{"source": "app", "environment": null}"#.utf8 + [0x0A]).write(to: temporary)
        guard rename(temporary.path, manifest.path) == 0 else {
            throw POSIXError(.init(rawValue: errno) ?? .EIO)
        }
    }

    /// `layout.is_voiced_venv`: a venv with voiced installed in it, not editable.
    static func isVoicedsLegacyVenv(_ state: URL) -> Bool {
        let fileManager = FileManager.default
        guard fileManager.fileExists(atPath: state.appendingPathComponent("pyvenv.cfg").path) else { return false }
        let lib = state.appendingPathComponent("lib")
        let pythons = ((try? fileManager.contentsOfDirectory(atPath: lib.path)) ?? []).filter { $0.hasPrefix("python3") }
        for python in pythons.sorted() {
            let site = lib.appendingPathComponent("\(python)/site-packages")
            let infos = ((try? fileManager.contentsOfDirectory(atPath: site.path)) ?? [])
                .filter { $0.hasPrefix("voiced-") && $0.hasSuffix(".dist-info") }
            guard let info = infos.sorted().first else { continue }
            let directURL = site.appendingPathComponent("\(info)/direct_url.json")
            guard let data = try? Data(contentsOf: directURL) else { return true }
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let editable = (json?["dir_info"] as? [String: Any])?["editable"] as? Bool ?? false
            return !editable
        }
        return false
    }
}

/// ADR 0017's install lock: an exclusive `flock` on the state directory, released by closing
/// it.
final class InstallLock {
    enum Attempt {
        case acquired(InstallLock)
        case busy
        case failed
    }

    private var fd: Int32

    private init(fd: Int32) {
        self.fd = fd
    }

    static func tryAcquire(_ directory: URL) -> Attempt {
        let fd = open(directory.path, O_RDONLY)
        guard fd >= 0 else { return .failed }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 { return .acquired(InstallLock(fd: fd)) }
        let busy = errno == EWOULDBLOCK
        close(fd)
        return busy ? .busy : .failed
    }

    func release() {
        guard fd >= 0 else { return }
        close(fd)
        fd = -1
    }

    deinit { release() }
}
