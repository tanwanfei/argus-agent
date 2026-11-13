
import Foundation

public struct LocationFix: Sendable, Equatable {
    public let latitude: Double
    public let longitude: Double
    public let accuracy: Double
    public let timestamp: Date
    public let speed: Double

    public init(latitude: Double, longitude: Double, accuracy: Double, timestamp: Date, speed: Double) {
        self.latitude = latitude
        self.longitude = longitude
        self.accuracy = accuracy
        self.timestamp = timestamp
        self.speed = speed
    }

    public var isMoving: Bool {
        speed >= 1
    }
}

@MainActor
public protocol PhoneLocation: AnyObject {
    func start() -> AsyncStream<LocationFix>
    func stop()
    func currentFix(within timeout: Duration) async -> LocationFix?
}

struct LocationReporter {
    static let movedDistance: Double = 200

    private(set) var latestFix: LocationFix?
    private var lastReportedFix: LocationFix?

    mutating func fixArrived(_ fix: LocationFix) -> LocationFix? {
        latestFix = fix
        if let lastReportedFix, distanceInMeters(from: lastReportedFix, to: fix) < Self.movedDistance {
            return nil
        }
        lastReportedFix = fix
        return fix
    }

    mutating func reportLatest() -> LocationFix? {
        if let latestFix {
            lastReportedFix = latestFix
        }
        return latestFix
    }
}

func distanceInMeters(from start: LocationFix, to end: LocationFix) -> Double {
    let earthRadius = 6_371_000.0
    let startLatitude = start.latitude * .pi / 180
    let endLatitude = end.latitude * .pi / 180
    let latitudeChange = (end.latitude - start.latitude) * .pi / 180
    let longitudeChange = (end.longitude - start.longitude) * .pi / 180
    let haversine = sin(latitudeChange / 2) * sin(latitudeChange / 2)
        + cos(startLatitude) * cos(endLatitude) * sin(longitudeChange / 2) * sin(longitudeChange / 2)
    return 2 * earthRadius * asin(min(1, sqrt(haversine)))
}

extension UpstreamMessage.Location {
    init(_ fix: LocationFix) {
        self.init(
            lat: fix.latitude,
            lng: fix.longitude,
            accuracy: fix.accuracy,
            ts: wireTimestamp(fix.timestamp),
            moving: fix.isMoving
        )
    }
}

public func reportLocationOnce(
    _ fix: LocationFix,
    to url: URL,
    hello: UpstreamMessage.Hello,
    timeout: Duration = connectTimeout
) async throws {
    try await reportLocationOnce(fix, over: openWebSocket(to: url), hello: hello, timeout: timeout)
}

func reportLocationOnce(
    _ fix: LocationFix,
    over channel: some HostChannel,
    hello: UpstreamMessage.Hello,
    timeout: Duration
) async throws {
    defer { channel.close() }
    _ = try await shakeHands(over: channel, hello: hello, timeout: timeout)
    try await channel.send(encodeUpstream(.location(UpstreamMessage.Location(fix))))
    _ = try? await channel.measureRoundTrip(timeout: .seconds(3))
}
