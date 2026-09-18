// Peer authorisation.
//
// The Unix socket authenticated the caller by group membership alone, which on
// a typical single-admin-user Mac means "every process the user runs". XPC
// gives the daemon the connecting process's identity, so the daemon can demand
// that the peer be Salvage.app, signed by the same identity as the helper.
//
// The requirement is derived from the *helper's own* signature at runtime
// rather than hardcoded, because the app is built with a self-signed identity
// that has no Team ID and whose certificate differs per developer machine
// (see DESIGN.md). Whatever signed the helper must also have signed the peer.

import CryptoKit
import Foundation
import Security

/// Builds the code requirement string a connecting peer must satisfy.
///
/// Pure and testable — the SecCode lookups that feed it are not.
/// Returns nil when the helper's own signature carries neither a Team ID nor a
/// leaf certificate (i.e. it is ad-hoc signed). There is nothing to pin
/// against in that case, so the caller must refuse every connection.
public func peerRequirementString(
    expectedIdentifier: String,
    teamIdentifier: String?,
    leafCertificateSHA1Hex: String?
) -> String? {
    if let team = teamIdentifier, !team.isEmpty {
        return "identifier \"\(expectedIdentifier)\" and anchor apple generic "
            + "and certificate leaf[subject.OU] = \"\(team)\""
    }
    if let hash = leafCertificateSHA1Hex, isSHA1Hex(hash) {
        return "identifier \"\(expectedIdentifier)\" and certificate leaf = H\"\(hash)\""
    }
    return nil
}

private func isSHA1Hex(_ text: String) -> Bool {
    text.count == 40 && text.allSatisfy { $0.isHexDigit && ($0.isNumber || $0.isLowercase) }
}

/// The helper's own signing identity, as far as it is needed to pin the peer.
public struct OwnSigningIdentity {
    public let teamIdentifier: String?
    public let leafCertificateSHA1Hex: String?

    public init(teamIdentifier: String?, leafCertificateSHA1Hex: String?) {
        self.teamIdentifier = teamIdentifier
        self.leafCertificateSHA1Hex = leafCertificateSHA1Hex
    }

    /// Reads the running helper's signature.
    public static func current() -> OwnSigningIdentity? {
        var selfCode: SecCode?
        guard SecCodeCopySelf([], &selfCode) == errSecSuccess, let selfCode else { return nil }

        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(selfCode, [], &staticCode) == errSecSuccess,
              let staticCode
        else { return nil }

        var infoRef: CFDictionary?
        guard SecCodeCopySigningInformation(
                staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &infoRef) == errSecSuccess,
              let info = infoRef as? [String: Any]
        else { return nil }

        let team = info[kSecCodeInfoTeamIdentifier as String] as? String
        var leafHash: String?
        if let certificates = info[kSecCodeInfoCertificates as String] as? [SecCertificate],
           let leaf = certificates.first {
            leafHash = sha1Hex(SecCertificateCopyData(leaf) as Data)
        }
        return OwnSigningIdentity(teamIdentifier: team, leafCertificateSHA1Hex: leafHash)
    }
}

/// Checks that the process behind an XPC connection satisfies `requirement`.
///
/// Known limitation: `kSecGuestAttributePid` resolves the peer by PID, which is
/// in principle subject to a PID-reuse race. There is no public API that takes
/// an XPC connection's audit token; `NSXPCConnection.auditToken` exists but is
/// not API. This matches the fix the security review asked for
/// (processIdentifier -> code-signature verification) and is a different order
/// of protection from the socket's "any admin process" gate, but it is worth
/// revisiting if Apple ships a supported audit-token accessor.
public func isPeerAuthorised(processIdentifier pid: pid_t, requirement: String) -> Bool {
    var requirementRef: SecRequirement?
    guard SecRequirementCreateWithString(requirement as CFString, [], &requirementRef) == errSecSuccess,
          let requirementRef
    else { return false }

    let attributes = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
    var peerCode: SecCode?
    guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &peerCode) == errSecSuccess,
          let peerCode
    else { return false }

    return SecCodeCheckValidity(peerCode, [], requirementRef) == errSecSuccess
}

func sha1Hex(_ data: Data) -> String {
    // SHA-1 because that is the digest codesign's H"…" requirement syntax uses.
    Insecure.SHA1.hash(data: data).map { String(format: "%02x", $0) }.joined()
}
