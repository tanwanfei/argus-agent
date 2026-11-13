
import Foundation
import os
import Synchronization

public let appLog = DiagnosticLog()

public final class DiagnosticLog: Sendable {
    public typealias Level = UpstreamMessage.Log.Level

    public struct Entry: Sendable, Equatable {
        public let at: Date
        public let level: Level
        public let message: String
    }

    static let keptEntries = 200
    static let backlogToUpload = 50

    private struct State {
        var entries: [Entry] = []
        var notUploaded: [Entry] = []
        var uploader: AsyncStream<UpstreamMessage.Log>.Continuation?
        var uploadGeneration = 0
    }

    private let state = Mutex(State())
    private let systemLogger = Logger(subsystem: "local.argus.app", category: "app")

    public init() {}

    public func debug(_ message: String) { record(.debug, message) }
    public func info(_ message: String) { record(.info, message) }
    public func warning(_ message: String) { record(.warning, message) }
    public func error(_ message: String) { record(.error, message) }

    public func record(_ level: Level, _ message: String) {
        guard !message.isEmpty else {
            return
        }
        systemLogger.log(level: Self.systemLevel(level), "\(message, privacy: .public)")
        let entry = Entry(at: .now, level: level, message: message)
        state.withLock { state in
            state.entries.append(entry)
            if state.entries.count > Self.keptEntries {
                state.entries.removeFirst(state.entries.count - Self.keptEntries)
            }
            if let uploader = state.uploader {
                uploader.yield(entry.upstreamMessage(recordedBeforeConnecting: false))
            } else {
                state.notUploaded.append(entry)
                if state.notUploaded.count > Self.backlogToUpload {
                    state.notUploaded.removeFirst(state.notUploaded.count - Self.backlogToUpload)
                }
            }
        }
    }

    public func recentEntries() -> [Entry] {
        state.withLock { $0.entries }
    }

    public func startUploading() -> AsyncStream<UpstreamMessage.Log> {
        let (messages, continuation) = AsyncStream.makeStream(of: UpstreamMessage.Log.self)
        let (generation, previousUploader) = state.withLock { state in
            for entry in state.notUploaded {
                continuation.yield(entry.upstreamMessage(recordedBeforeConnecting: true))
            }
            state.notUploaded.removeAll()
            let previousUploader = state.uploader
            state.uploader = continuation
            state.uploadGeneration += 1
            return (state.uploadGeneration, previousUploader)
        }
        previousUploader?.finish()
        continuation.onTermination = { [weak self] _ in
            self?.state.withLock { state in
                if state.uploadGeneration == generation {
                    state.uploader = nil
                }
            }
        }
        return messages
    }

    public func stopUploading() {
        let uploader = state.withLock { state in
            let uploader = state.uploader
            state.uploader = nil
            return uploader
        }
        uploader?.finish()
    }

    private static func systemLevel(_ level: Level) -> OSLogType {
        switch level {
        case .debug: .debug
        case .info: .info
        case .warning: .default
        case .error: .error
        }
    }
}

extension DiagnosticLog.Entry {
    func upstreamMessage(recordedBeforeConnecting: Bool) -> UpstreamMessage.Log {
        guard recordedBeforeConnecting else {
            return UpstreamMessage.Log(level: level, msg: message)
        }
        let recordedAt = at.formatted(date: .omitted, time: .standard)
        return UpstreamMessage.Log(level: level, msg: "（连上之前 \(recordedAt) 记的）\(message)")
    }
}
