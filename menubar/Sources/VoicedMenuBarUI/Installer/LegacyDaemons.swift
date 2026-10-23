import Foundation
import Security
import VoicedControl

/// The LaunchDaemons releases before ADR 0035 installed in `/Library/LaunchDaemons`, and the
/// one administrator prompt that removes them. The services are LaunchAgents now and need no
/// root at all; this prompt is the last time voiced asks for a password, once per Mac that
/// ran an earlier release.
enum LegacyDaemons {
    /// What root runs: fixed commands by absolute path, with no input at all, so root never
    /// runs anything the user's account wrote. They boot out and delete the three daemons,
    /// then wait up to 30 s per label until launchd has let go of each. `voiced.launchd.control.legacy_removal_command`
    /// prints the same commands for a terminal; a pytest keeps the two equal.
    static let removal = "/bin/launchctl bootout system/io.github.julianbeaulieu.voiced.stt 2>/dev/null; /bin/launchctl bootout system/io.github.julianbeaulieu.voiced.tts 2>/dev/null; /bin/launchctl bootout system/io.github.julianbeaulieu.voiced.watchdog 2>/dev/null; /bin/rm -f /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.watchdog.plist; n=0; while /bin/launchctl print system/io.github.julianbeaulieu.voiced.stt >/dev/null 2>&1 && [ $n -lt 150 ]; do /bin/sleep 0.2; n=$((n+1)); done; n=0; while /bin/launchctl print system/io.github.julianbeaulieu.voiced.tts >/dev/null 2>&1 && [ $n -lt 150 ]; do /bin/sleep 0.2; n=$((n+1)); done; n=0; while /bin/launchctl print system/io.github.julianbeaulieu.voiced.watchdog >/dev/null 2>&1 && [ $n -lt 150 ]; do /bin/sleep 0.2; n=$((n+1)); done"

    /// The fixed AppleScript `osascript` runs. The commands and the prompt arrive as `argv`,
    /// never spliced into source.
    static let appleScript = """
        on run argv
            set {command_text, prompt_text} to argv
            do shell script command_text with prompt prompt_text with administrator privileges
        end run
        """

    /// The only environment `osascript` gets. `do shell script` runs `/bin/sh -c` as root
    /// with the environment of its caller, and that shell reads `SHELLOPTS`, `PS4` and
    /// `BASH_ENV` before anything else (ADR 0018); the app's own environment must never get
    /// there.
    static let environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]

    static let prompt = "voiced needs an administrator to remove the old background services (the last time a password is needed)."

    /// The command that shows the prompt and removes the daemons as root.
    static var command: InstallCommand {
        InstallCommand(
            "/usr/bin/osascript", ["-e", appleScript, removal, prompt], environment: environment,
            inheritsEnvironment: false)
    }

    /// Reads how `osascript` ended. A failed `do shell script` exits 1 with
    /// `... execution error: <stderr> (<exit status>)`; -128 is a cancelled prompt.
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

    /// What a run of the prompt means for the installer.
    enum Result: Equatable {
        case done
        case cancelled
        case failed(cause: String, output: [String])
    }

    static func result(of outcome: PrivilegedOutcome) -> Result {
        switch outcome {
        case .cancelled: return .cancelled
        case .damaged(let problem):
            return .failed(cause: "This copy of voiced is damaged; download it again. (\(problem))", output: [])
        case .finished(let status, let output):
            if status == 0 { return .done }
            return .failed(
                cause: "The old background services could not be removed (status \(status)).",
                output: output.split(whereSeparator: \.isNewline).map(String.init))
        }
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
