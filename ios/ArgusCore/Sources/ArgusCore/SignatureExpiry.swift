
import Foundation

private let secondsPerHour: TimeInterval = 3600
private let secondsPerDay: TimeInterval = 24 * secondsPerHour

public let signatureLifetime: TimeInterval = 7 * secondsPerDay

public enum SignatureUrgency: Sendable, Equatable {
    case fine
    case soon
    case urgent

    public init(expiresAt: Date, now: Date) {
        let remaining = expiresAt.timeIntervalSince(now)
        if remaining <= secondsPerDay {
            self = .urgent
        } else if remaining <= 3 * secondsPerDay {
            self = .soon
        } else {
            self = .fine
        }
    }
}

public func describeRemaining(until expiresAt: Date, now: Date) -> String {
    let remaining = expiresAt.timeIntervalSince(now)
    let days = Int(remaining / secondsPerDay)
    if days >= 1 {
        return "\(days) 天后"
    }
    let hours = Int(remaining / secondsPerHour)
    if hours >= 1 {
        return "\(hours) 小时后"
    }
    return remaining > 0 ? "不到 1 小时" : "已经到期"
}

public func provisioningProfileExpiry(_ profileData: Data) -> Date? {
    guard let plistStart = profileData.range(of: Data("<?xml".utf8)),
          let plistEnd = profileData.range(
              of: Data("</plist>".utf8),
              in: plistStart.lowerBound..<profileData.endIndex
          )
    else {
        return nil
    }
    let plistData = profileData[plistStart.lowerBound..<plistEnd.upperBound]
    let plist = try? PropertyListSerialization.propertyList(from: plistData, format: nil)
    return (plist as? [String: Any])?["ExpirationDate"] as? Date
}
