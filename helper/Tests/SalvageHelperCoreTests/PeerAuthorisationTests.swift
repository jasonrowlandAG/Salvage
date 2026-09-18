import Darwin
import Security
import Testing

@testable import SalvageHelperCore

private let leafHash = "0123456789abcdef0123456789abcdef01234567"

@Suite("Peer code requirement")
struct PeerRequirementTests {
    @Test("a Team ID is preferred and anchored to Apple")
    func teamIdentifierIsPreferred() {
        #expect(
            peerRequirementString(
                expectedIdentifier: "com.salvage.app",
                teamIdentifier: "ABCDE12345",
                leafCertificateSHA1Hex: leafHash
            ) == "identifier \"com.salvage.app\" and anchor apple generic "
                + "and certificate leaf[subject.OU] = \"ABCDE12345\""
        )
    }

    @Test("a self-signed build pins the leaf certificate instead")
    func selfSignedBuildPinsLeafCertificate() {
        #expect(
            peerRequirementString(
                expectedIdentifier: "com.salvage.app",
                teamIdentifier: nil,
                leafCertificateSHA1Hex: leafHash
            ) == "identifier \"com.salvage.app\" and certificate leaf = H\"\(leafHash)\""
        )
    }

    @Test("an empty Team ID falls back to the leaf certificate")
    func emptyTeamIdentifierFallsBack() {
        #expect(
            peerRequirementString(
                expectedIdentifier: "com.salvage.app",
                teamIdentifier: "",
                leafCertificateSHA1Hex: leafHash
            ) == "identifier \"com.salvage.app\" and certificate leaf = H\"\(leafHash)\""
        )
    }

    @Test("an ad-hoc signature yields no requirement, so the daemon fails closed")
    func adHocSignatureYieldsNoRequirement() {
        #expect(
            peerRequirementString(
                expectedIdentifier: "com.salvage.app",
                teamIdentifier: nil,
                leafCertificateSHA1Hex: nil
            ) == nil
        )
    }

    @Test("a malformed certificate hash is not accepted", arguments: [
        "", "nothex", String(repeating: "a", count: 39), String(repeating: "a", count: 41),
    ])
    func malformedCertificateHashIsRejected(hash: String) {
        #expect(
            peerRequirementString(
                expectedIdentifier: "com.salvage.app",
                teamIdentifier: nil,
                leafCertificateSHA1Hex: hash
            ) == nil
        )
    }

    // The test runner is not Salvage.app, so the real SecCode check must say
    // no — the daemon's gate is not a no-op in practice.
    @Test("an untrusted process does not satisfy the requirement")
    func untrustedProcessIsRefused() {
        let requirement = peerRequirementString(
            expectedIdentifier: "com.salvage.app",
            teamIdentifier: nil,
            leafCertificateSHA1Hex: leafHash
        )
        #expect(requirement != nil)
        #expect(isPeerAuthorised(processIdentifier: getpid(), requirement: requirement!) == false)
    }

    @Test("a requirement string that does not parse refuses the peer")
    func unparsableRequirementRefuses() {
        #expect(isPeerAuthorised(processIdentifier: getpid(), requirement: "not a requirement") == false)
    }

    // Positive control: the refusals above would also "pass" if the SecCode
    // lookup simply never worked. Feed the check a requirement this process is
    // known to satisfy — its own designated requirement — and it must say yes.
    @Test("the check does say yes to a process that satisfies the requirement")
    func matchingProcessIsAuthorised() throws {
        var selfCode: SecCode?
        #expect(SecCodeCopySelf([], &selfCode) == errSecSuccess)
        var staticCode: SecStaticCode?
        #expect(SecCodeCopyStaticCode(try #require(selfCode), [], &staticCode) == errSecSuccess)
        var requirement: SecRequirement?
        #expect(
            SecCodeCopyDesignatedRequirement(try #require(staticCode), [], &requirement)
                == errSecSuccess
        )
        var text: CFString?
        #expect(SecRequirementCopyString(try #require(requirement), [], &text) == errSecSuccess)

        #expect(isPeerAuthorised(processIdentifier: getpid(), requirement: try #require(text) as String))
    }
}
