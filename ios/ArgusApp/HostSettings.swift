import Foundation
import Observation
import Security

@MainActor
@Observable
final class HostSettings {
    private(set) var address: String
    private(set) var hasToken: Bool
    private(set) var lastConnectedAt: Date?

    var isConfigured: Bool { !address.isEmpty && hasToken }

    init() {
        address = UserDefaults.standard.string(forKey: Keys.address) ?? ""
        hasToken = TokenKeychain.load() != nil
        lastConnectedAt = UserDefaults.standard.object(forKey: Keys.lastConnectedAt) as? Date
    }

    func savedToken() -> String? {
        TokenKeychain.load()
    }

    func save(address newAddress: String, token: String, connectedAt: Date) -> Bool {
        guard TokenKeychain.save(token) else {
            return false
        }
        address = newAddress
        hasToken = true
        UserDefaults.standard.set(newAddress, forKey: Keys.address)
        recordConnected(at: connectedAt)
        return true
    }

    func recordConnected(at date: Date) {
        lastConnectedAt = date
        UserDefaults.standard.set(date, forKey: Keys.lastConnectedAt)
    }

    private enum Keys {
        static let address = "hostAddress"
        static let lastConnectedAt = "lastConnectedAt"
    }
}

private enum TokenKeychain {
    private static var itemQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "local.argus.app",
            kSecAttrAccount as String: "app-token",
        ]
    }

    static func load() -> String? {
        var query = itemQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let tokenData = result as? Data
        else {
            return nil
        }
        return String(data: tokenData, encoding: .utf8)
    }

    static func save(_ token: String) -> Bool {
        let tokenData = Data(token.utf8)
        let updateStatus = SecItemUpdate(
            itemQuery as CFDictionary,
            [kSecValueData as String: tokenData] as CFDictionary
        )
        if updateStatus != errSecItemNotFound {
            return updateStatus == errSecSuccess
        }
        var newItem = itemQuery
        newItem[kSecValueData as String] = tokenData
        newItem[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(newItem as CFDictionary, nil) == errSecSuccess
    }
}
