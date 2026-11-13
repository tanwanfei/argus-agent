import ArgusCore
import AVFoundation
import Synchronization

@MainActor
final class PhoneAudioEngine: PhoneAudio {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var microphonePackets: AsyncStream<Data>.Continuation?
    private var speechStarts: AsyncStream<Void>.Continuation?
    private var interruptions: AsyncStream<AudioInterruption>.Continuation?
    private var isInterrupted = false
    private var notificationObservers: [any NSObjectProtocol] = []
    private var decoder: OpusDecoder?
    private let playbackFormat = AVAudioFormat(standardFormatWithSampleRate: hostSampleRate, channels: 1)
    private var hasReportedBadPacket = false
    private let levelMeter = LevelMeter()

    init() {
        engine.attach(player)
    }

    func microphoneLevel() -> Float {
        levelMeter.read()
    }

    func requestMicrophonePermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    func start() throws -> MicrophoneStreams {
        let (packets, packetsContinuation) = AsyncStream.makeStream(
            of: Data.self, bufferingPolicy: .bufferingNewest(50)
        )
        let (speechStarts, speechStartsContinuation) = AsyncStream.makeStream(
            of: Void.self, bufferingPolicy: .bufferingNewest(1)
        )
        let (interruptions, interruptionsContinuation) = AsyncStream.makeStream(of: AudioInterruption.self)
        microphonePackets = packetsContinuation
        self.speechStarts = speechStartsContinuation
        self.interruptions = interruptionsContinuation
        do {
            let session = AVAudioSession.sharedInstance()
            var options: AVAudioSession.CategoryOptions = [.allowBluetoothHFP]
            if !PhonePreferences.usesEarpiece {
                options.insert(.defaultToSpeaker)
            }
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
            try session.setActive(true)
            try startEngine(packets: packetsContinuation, speechStarts: speechStartsContinuation)
        } catch {
            stop()
            throw error
        }
        observeSystemAudioChanges()
        return MicrophoneStreams(packets: packets, speechStarts: speechStarts, interruptions: interruptions)
    }

    func resume() throws {
        try AVAudioSession.sharedInstance().setActive(true)
        try restartEngine()
        isInterrupted = false
    }

    private func restartEngine() throws {
        guard let microphonePackets, let speechStarts else {
            return
        }
        player.stop()
        engine.stop()
        try startEngine(packets: microphonePackets, speechStarts: speechStarts)
    }

    private func startEngine(
        packets: AsyncStream<Data>.Continuation,
        speechStarts: AsyncStream<Void>.Continuation
    ) throws {
        try engine.inputNode.setVoiceProcessingEnabled(true)
        let inputFormat = engine.inputNode.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw PhoneAudioError.noMicrophone
        }
        guard let playbackFormat else {
            throw PhoneAudioError.noPlaybackFormat
        }
        decoder = try OpusDecoder()
        hasReportedBadPacket = false
        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)

        let uplink = MicrophoneUplink(
            encoder: try OpusEncoder(inputSampleRate: inputFormat.sampleRate),
            speechOnsetDetector: SpeechOnsetDetector(sampleRate: inputFormat.sampleRate),
            sampleRate: inputFormat.sampleRate,
            packets: packets,
            speechStarts: speechStarts,
            levelMeter: levelMeter
        )
        engine.inputNode.removeTap(onBus: 0)
        engine.inputNode.installTap(
            onBus: 0, bufferSize: 4096, format: inputFormat, block: Self.makeTapBlock(uplink)
        )
        try engine.start()
        player.play()
        appLog.info("开麦：\(Int(inputFormat.sampleRate))Hz，\(inputFormat.channelCount) 个声道")
    }

    private func observeSystemAudioChanges() {
        let center = NotificationCenter.default
        let interruptionObserver = center.addObserver(
            forName: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(), queue: .main
        ) { [weak self] notification in
            let rawType = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let rawOptions = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt
            MainActor.assumeIsolated {
                self?.handleInterruption(rawType: rawType, rawOptions: rawOptions)
            }
        }
        let configurationObserver = center.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.restartAfterConfigurationChange()
            }
        }
        notificationObservers = [interruptionObserver, configurationObserver]
    }

    private func handleInterruption(rawType: UInt?, rawOptions: UInt?) {
        guard let rawType, let type = AVAudioSession.InterruptionType(rawValue: rawType) else {
            return
        }
        switch type {
        case .began:
            isInterrupted = true
            player.stop()
            levelMeter.update(0)
            appLog.info("声音被占走了（来电、Siri 之类）")
            interruptions?.yield(.began)
        case .ended:
            let shouldResume = AVAudioSession.InterruptionOptions(rawValue: rawOptions ?? 0).contains(.shouldResume)
            appLog.info("声音不占了，系统\(shouldResume ? "建议" : "没建议")接着")
            interruptions?.yield(.ended)
        @unknown default:
            break
        }
    }

    private func restartAfterConfigurationChange() {
        guard !engine.isRunning, !isInterrupted, microphonePackets != nil else {
            return
        }
        appLog.info("声音设备变了，重开麦克风")
        do {
            try restartEngine()
        } catch {
            appLog.error("声音设备变了以后麦克风开不起来：\(error)")
        }
    }

    func stopPlayback() {
        player.stop()
        if engine.isRunning {
            player.play()
        }
    }

    func play(_ packet: Data) {
        guard let decoder, let playbackFormat, engine.isRunning else {
            return
        }
        let samples: [Float]
        do {
            samples = try decoder.decode(packet)
        } catch {
            if !hasReportedBadPacket {
                hasReportedBadPacket = true
                appLog.warning("主机发来的音频包解不开，丢掉：\(error)")
            }
            return
        }
        guard !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: playbackFormat, frameCapacity: AVAudioFrameCount(samples.count)),
              let channel = buffer.floatChannelData?[0]
        else {
            return
        }
        for (index, sample) in samples.enumerated() {
            channel[index] = sample
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        player.scheduleBuffer(buffer, completionHandler: nil)
    }

    func stop() {
        for observer in notificationObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        notificationObservers = []
        isInterrupted = false
        player.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        decoder = nil
        microphonePackets?.finish()
        microphonePackets = nil
        speechStarts?.finish()
        speechStarts = nil
        interruptions?.finish()
        interruptions = nil
        levelMeter.update(0)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private nonisolated static func makeTapBlock(_ uplink: MicrophoneUplink) -> AVAudioNodeTapBlock {
        return { buffer, _ in
            uplink.accept(buffer)
        }
    }
}

private final class MicrophoneUplink: @unchecked Sendable {
    private let encoder: OpusEncoder
    private var speechOnsetDetector: SpeechOnsetDetector
    private let sampleRate: Double
    private let packets: AsyncStream<Data>.Continuation
    private let speechStarts: AsyncStream<Void>.Continuation
    private let levelMeter: LevelMeter
    private var hasReportedProblem = false

    init(
        encoder: OpusEncoder,
        speechOnsetDetector: SpeechOnsetDetector,
        sampleRate: Double,
        packets: AsyncStream<Data>.Continuation,
        speechStarts: AsyncStream<Void>.Continuation,
        levelMeter: LevelMeter
    ) {
        self.encoder = encoder
        self.speechOnsetDetector = speechOnsetDetector
        self.sampleRate = sampleRate
        self.packets = packets
        self.speechStarts = speechStarts
        self.levelMeter = levelMeter
    }

    func accept(_ buffer: AVAudioPCMBuffer) {
        guard buffer.format.sampleRate == sampleRate, let channels = buffer.floatChannelData else {
            reportOnce("麦克风的格式变了（\(Int(buffer.format.sampleRate))Hz），这段声音发不出去")
            return
        }
        let samples = Array(UnsafeBufferPointer(start: channels[0], count: Int(buffer.frameLength)))
        levelMeter.update(loudness(of: samples))
        if speechOnsetDetector.feed(samples) {
            speechStarts.yield(())
        }
        do {
            for packet in try encoder.encode(samples) {
                packets.yield(packet)
            }
        } catch {
            reportOnce("编码失败：\(error)")
        }
    }

    private func reportOnce(_ problem: String) {
        guard !hasReportedProblem else { return }
        hasReportedProblem = true
        appLog.error(problem)
    }
}

private final class LevelMeter: Sendable {
    private struct Reading {
        var previous: Float = 0
        var latest: Float = 0
        var updatedAt = ContinuousClock.now
    }

    private static let transition: Duration = .milliseconds(100)
    private let reading = Mutex(Reading())

    func update(_ level: Float) {
        let now = ContinuousClock.now
        reading.withLock { reading in
            reading.previous = Self.shown(reading, at: now)
            reading.latest = level
            reading.updatedAt = now
        }
    }

    func read() -> Float {
        let now = ContinuousClock.now
        return reading.withLock { Self.shown($0, at: now) }
    }

    private static func shown(_ reading: Reading, at now: ContinuousClock.Instant) -> Float {
        let progress = min((now - reading.updatedAt) / transition, 1)
        return reading.previous + (reading.latest - reading.previous) * Float(progress)
    }
}

private enum PhoneAudioError: LocalizedError {
    case noMicrophone
    case noPlaybackFormat

    var errorDescription: String? {
        switch self {
        case .noMicrophone: "没找到能用的麦克风"
        case .noPlaybackFormat: "建不出放音用的格式"
        }
    }
}
