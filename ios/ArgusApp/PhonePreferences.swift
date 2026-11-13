import Foundation

enum PhonePreferences {
    static let idleTimeoutMinutesKey = "idleTimeoutMinutes"
    static let idleTimeoutChoices = [1, 3, 5, 10]
    static let defaultIdleTimeoutMinutes = 3

    static let startsOnLaunchKey = "startsOnLaunch"

    static let usesEarpieceKey = "usesEarpiece"

    static var usesEarpiece: Bool {
        UserDefaults.standard.bool(forKey: usesEarpieceKey)
    }
}
