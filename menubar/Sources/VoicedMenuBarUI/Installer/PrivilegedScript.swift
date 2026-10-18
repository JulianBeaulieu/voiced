import Foundation
import Security
import VoicedControl

/// The root half of the install (ADR 0018, the privileged step): `voiced-privileged.sh`,
/// compiled into the executable, and how the app runs it and reads its result.
enum PrivilegedScript {
    /// The script's text. Its bytes are part of the signed executable, not a file in the
    /// bundle: a cask install leaves the bundle writable by the user, and root must never run
    /// anything a user process wrote.
    static let text = String(decoding: PackageResources.voiced_privileged_sh, as: UTF8.self)

    /// The fixed AppleScript `osascript` runs. The script text, the mode, the user name and
    /// the prompt arrive as `argv` and are quoted by `quoted form of`, so nothing is spliced
    /// into source. `env -i` and `--noprofile --norc` keep whatever environment
    /// `do shell script` passes on away from the script itself.
    static let appleScript = """
        on run argv
            set {script_text, mode_name, user_name, prompt_text} to argv
            do shell script "/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash --noprofile --norc -c " & quoted form of script_text & " voiced-privileged " & quoted form of mode_name & " " & quoted form of user_name with prompt prompt_text with administrator privileges
        end run
        """

    /// The only environment `osascript` gets. `do shell script` runs `/bin/sh -c` as root
    /// with the environment of its caller, and that shell reads `SHELLOPTS`, `PS4` and
    /// `BASH_ENV` before `env -i` runs; the app's own environment must never get there.
    static let environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]

    static func prompt(for mode: PrivilegedMode) -> String {
        switch mode {
        case .install: "voiced needs an administrator to install its background services."
        case .uninstall: "voiced needs an administrator to remove its background services."
        }
    }

    /// The command that shows the prompt and runs the script as root.
    static func command(_ mode: PrivilegedMode, user: String) -> InstallCommand {
        InstallCommand(
            "/usr/bin/osascript", ["-e", appleScript, text, mode.rawValue, user, prompt(for: mode)],
            environment: environment, inheritsEnvironment: false)
    }

    /// Reads how `osascript` ended. A failed `do shell script` exits 1 with
    /// `... execution error: <the script's stderr> (<its exit status>)`; -128 is a cancelled
    /// prompt.
    static func outcome(status: Int32, stdout: [String], stderr: [String]) -> PrivilegedOutcome {
        if status == 0 { return .finished(status: 0, output: stdout.joined(separator: "\n")) }
        let text = stderr.joined(separator: "\n")
        guard let match = text.range(of: #"\((-?\d+)\)\s*$"#, options: .regularExpression),
            let number = Int32(text[match].dropFirst().prefix { $0 != ")" })
        else { return .finished(status: status, output: text) }
        if number == -128 { return .cancelled }
        var message = String(text[..<match.lowerBound]).trimmingCharacters(in: .whitespaces)
        if let range = message.range(of: "execution error: ") { message = String(message[range.upperBound...]) }
        return .finished(status: number, output: message)
    }

    /// What a run of the script means for the installer.
    enum Result: Equatable {
        /// Installed; a firewall problem, if any, is a warning, not a failure.
        case installed(warning: String?)
        case cancelled
        /// Another live account's install, as the screen says it.
        case anotherAccount(String)
        case failed(cause: String, output: [String])
    }

    /// Exit codes and output lines as `voiced-privileged.sh` defines them in its header:
    /// 0 done, 1 refused with `refused: ...` and nothing written, 3 a bootstrap failed with
    /// `failed <label>` per label, 4 and 5 firewall problems after a successful install.
    static func result(of outcome: PrivilegedOutcome) -> Result {
        let status: Int32
        let lines: [String]
        switch outcome {
        case .cancelled: return .cancelled
        case .damaged(let problem):
            return .failed(cause: "This copy of voiced is damaged; download it again. (\(problem))", output: [])
        case .finished(let code, let output):
            status = code
            lines = output.split(whereSeparator: \.isNewline).map(String.init)
        }
        switch status {
        case 0:
            return .installed(warning: nil)
        case 4:
            return .installed(warning: "voiced could not allow its services through the firewall. Allow them in System Settings > Network > Firewall.")
        case 5:
            return .installed(warning: "Your firewall is on and voiced's Python is outside your home folder, so voiced left the firewall alone. Allow it in System Settings > Network > Firewall.")
        case 3:
            let services = lines.compactMap(failedService)
            let names = services.isEmpty ? "the background services" : services.joined(separator: " and ")
            return .failed(cause: "macOS did not start \(names).", output: lines)
        default:
            let refusal = lines.lazy.compactMap { line -> String? in
                guard line.hasPrefix("refused: ") else { return nil }
                return String(line.dropFirst("refused: ".count))
            }.first
            if let refusal, refusal.hasPrefix("voiced is already installed for ") {
                return .anotherAccount(refusal + ".")
            }
            let cause = refusal.map { "Refused: \($0)." } ?? "The privileged step exited with status \(status)."
            return .failed(cause: cause, output: lines)
        }
    }

    /// "failed io.github.julianbeaulieu.voiced.tts" -> "text to speech".
    static func failedService(_ line: String) -> String? {
        guard line.hasPrefix("failed "), let service = line.split(separator: ".").last else { return nil }
        if let kind = ServiceKind(rawValue: String(service)) { return kind.title.lowercased() }
        return service == "watchdog" ? "the watchdog" : String(service)
    }
}

/// ADR 0018's check of the app's own bundle before the prompt: strict validation of every
/// architecture and nested code. It catches a half-copied or partly modified bundle, nothing
/// more. The designated requirement with voiced's team identifier is added once releases are
/// signed with a Developer ID; until then every build is ad hoc and has no team to require.
enum BundleCheck {
    /// Why the bundle at `url` does not check out, or nil when it does or is not a bundle
    /// (`swift run`).
    static func problem(at url: URL) -> String? {
        guard url.pathExtension == "app" else { return nil }
        var code: SecStaticCode?
        var status = SecStaticCodeCreateWithPath(url as CFURL, [], &code)
        guard status == errSecSuccess, let code else { return "cannot read its signature (\(status))" }
        let flags = SecCSFlags(
            rawValue: kSecCSStrictValidate | kSecCSCheckAllArchitectures | kSecCSCheckNestedCode)
        status = SecStaticCodeCheckValidity(code, flags, nil)
        guard status != errSecSuccess else { return nil }
        let message = SecCopyErrorMessageString(status, nil) as String? ?? "error \(status)"
        return "its signature does not check out: \(message)"
    }
}
