import ArgusCore
import CoreLocation
import Observation

enum LocationPermission {
    case notAsked
    case allowed
    case denied
}

@MainActor
@Observable
final class PhoneLocationManager: NSObject, PhoneLocation, CLLocationManagerDelegate {
    private(set) var permission: LocationPermission = .notAsked
    private(set) var isAlwaysAllowed = false
    @ObservationIgnored private let manager = CLLocationManager()
    @ObservationIgnored private var fixContinuation: AsyncStream<LocationFix>.Continuation?
    @ObservationIgnored private var fixWaiters: [UUID: AsyncStream<LocationFix?>.Continuation] = [:]

    private static let freshEnough: TimeInterval = 60

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.pausesLocationUpdatesAutomatically = false
        permission = Self.permission(from: manager.authorizationStatus)
        isAlwaysAllowed = manager.authorizationStatus == .authorizedAlways
    }

    func requestPermission() {
        manager.requestWhenInUseAuthorization()
    }

    func requestAlwaysPermission() {
        manager.requestAlwaysAuthorization()
    }

    func start() -> AsyncStream<LocationFix> {
        let (fixes, continuation) = AsyncStream.makeStream(of: LocationFix.self, bufferingPolicy: .bufferingNewest(1))
        guard permission == .allowed else {
            appLog.info("没有定位权限，这次会话不报位置")
            continuation.finish()
            return fixes
        }
        fixContinuation = continuation
        manager.allowsBackgroundLocationUpdates = true
        manager.startUpdatingLocation()
        return fixes
    }

    func stop() {
        manager.stopUpdatingLocation()
        manager.allowsBackgroundLocationUpdates = false
        fixContinuation?.finish()
        fixContinuation = nil
    }

    func currentFix(within timeout: Duration) async -> LocationFix? {
        guard permission == .allowed else {
            return nil
        }
        if let cached = manager.location.flatMap(Self.makeFix), -cached.timestamp.timeIntervalSinceNow < Self.freshEnough {
            return cached
        }
        let waiterID = UUID()
        let (answer, continuation) = AsyncStream.makeStream(of: LocationFix?.self)
        fixWaiters[waiterID] = continuation
        manager.requestLocation()
        Task { [weak self] in
            try? await Task.sleep(for: timeout)
            self?.finishWaiting(waiterID, with: nil)
        }
        for await fix in answer {
            return fix
        }
        return nil
    }

    private func finishWaiting(_ waiterID: UUID, with fix: LocationFix?) {
        guard let waiter = fixWaiters.removeValue(forKey: waiterID) else {
            return
        }
        waiter.yield(fix)
        waiter.finish()
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        MainActor.assumeIsolated {
            permission = Self.permission(from: status)
            isAlwaysAllowed = status == .authorizedAlways
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let newFixes = locations.compactMap(Self.makeFix)
        MainActor.assumeIsolated {
            for fix in newFixes {
                fixContinuation?.yield(fix)
            }
            if let latest = newFixes.last {
                for waiterID in Array(fixWaiters.keys) {
                    finishWaiting(waiterID, with: latest)
                }
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: any Error) {
        if (error as? CLError)?.code == .locationUnknown {
            return
        }
        appLog.warning("定位出错：\(error.localizedDescription)")
    }

    nonisolated static func makeFix(_ location: CLLocation) -> LocationFix? {
        guard location.horizontalAccuracy >= 0 else {
            return nil
        }
        return LocationFix(
            latitude: location.coordinate.latitude,
            longitude: location.coordinate.longitude,
            accuracy: location.horizontalAccuracy,
            timestamp: location.timestamp,
            speed: location.speed
        )
    }

    private nonisolated static func permission(from status: CLAuthorizationStatus) -> LocationPermission {
        switch status {
        case .notDetermined: .notAsked
        case .authorizedWhenInUse, .authorizedAlways: .allowed
        case .denied, .restricted: .denied
        @unknown default: .denied
        }
    }
}
