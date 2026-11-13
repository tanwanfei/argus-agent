import ArgusCore
import Foundation

func connectOnce(url: URL, token: String) async -> String? {
    do {
        _ = try await checkConnection(to: url, hello: makeHello(token: token))
        appLog.info("连接页试连：连上了")
        return nil
    } catch let failure as ConnectFailure {
        appLog.warning("连接页试连没连上（\(failure)）")
        return failure.message
    } catch {
        return "连不上：\(error)"
    }
}

func makeHello(token: String) -> UpstreamMessage.Hello {
    UpstreamMessage.Hello(
        v: protocolVersion,
        token: token,
        appVersion: appVersion(),
        sigExpiresAt: wireTimestamp(signatureExpiryForHost())
    )
}

func appVersion() -> String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
}

func signatureExpiryDate() -> Date? {
    guard let profileURL = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
          let profileData = try? Data(contentsOf: profileURL)
    else {
        return nil
    }
    return provisioningProfileExpiry(profileData)
}

private func signatureExpiryForHost() -> Date {
    if let expiry = signatureExpiryDate() {
        return expiry
    }
    appLog.warning("读不到签名到期时间，按现在报给主机")
    return .now
}
