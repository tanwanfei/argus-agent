
import Foundation

public func pairOnce(
    publicKeyDER: Data,
    to url: URL,
    hello: UpstreamMessage.Hello,
    timeout: Duration = connectTimeout
) async throws {
    try await pairOnce(publicKeyDER: publicKeyDER, over: openWebSocket(to: url), hello: hello, timeout: timeout)
}

func pairOnce(
    publicKeyDER: Data,
    over channel: some HostChannel,
    hello: UpstreamMessage.Hello,
    timeout: Duration
) async throws {
    defer { channel.close() }
    _ = try await shakeHands(over: channel, hello: hello, timeout: timeout)
    let pair = UpstreamMessage.Pair(pubkey: publicKeyDER.base64EncodedString())
    try await channel.send(encodeUpstream(.pair(pair)))
    _ = try? await channel.measureRoundTrip(timeout: .seconds(3))
}
