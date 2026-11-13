import ArgusCore
import CoreLocation
import UIKit

@MainActor
final class BackgroundLocationReporter: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private let settings: HostSettings
    private let isHostLinkInUse: @MainActor () -> Bool
    private var betterFixes: AsyncStream<LocationFix>.Continuation?
    private var reporting: Task<Void, Never>?
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid

    private static let accurateEnough: CLLocationAccuracy = 100
    private static let freshEnough: TimeInterval = 60
    private static let betterFixTimeout: Duration = .seconds(8)

    init(settings: HostSettings, isHostLinkInUse: @escaping @MainActor () -> Bool) {
        self.settings = settings
        self.isHostLinkInUse = isHostLinkInUse
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    private func updateMonitoring(_ status: CLAuthorizationStatus) {
        guard status == .authorizedAlways else {
            manager.stopMonitoringSignificantLocationChanges()
            manager.stopMonitoringVisits()
            return
        }
        manager.startMonitoringSignificantLocationChanges()
        manager.startMonitoringVisits()
        appLog.info("后台定位开着：走远了、到了或离开一个地方时报给主机")
    }

    private func woke(with fix: LocationFix?, reason: String) {
        guard reporting == nil else {
            return
        }
        guard !isHostLinkInUse() else {
            appLog.info("后台定位：\(reason)，正连着主机，不另连")
            return
        }
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "后台报位置") { [weak self] in
            MainActor.assumeIsolated { self?.giveUp() }
        }
        reporting = Task {
            await report(wokenFix: fix, reason: reason)
            reporting = nil
            endBackgroundTask()
        }
    }

    private func report(wokenFix: LocationFix?, reason: String) async {
        let fix: LocationFix?
        if let wokenFix, Self.isAccurateAndFresh(wokenFix) {
            fix = wokenFix
        } else {
            fix = await betterFix() ?? wokenFix
        }
        guard let fix else {
            appLog.info("后台定位：\(reason)，没定到，这次不报")
            return
        }
        guard let url = HostAddress.webSocketURL(from: settings.address), let token = settings.savedToken() else {
            return
        }
        do {
            try await reportLocationOnce(fix, to: url, hello: makeHello(token: token))
            appLog.info("后台定位：\(reason)，报给主机了（精度 \(Int(fix.accuracy)) 米）")
        } catch {
            appLog.warning("后台定位：\(reason)，没报成（\(error)）")
        }
    }

    private static func isAccurateAndFresh(_ fix: LocationFix) -> Bool {
        fix.accuracy <= accurateEnough && -fix.timestamp.timeIntervalSinceNow < freshEnough
    }

    private func betterFix() async -> LocationFix? {
        let (fixes, continuation) = AsyncStream.makeStream(of: LocationFix.self)
        betterFixes = continuation
        defer { betterFixes = nil }
        manager.requestLocation()
        let timeout = Task {
            try? await Task.sleep(for: Self.betterFixTimeout)
            continuation.finish()
        }
        defer { timeout.cancel() }
        for await fix in fixes {
            return fix
        }
        return nil
    }

    private func giveUp() {
        appLog.warning("后台定位：系统给的时间到了还没报完，不报了")
        reporting?.cancel()
        endBackgroundTask()
    }

    private func endBackgroundTask() {
        guard backgroundTask != .invalid else {
            return
        }
        UIApplication.shared.endBackgroundTask(backgroundTask)
        backgroundTask = .invalid
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        MainActor.assumeIsolated {
            updateMonitoring(status)
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let latest = locations.last.flatMap(PhoneLocationManager.makeFix) else {
            return
        }
        MainActor.assumeIsolated {
            if let betterFixes {
                betterFixes.yield(latest)
            } else {
                woke(with: latest, reason: "走远了")
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        let reason = visit.departureDate == .distantFuture ? "到了一个地方" : "离开了一个地方"
        MainActor.assumeIsolated {
            woke(with: nil, reason: reason)
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: any Error) {
        let errorNote = (error as? CLError)?.code == .locationUnknown ? nil : error.localizedDescription
        MainActor.assumeIsolated {
            betterFixes?.finish()
            if let errorNote {
                appLog.warning("后台定位出错：\(errorNote)")
            }
        }
    }
}
