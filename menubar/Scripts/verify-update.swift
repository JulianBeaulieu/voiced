// Checks an update archive's EdDSA signature against a public key, as Sparkle does with the
// app's SUPublicEDKey before it installs (ADR 0034). The release workflow runs it on the zip
// it signed: with the built app's key on a tag, so a SPARKLE_PRIVATE_KEY that is not the
// app's key fails the build instead of publishing an update no installed app accepts.
//
//     swift menubar/Scripts/verify-update.swift PUBLIC_KEY ARCHIVE SIGNATURE
import CryptoKit
import Foundation

let arguments = CommandLine.arguments.dropFirst()
guard arguments.count == 3 else {
    FileHandle.standardError.write(Data("usage: verify-update.swift PUBLIC_KEY ARCHIVE SIGNATURE\n".utf8))
    exit(2)
}
let (encodedKey, archive, signature) = (arguments[1], arguments[2], arguments[3])

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("verify-update.swift: \(message)\n".utf8))
    exit(1)
}

guard let keyData = Data(base64Encoded: encodedKey), let key = try? Curve25519.Signing.PublicKey(rawRepresentation: keyData)
else { fail("the public key is not a base64 Ed25519 key") }
guard let signatureData = Data(base64Encoded: signature) else { fail("the signature is not base64") }
guard let data = FileManager.default.contents(atPath: archive) else { fail("cannot read \(archive)") }
guard key.isValidSignature(signatureData, for: data) else { fail("\(archive) is not signed with that key") }
print("\(URL(fileURLWithPath: archive).lastPathComponent): signed with the key")
