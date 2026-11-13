
import Foundation
import Observation
import Synchronization

public enum AudioInterruption: Sendable {
    case began
    case ended
}

public struct MicrophoneStreams: Sendable {
    public let packets: AsyncStream<Data>
    public let speechStarts: AsyncStream<Void>
    public let interruptions: AsyncStream<AudioInterruption>

    public init(
        packets: AsyncStream<Data>,
        speechStarts: AsyncStream<Void>,
        interruptions: AsyncStream<AudioInterruption>
    ) {
        self.packets = packets
        self.speechStarts = speechStarts
        self.interruptions = interruptions
    }
}

@MainActor
public protocol PhoneAudio: AnyObject {
    func requestMicrophonePermission() async -> Bool
    func start() throws -> MicrophoneStreams
    func resume() throws
    func play(_ packet: Data)
    func stopPlayback()
    func microphoneLevel() -> Float
    func stop()
}

@MainActor
@Observable
public final class VoiceSession {
    public struct Timing: Sendable {
        public var roundTripInterval: Duration
        public var trafficReportInterval: Duration
        public var idleTimeout: Duration
        public var reconnectDelays: [Duration]
        public var notYouHintDuration: Duration

        public static let standard = Timing()

        public init(
            roundTripInterval: Duration = .seconds(30),
            trafficReportInterval: Duration = .seconds(60),
            idleTimeout: Duration = .seconds(180),
            reconnectDelays: [Duration] = [.zero, .seconds(2), .seconds(5)],
            notYouHintDuration: Duration = .seconds(4)
        ) {
            self.roundTripInterval = roundTripInterval
            self.trafficReportInterval = trafficReportInterval
            self.idleTimeout = idleTimeout
            self.reconnectDelays = reconnectDelays
            self.notYouHintDuration = notYouHintDuration
        }
    }

    public enum Phase: Equatable, Sendable {
        case notConnected
        case connecting
        case connected(DownstreamMessage.ShownState)
        case paused
        case reconnecting
    }

    public enum Ending: Equatable, Sendable {
        case byUser
        case idleTimeout
        case microphoneDenied
        case connectFailed(ConnectFailure)
        case audioUnavailable
        case reconnectFailed(ConnectFailure)
        case interrupted
    }

    public private(set) var phase: Phase = .notConnected
    public private(set) var lastEnding: Ending?
    public private(set) var lastConnectDuration: Duration?
    public private(set) var lastRoundTrip: Duration?
    public private(set) var isShowingNotYou = false
    public private(set) var idleDeadline: Date?
    public private(set) var connectedAt: Date?

    private let audio: any PhoneAudio
    private let log: DiagnosticLog
    private let timing: Timing
    @ObservationIgnored public var idleTimeout: Duration
    @ObservationIgnored private var channel: (any HostChannel)?
    @ObservationIgnored private var requestedEnding: Ending?
    private var isEndRequested: Bool { requestedEnding != nil }
    @ObservationIgnored private var idleTimer: Task<Void, Never>?
    @ObservationIgnored private var notYouHintTimer: Task<Void, Never>?
    @ObservationIgnored private var isDiscardingAudio = false
    @ObservationIgnored private var hostState: DownstreamMessage.ShownState = .idle
    @ObservationIgnored private var isAudioTakenAway = false
    @ObservationIgnored private var reconnectWait: Task<Void, Never>?

    private let location: (any PhoneLocation)?
    @ObservationIgnored private var locationReporter = LocationReporter()
    @ObservationIgnored public weak var chat: (any ChatFrameReceiver)?

    public init(
        audio: any PhoneAudio,
        location: (any PhoneLocation)? = nil,
        log: DiagnosticLog = appLog,
        timing: Timing = .standard
    ) {
        self.audio = audio
        self.location = location
        self.log = log
        self.timing = timing
        self.idleTimeout = timing.idleTimeout
    }

    public func start(hello: UpstreamMessage.Hello, connect: @escaping @Sendable () -> any HostChannel) {
        guard phase == .notConnected else {
            return
        }
        show(.connecting)
        lastEnding = nil
        requestedEnding = nil
        chat?.voiceSessionWillConnect()
        Task {
            let ending = await run(hello: hello, connect: connect)
            audio.stop()
            channel = nil
            connectedAt = nil
            show(.notConnected)
            lastEnding = ending
            log.info("会话结束：\(Self.describe(ending))")
            chat?.voiceSessionDidEnd()
        }
    }

    private func show(_ newPhase: Phase) {
        if newPhase != phase {
            hideNotYouHint()
        }
        let wasIdle = phase == .connected(.idle)
        let isIdle = newPhase == .connected(.idle)
        phase = newPhase
        guard isIdle != wasIdle else {
            return
        }
        idleTimer?.cancel()
        idleTimer = nil
        idleDeadline = nil
        if isIdle {
            let idleTimeout = self.idleTimeout
            idleDeadline = .now.addingTimeInterval(idleTimeout / .seconds(1))
            idleTimer = Task { [weak self] in
                try? await Task.sleep(for: idleTimeout)
                guard !Task.isCancelled else {
                    return
                }
                self?.endAfterIdle()
            }
        }
    }

    private func showNotYouHint() {
        isShowingNotYou = true
        notYouHintTimer?.cancel()
        let duration = timing.notYouHintDuration
        notYouHintTimer = Task { [weak self] in
            try? await Task.sleep(for: duration)
            guard !Task.isCancelled else {
                return
            }
            self?.isShowingNotYou = false
        }
    }

    private func hideNotYouHint() {
        notYouHintTimer?.cancel()
        notYouHintTimer = nil
        isShowingNotYou = false
    }

    private func endAfterIdle() {
        guard phase == .connected(.idle), !isEndRequested else {
            return
        }
        log.info("待命 \(Self.describeSeconds(idleTimeout)) 秒没人说话，自动结束")
        requestEnding(.idleTimeout)
    }

    private func requestEnding(_ ending: Ending) {
        requestedEnding = ending
        channel?.close()
        reconnectWait?.cancel()
    }

    private static func describe(_ ending: Ending) -> String {
        switch ending {
        case .byUser: "你结束的"
        case .idleTimeout: "待命太久没人说话"
        case .microphoneDenied: "没有麦克风权限"
        case .connectFailed(let failure): "没连上（\(failure)）"
        case .audioUnavailable: "麦克风打不开"
        case .reconnectFailed(let failure): "连接断了，重连没连上（\(failure)）"
        case .interrupted: "被来电之类打断，没接上"
        }
    }

    public func microphoneLevel() -> Float {
        audio.microphoneLevel()
    }

    public func end() {
        guard phase != .notConnected, !isEndRequested else {
            return
        }
        requestEnding(.byUser)
    }

    private func run(
        hello: UpstreamMessage.Hello,
        connect: @Sendable () -> any HostChannel
    ) async -> Ending {
        guard await audio.requestMicrophonePermission() else {
            return requestedEnding ?? .microphoneDenied
        }
        if let requestedEnding {
            return requestedEnding
        }

        let firstChannel: any HostChannel
        switch await openChannel(hello: hello, connect: connect) {
        case .success(let opened):
            firstChannel = opened
        case .failure(let failure):
            return requestedEnding ?? .connectFailed(failure)
        }

        let microphone: MicrophoneStreams
        do {
            microphone = try audio.start()
        } catch {
            firstChannel.close()
            log.error("声音打不开：\(error)")
            return .audioUnavailable
        }
        hostState = .idle
        isDiscardingAudio = false
        isAudioTakenAway = false
        connectedAt = .now
        show(.connected(.idle))
        locationReporter = LocationReporter()
        let locationFixes = location?.start()
        defer { location?.stop() }

        let traffic = TrafficCounter()
        let uplink = CurrentChannel()
        return await withTaskGroup(of: Void.self, returning: Ending.self) { group in
            group.addTask {
                for await packet in microphone.packets {
                    guard let channel = uplink.current else {
                        continue
                    }
                    do {
                        try await channel.sendAudio(packet)
                        traffic.countSent()
                    } catch {
                    }
                }
            }
            group.addTask {
                for await _ in microphone.speechStarts {
                    if let fix = await self.latestLocationToReport(), let channel = uplink.current {
                        try? await channel.send(try Self.locationFrame(fix))
                    }
                }
            }
            if let locationFixes {
                group.addTask {
                    for await fix in locationFixes {
                        if let fixToReport = await self.locationToReport(after: fix), let channel = uplink.current {
                            try? await channel.send(try Self.locationFrame(fixToReport))
                        }
                    }
                }
            }
            group.addTask {
                for await interruption in microphone.interruptions {
                    switch interruption {
                    case .began:
                        if await self.pause(), let channel = uplink.current {
                            try? await channel.send(try Self.bargeInFrame())
                        }
                    case .ended:
                        await self.resumeAfterInterruption()
                    }
                }
            }
            group.addTask { [log, timing] in
                await Self.reportTraffic(traffic, every: timing.trafficReportInterval, to: log)
            }
            defer { group.cancelAll() }

            var channel = firstChannel
            while true {
                uplink.use(channel)
                chat?.voiceSessionConnectionChanged(channel)
                if let fix = locationReporter.reportLatest() {
                    try? await channel.send(try Self.locationFrame(fix))
                }
                await stayConnected(to: channel, countingInto: traffic)
                uplink.use(nil)
                chat?.voiceSessionConnectionChanged(nil)
                channel.close()
                if let requestedEnding {
                    return requestedEnding
                }
                if phase == .paused {
                    return .interrupted
                }
                switch await reconnect(hello: hello, connect: connect) {
                case .success(let reconnected):
                    channel = reconnected
                case .failure(let failure):
                    return requestedEnding ?? .reconnectFailed(failure)
                }
            }
        }
    }

    private func openChannel(
        hello: UpstreamMessage.Hello,
        connect: @Sendable () -> any HostChannel
    ) async -> Result<any HostChannel, ConnectFailure> {
        let startedAt = ContinuousClock.now
        let channel = connect()
        self.channel = channel
        do {
            _ = try await shakeHands(over: channel, hello: hello, timeout: connectTimeout)
            lastConnectDuration = ContinuousClock.now - startedAt
            return .success(channel)
        } catch {
            channel.close()
            guard let failure = error as? ConnectFailure else {
                log.error("hello 不合协议：\(error)")
                return .failure(.unexpectedReply)
            }
            return .failure(failure)
        }
    }

    private func stayConnected(to channel: any HostChannel, countingInto traffic: TrafficCounter) async {
        let logMessages = log.startUploading()
        defer { log.stopUploading() }
        if let lastConnectDuration {
            log.info("连上主机，用了 \(Self.describeSeconds(lastConnectDuration)) 秒")
        }
        let roundTripMeter = Task { [weak self] in
            await self?.measureRoundTrips(over: channel)
        }
        defer { roundTripMeter.cancel() }

        await withTaskGroup(of: Void.self) { group in
            group.addTask {
                for await message in logMessages {
                    do {
                        try await channel.send(try encodeUpstream(.log(message)))
                    } catch {
                        return
                    }
                }
            }
            defer { group.cancelAll() }
            try? await receiveUntilDisconnected(channel, countingInto: traffic)
        }
    }

    private func reconnect(
        hello: UpstreamMessage.Hello,
        connect: @Sendable () -> any HostChannel
    ) async -> Result<any HostChannel, ConnectFailure> {
        audio.stopPlayback()
        show(.reconnecting)
        var lastFailure = ConnectFailure.hostUnreachable
        for (index, delay) in timing.reconnectDelays.enumerated() {
            await waitBeforeReconnecting(delay)
            if isEndRequested {
                return .failure(lastFailure)
            }
            log.info("连接断了，第 \(index + 1) 次重连")
            switch await openChannel(hello: hello, connect: connect) {
            case .success(let channel):
                hostState = .idle
                isDiscardingAudio = false
                show(isAudioTakenAway ? .paused : .connected(.idle))
                return .success(channel)
            case .failure(let failure):
                if isEndRequested {
                    return .failure(failure)
                }
                lastFailure = failure
                log.warning("第 \(index + 1) 次重连没连上：\(failure)")
            }
        }
        return .failure(lastFailure)
    }

    private func waitBeforeReconnecting(_ delay: Duration) async {
        guard delay > .zero else {
            return
        }
        let wait = Task {
            _ = try? await Task.sleep(for: delay)
        }
        reconnectWait = wait
        await wait.value
        reconnectWait = nil
    }

    private func measureRoundTrips(over channel: any HostChannel) async {
        var unansweredInARow = 0
        while !Task.isCancelled {
            do {
                lastRoundTrip = try await channel.measureRoundTrip(timeout: .seconds(5))
                unansweredInARow = 0
            } catch ConnectFailure.timedOut {
                guard !Task.isCancelled else {
                    return
                }
                unansweredInARow += 1
                if unansweredInARow >= 2 {
                    log.warning("连着两次 ping 不回，当连接断了")
                    channel.close()
                    return
                }
                continue
            } catch {
                return
            }
            try? await Task.sleep(for: timing.roundTripInterval)
        }
    }

    private nonisolated static func reportTraffic(
        _ traffic: TrafficCounter, every interval: Duration, to log: DiagnosticLog
    ) async {
        while true {
            try? await Task.sleep(for: interval)
            guard !Task.isCancelled else {
                return
            }
            let counts = traffic.takeCounts()
            log.info("过去 \(Self.describeSeconds(interval)) 秒：发了 \(counts.sent) 包声音，收了 \(counts.received) 包")
        }
    }

    private nonisolated static func describeSeconds(_ duration: Duration) -> String {
        (duration / .seconds(1)).formatted(.number.precision(.fractionLength(0...1)))
    }

    private func stopPlayingInterruptedReply() {
        audio.stopPlayback()
        isDiscardingAudio = true
    }

    private nonisolated static func bargeInFrame() throws -> String {
        try encodeUpstream(.bargeIn(.init(ts: wireTimestamp(.now))))
    }

    private nonisolated static func locationFrame(_ fix: LocationFix) throws -> String {
        try encodeUpstream(.location(UpstreamMessage.Location(fix)))
    }

    private func locationToReport(after fix: LocationFix) -> LocationFix? {
        locationReporter.fixArrived(fix)
    }

    private func latestLocationToReport() -> LocationFix? {
        locationReporter.reportLatest()
    }

    private func pause() -> Bool {
        isAudioTakenAway = true
        let wasSpeaking = phase == .connected(.speaking)
        stopPlayingInterruptedReply()
        if case .connected = phase {
            show(.paused)
        }
        log.info("声音被来电之类占走了，会话暂停（当时\(wasSpeaking ? "在说" : "没在说")）")
        return wasSpeaking
    }

    private func resumeAfterInterruption() {
        guard isAudioTakenAway, !isEndRequested else {
            return
        }
        do {
            try audio.resume()
        } catch {
            log.error("来电之类结束了，声音拿不回来：\(error)")
            requestEnding(.interrupted)
            return
        }
        isAudioTakenAway = false
        log.info("来电之类结束了，会话接着")
        if phase == .paused {
            show(.connected(hostState))
        }
    }

    private func receiveUntilDisconnected(_ channel: any HostChannel, countingInto traffic: TrafficCounter) async throws {
        while true {
            switch try await channel.receive() {
            case .audio(let packet):
                traffic.countReceived()
                if !isDiscardingAudio && phase != .paused {
                    audio.play(packet)
                }
            case .text(let frameText):
                handle(frameText)
            }
        }
    }

    private func handle(_ frameText: String) {
        let message: DownstreamMessage
        do {
            message = try decodeDownstream(frameText)
        } catch {
            log.warning("主机发来的一条消息读不懂，丢掉：\(error)")
            return
        }
        switch message {
        case .state(let state):
            hostState = state.state
            guard phase != .paused else {
                return
            }
            if phase == .connected(.speaking), state.state == .listening {
                stopPlayingInterruptedReply()
            }
            show(.connected(state.state))
        case .playBegin:
            isDiscardingAudio = false
        case .playEnd:
            break
        case .notYou:
            showNotYouHint()
        case .text, .textEnd, .history, .confirmCard:
            chat?.receive(message)
        case .welcome, .bye:
            break
        }
    }
}

private final class CurrentChannel: Sendable {
    private let channel = Mutex<(any HostChannel)?>(nil)

    var current: (any HostChannel)? {
        channel.withLock { $0 }
    }

    func use(_ newChannel: (any HostChannel)?) {
        channel.withLock { $0 = newChannel }
    }
}

private final class TrafficCounter: Sendable {
    private struct Counts {
        var sent = 0
        var received = 0
    }

    private let counts = Mutex(Counts())

    func countSent() {
        counts.withLock { $0.sent += 1 }
    }

    func countReceived() {
        counts.withLock { $0.received += 1 }
    }

    func takeCounts() -> (sent: Int, received: Int) {
        counts.withLock { counts in
            let taken = (sent: counts.sent, received: counts.received)
            counts = Counts()
            return taken
        }
    }
}
