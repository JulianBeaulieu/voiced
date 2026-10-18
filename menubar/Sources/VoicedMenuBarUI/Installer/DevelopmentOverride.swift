import Foundation
import Security

/// ADR 0018's development override: the `installSource` user default points at a voiced
/// wheel or checkout, which the installer then installs instead of the wheel in the bundle, so
/// a development build of the app can install the matching, unpushed voiced:
///
///     defaults write io.github.julianbeaulieu.voiced.menubar installSource ~/src/voiced
///
/// Release builds ignore it. A release is what the release workflow signs with a Developer
/// ID, so the test is the app's own signature: one with a team identifier is a release; an ad
/// hoc signature (bundle.sh) or none (`swift run`) is a development build.
public enum DevelopmentOverride {
    public static let key = "installSource"

    public static func installSource(
        defaults: UserDefaults = .standard, isRelease: Bool = isReleaseBuild()
    ) -> URL? {
        guard !isRelease, let path = defaults.string(forKey: key)?.trimmingCharacters(in: .whitespaces),
            !path.isEmpty
        else { return nil }
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath).standardizedFileURL
    }

    /// Whether the running app is signed with a team identifier.
    public static func isReleaseBuild() -> Bool {
        var code: SecCode?
        var staticCode: SecStaticCode?
        var information: CFDictionary?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code,
            SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode,
            SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &information)
                == errSecSuccess,
            let information = information as? [String: Any]
        else { return false }
        return information[kSecCodeInfoTeamIdentifier as String] is String
    }
}
