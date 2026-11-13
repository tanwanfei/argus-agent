
import Foundation
import Synchronization

public let protocolVersion = 1

public let connectTimeout: Duration = .seconds(15)

public let slowConnectThreshold: Duration = .seconds(5)

public enum ConnectFailure: Error, Equatable, Sendable {
    case hostNotFound
    case argusNotRunning
    case hostUnreachable
    case timedOut
    case tokenRejected
    case versionMismatch
    case closedByHost
    case unexpectedReply

    public var message: String {
        switch self {
        case .hostNotFound: "找不到主机，Tailscale 开着吗？"
        case .argusNotRunning: "主机上的阿格斯没在跑"
        case .hostUnreachable, .timedOut: "主机没开机或不在线"
        case .tokenRejected: "Token 不对，检查主机上的 .env"
        case .versionMismatch: "App 和主机的版本对不上，要更新一边"
        case .closedByHost: "主机断开了连接，过一会儿再试"
        case .unexpectedReply: "主机回的消息不对，看主机日志"
        }
    }

    public static func guess(closeCode: Int?, httpStatus: Int?, urlErrorCode: URLError.Code?) -> ConnectFailure {
        if let closeCode, closeCode != 1006 {
            switch closeCode {
            case 1008: return .tokenRejected
            case 4001: return .versionMismatch
            default: return .closedByHost
            }
        }
        if httpStatus == 502 {
            return .argusNotRunning
        }
        if urlErrorCode == .cannotFindHost || urlErrorCode == .dnsLookupFailed {
            return .hostNotFound
        }
        return .hostUnreachable
    }
}

public enum HostFrame: Sendable, Equatable {
    case text(String)
    case audio(Data)
}

public protocol HostChannel: Sendable {
    func send(_ frameText: String) async throws
    func sendAudio(_ packet: Data) async throws
    func receive() async throws -> HostFrame
    func measureRoundTrip(timeout: Duration) async throws -> Duration
    func close()
}

public func openWebSocket(to url: URL) -> any HostChannel {
    URLSessionHostChannel(url: url)
}

public func checkConnection(
    to url: URL,
    hello: UpstreamMessage.Hello,
    timeout: Duration = connectTimeout
) async throws -> DownstreamMessage.Welcome {
    let channel = URLSessionHostChannel(url: url)
    defer { channel.close() }
    return try await shakeHands(over: channel, hello: hello, timeout: timeout)
}

public func shakeHands(
    over channel: some HostChannel,
    hello: UpstreamMessage.Hello,
    timeout: Duration
) async throws -> DownstreamMessage.Welcome {
    let helloFrame = try encodeUpstream(.hello(hello))
    return try await withThrowingTaskGroup(
        of: DownstreamMessage.Welcome.self,
        returning: DownstreamMessage.Welcome.self
    ) { group in
        group.addTask {
            try await channel.send(helloFrame)
            return try await awaitWelcome(on: channel)
        }
        group.addTask {
            try await Task.sleep(for: timeout)
            throw ConnectFailure.timedOut
        }
        do {
            guard let welcome = try await group.next() else {
                throw ConnectFailure.hostUnreachable
            }
            group.cancelAll()
            return welcome
        } catch {
            group.cancelAll()
            channel.close()
            throw error
        }
    }
}

private func awaitWelcome(on channel: some HostChannel) async throws -> DownstreamMessage.Welcome {
    guard case .text(let frameText) = try await channel.receive() else {
        throw ConnectFailure.unexpectedReply
    }
    let reply: DownstreamMessage
    do {
        reply = try decodeDownstream(frameText)
    } catch {
        throw ConnectFailure.unexpectedReply
    }
    switch reply {
    case .welcome(let welcome):
        guard welcome.v == protocolVersion else {
            throw ConnectFailure.versionMismatch
        }
        return welcome
    case .bye:
        throw ConnectFailure.versionMismatch
    default:
        throw ConnectFailure.unexpectedReply
    }
}

final class URLSessionHostChannel: HostChannel {
    private let session: URLSession
    private let task: URLSessionWebSocketTask

    init(url: URL) {
        session = URLSession(configuration: .ephemeral)
        task = session.webSocketTask(with: url)
        task.resume()
    }

    func send(_ frameText: String) async throws {
        do {
            try await task.send(.string(frameText))
        } catch {
            throw failure(from: error)
        }
    }

    func sendAudio(_ packet: Data) async throws {
        do {
            try await task.send(.data(packet))
        } catch {
            throw failure(from: error)
        }
    }

    func receive() async throws -> HostFrame {
        let message: URLSessionWebSocketTask.Message
        do {
            message = try await task.receive()
        } catch {
            throw failure(from: error)
        }
        switch message {
        case .string(let frameText):
            return .text(frameText)
        case .data(let packet):
            return .audio(packet)
        @unknown default:
            throw ConnectFailure.unexpectedReply
        }
    }

    func measureRoundTrip(timeout: Duration) async throws -> Duration {
        let sentAt = ContinuousClock.now
        let pong = PongWaiter()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            pong.wait(continuation)
            task.sendPing { error in
                if let error {
                    pong.finish(throwing: self.failure(from: error))
                } else {
                    pong.finish()
                }
            }
            Task {
                try? await Task.sleep(for: timeout)
                pong.finish(throwing: ConnectFailure.timedOut)
            }
        }
        return ContinuousClock.now - sentAt
    }

    func close() {
        task.cancel(with: .normalClosure, reason: nil)
        session.invalidateAndCancel()
    }

    private func failure(from error: any Error) -> ConnectFailure {
        let closeCode = task.closeCode == .invalid ? nil : task.closeCode.rawValue
        let httpStatus = (task.response as? HTTPURLResponse)?.statusCode
        return ConnectFailure.guess(closeCode: closeCode, httpStatus: httpStatus, urlErrorCode: (error as? URLError)?.code)
    }
}

private final class PongWaiter: Sendable {
    private let waiting = Mutex<CheckedContinuation<Void, any Error>?>(nil)

    func wait(_ continuation: CheckedContinuation<Void, any Error>) {
        waiting.withLock { $0 = continuation }
    }

    func finish(throwing error: (any Error)? = nil) {
        let continuation = waiting.withLock { waiting in
            let continuation = waiting
            waiting = nil
            return continuation
        }
        if let error {
            continuation?.resume(throwing: error)
        } else {
            continuation?.resume()
        }
    }
}
