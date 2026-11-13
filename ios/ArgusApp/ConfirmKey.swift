import ArgusCore
import CryptoKit
import Foundation
import LocalAuthentication

enum ConfirmKey {
    private static let keyFile = URL.applicationSupportDirectory.appending(path: "confirm-key")

    static func publicKeyDER() throws -> Data {
        guard SecureEnclave.isAvailable else { throw ConfirmKeyError.noSecureEnclave }
        let context = LAContext()
        let key = try loadSavedKey(context: context) ?? createAndSaveKey(context: context)
        return key.publicKey.derRepresentation
    }

    static func sign(ticketID: String, argsHash: String, reason: String) throws -> Data {
        guard SecureEnclave.isAvailable else { throw ConfirmKeyError.noSecureEnclave }
        let context = LAContext()
        context.localizedReason = reason
        guard let key = try loadSavedKey(context: context) else { throw ConfirmKeyError.notPaired }
        let signed = Data((ticketID + "\n" + argsHash).utf8)
        return try key.signature(for: signed).derRepresentation
    }

    private static func loadSavedKey(context: LAContext) throws -> SecureEnclave.P256.Signing.PrivateKey? {
        guard FileManager.default.fileExists(atPath: keyFile.path(percentEncoded: false)) else {
            return nil
        }
        let keyHandle = try Data(contentsOf: keyFile)
        return try SecureEnclave.P256.Signing.PrivateKey(
            dataRepresentation: keyHandle, authenticationContext: context)
    }

    private static func createAndSaveKey(context: LAContext) throws -> SecureEnclave.P256.Signing.PrivateKey {
        guard
            let accessControl = SecAccessControlCreateWithFlags(
                kCFAllocatorDefault, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                [.privateKeyUsage, .biometryCurrentSet], nil)
        else {
            throw ConfirmKeyError.accessControlFailed
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(
            accessControl: accessControl, authenticationContext: context)
        try FileManager.default.createDirectory(
            at: keyFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try key.dataRepresentation.write(to: keyFile, options: .atomic)
        appLog.info("生成了批准卡片用的密钥")
        return key
    }
}

enum ConfirmKeyError: LocalizedError {
    case noSecureEnclave
    case accessControlFailed
    case notPaired

    var errorDescription: String? {
        switch self {
        case .noSecureEnclave: "这台设备用不了安全芯片（模拟器上会这样）"
        case .accessControlFailed: "设不了密钥的 Face ID 访问控制"
        case .notPaired: "这台手机还没生成批准用的密钥，重新打开 App 配对一次"
        }
    }
}

@MainActor
func reportConfirmKey(settings: HostSettings) async -> Bool {
    guard let url = HostAddress.webSocketURL(from: settings.address), let token = settings.savedToken() else {
        return false
    }
    do {
        let publicKeyDER = try await Task.detached { try ConfirmKey.publicKeyDER() }.value
        try await pairOnce(publicKeyDER: publicKeyDER, to: url, hello: makeHello(token: token))
        appLog.info("报了批准卡片用的公钥，指纹 \(fingerprint(of: publicKeyDER))")
        return true
    } catch {
        appLog.warning("没报成批准卡片用的公钥：\(error)")
        return false
    }
}

private func fingerprint(of publicKeyDER: Data) -> String {
    String(SHA256.hash(data: publicKeyDER).map { String(format: "%02x", $0) }.joined().prefix(16))
}
