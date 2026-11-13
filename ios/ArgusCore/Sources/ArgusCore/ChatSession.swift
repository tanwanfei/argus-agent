
import Foundation
import Observation

public struct ChatMessage: Identifiable, Equatable, Sendable {
    public enum Speaker: Sendable {
        case owner
        case other
        case argus
    }

    public enum Via: Sendable {
        case voice
        case typed
    }

    public private(set) var id: UUID
    public let historyID: Int?
    public let speaker: Speaker
    public internal(set) var text: String
    public let timestamp: Date
    public let via: Via
    public internal(set) var isFailed: Bool
    public internal(set) var confirmation: PermissionPrompt?

    public struct PermissionPrompt: Equatable, Sendable {
        public enum State: Sendable {
            case pending, approved, rejected, expired
        }

        public let ticketID: String
        public let title: String
        public let expiresAt: Date
        public internal(set) var state: State

        init?(_ wire: DownstreamMessage.Confirmation) {
            guard let expiresAt = wire.expiryDate else {
                return nil
            }
            self.ticketID = wire.ticketID
            self.title = wire.title
            self.expiresAt = expiresAt
            self.state = switch wire.state {
            case .pending: .pending
            case .approved: .approved
            case .rejected: .rejected
            case .expired: .expired
            }
        }

        public func shownState(at now: Date) -> State {
            state == .pending && now >= expiresAt ? .expired : state
        }
    }

    init(
        historyID: Int? = nil,
        speaker: Speaker,
        text: String,
        timestamp: Date,
        via: Via,
        isFailed: Bool = false,
        confirmation: PermissionPrompt? = nil
    ) {
        self.id = UUID()
        self.historyID = historyID
        self.speaker = speaker
        self.text = text
        self.timestamp = timestamp
        self.via = via
        self.isFailed = isFailed
        self.confirmation = confirmation
    }

    init?(historyItem item: DownstreamMessage.HistoryItem) {
        guard !item.text.isEmpty, let timestamp = parseWireTimestamp(item.ts) else {
            return nil
        }
        let speaker: Speaker = switch item.who {
        case .owner: .owner
        case .other: .other
        case .argus: .argus
        }
        self.init(
            historyID: item.id,
            speaker: speaker,
            text: item.text,
            timestamp: timestamp,
            via: item.via == .typed ? .typed : .voice,
            confirmation: item.confirmation.flatMap(PermissionPrompt.init)
        )
    }

    func keepingID(of messageInHand: ChatMessage) -> ChatMessage {
        var copy = self
        copy.id = messageInHand.id
        return copy
    }
}

@MainActor
public protocol ChatFrameReceiver: AnyObject {
    func voiceSessionWillConnect()
    func voiceSessionConnectionChanged(_ channel: (any HostChannel)?)
    func receive(_ message: DownstreamMessage)
    func voiceSessionDidEnd()
}

@MainActor
@Observable
public final class ChatSession: ChatFrameReceiver {
    public enum LinkState: Equatable, Sendable {
        case offline
        case connecting
        case online
    }

    public private(set) var messages: [ChatMessage] = []
    public private(set) var hasEarlierHistory = true
    public private(set) var isAwaitingReply = false
    public private(set) var cardQueue: [DownstreamMessage.ConfirmCard] = []
    public var pendingCard: DownstreamMessage.ConfirmCard? {
        cardQueue.first
    }

    public private(set) var isWantedCardGone = false
    @ObservationIgnored private var wantedTicketID: String?
    var cardWait: Duration = .seconds(5)

    public var canAnswerCard: Bool {
        currentChannel != nil
    }

    public var linkState: LinkState {
        if isVoiceSessionRunning {
            return voiceChannel == nil ? .connecting : .online
        }
        return ownLinkState
    }

    public var isLoadingHistory: Bool {
        pendingHistoryRequest != nil
    }

    private let log: DiagnosticLog
    @ObservationIgnored private let location: (any PhoneLocation)?
    private var isOpen = false
    @ObservationIgnored private var hello: UpstreamMessage.Hello?
    @ObservationIgnored private var connect: (@Sendable () -> any HostChannel)?

    private var isVoiceSessionRunning = false
    private var voiceChannel: (any HostChannel)?

    private var ownLinkState: LinkState = .offline
    @ObservationIgnored private var ownChannel: (any HostChannel)?
    @ObservationIgnored private var ownLinkGeneration = 0

    private enum HistoryRequest {
        case latest
        case earlier
    }

    private var pendingHistoryRequest: HistoryRequest?
    @ObservationIgnored private var replyBubbleID: UUID?

    static let locationWait: Duration = .seconds(2)

    public init(location: (any PhoneLocation)? = nil, log: DiagnosticLog = appLog) {
        self.location = location
        self.log = log
    }

    private var currentChannel: (any HostChannel)? {
        if isVoiceSessionRunning {
            return voiceChannel
        }
        return ownLinkState == .online ? ownChannel : nil
    }

    public func open(hello: UpstreamMessage.Hello, connect: @escaping @Sendable () -> any HostChannel) {
        self.hello = hello
        self.connect = connect
        isOpen = true
        refresh()
    }

    public func close() {
        isOpen = false
        closeOwnLink()
    }

    public func refresh() {
        guard isOpen else {
            return
        }
        if isVoiceSessionRunning || ownLinkState == .online {
            requestHistory(.latest, before: nil)
        } else if ownLinkState == .offline {
            startOwnLink()
        }
    }

    public func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        let message = ChatMessage(speaker: .owner, text: trimmed, timestamp: .now, via: .typed)
        messages.append(message)
        deliver(message.id)
    }

    public func retry(_ messageID: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }), messages[index].isFailed else {
            return
        }
        messages[index].isFailed = false
        deliver(messageID)
    }

    private func deliver(_ messageID: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }) else {
            return
        }
        guard let channel = currentChannel else {
            messages[index].isFailed = true
            refresh()
            return
        }
        let text = messages[index].text
        let location = isVoiceSessionRunning ? nil : location
        isAwaitingReply = true
        replyBubbleID = nil
        Task {
            do {
                if let fix = await location?.currentFix(within: Self.locationWait) {
                    try await channel.send(try encodeUpstream(.location(.init(fix))))
                }
                try await channel.send(try encodeUpstream(.text(.init(text: text))))
            } catch {
                markFailed(messageID)
            }
        }
    }

    private func markFailed(_ messageID: UUID) {
        isAwaitingReply = false
        if let index = messages.firstIndex(where: { $0.id == messageID }) {
            messages[index].isFailed = true
        }
        log.warning("文字页打的一句没发出去")
    }

    public func loadEarlier() {
        guard hasEarlierHistory, let oldestID = messages.compactMap(\.historyID).min() else {
            return
        }
        log.info("文字页往回翻，取更早的一页")
        requestHistory(.earlier, before: oldestID)
    }

    private func requestHistory(_ request: HistoryRequest, before: Int?) {
        guard pendingHistoryRequest == nil, let channel = currentChannel else {
            return
        }
        pendingHistoryRequest = request
        Task {
            do {
                try await channel.send(try encodeUpstream(.getHistory(.init(before: before))))
            } catch {
                pendingHistoryRequest = nil
            }
        }
    }

    public func receive(_ message: DownstreamMessage) {
        switch message {
        case .text(let reply):
            appendReply(reply.text, confirmation: reply.confirmation.flatMap(ChatMessage.PermissionPrompt.init))
        case .textEnd:
            replyBubbleID = nil
        case .history(let page):
            applyHistory(page.items)
        case .confirmCard(let card):
            if let index = cardQueue.firstIndex(where: { $0.ticketID == card.ticketID }) {
                cardQueue[index] = card
            } else {
                cardQueue.append(card)
            }
            if card.ticketID == wantedTicketID {
                wantedTicketID = nil
            }
            log.info("收到一张确认卡片")
        default:
            break
        }
    }

    public func answer(_ card: DownstreamMessage.ConfirmCard, approved: Bool, signature: String?) async throws {
        guard let channel = currentChannel else {
            throw ConnectFailure.hostUnreachable
        }
        let confirm = UpstreamMessage.Confirm(
            ticketID: card.ticketID, argsHash: card.argsHash, approved: approved, sig: signature)
        try await channel.send(try encodeUpstream(.confirm(confirm)))
        log.info(approved ? "批准了确认卡片" : "拒绝了确认卡片")
        cardQueue.removeAll { $0.ticketID == card.ticketID }
        for index in messages.indices where messages[index].confirmation?.ticketID == card.ticketID {
            messages[index].confirmation?.state = approved ? .approved : .rejected
        }
    }

    public func closeCard() {
        if !cardQueue.isEmpty {
            cardQueue.removeFirst()
        }
    }

    public func reopenCard(ticketID: String) {
        if let index = cardQueue.firstIndex(where: { $0.ticketID == ticketID }) {
            let card = cardQueue.remove(at: index)
            cardQueue.insert(card, at: 0)
            return
        }
        requestCard(ticketID: ticketID)
    }

    public func requestCard(ticketID: String) {
        wantedTicketID = ticketID
        isWantedCardGone = false
        requestWantedCard()
    }

    public func dismissCardGone() {
        isWantedCardGone = false
    }

    private func requestWantedCard() {
        guard let ticketID = wantedTicketID, let channel = currentChannel else {
            return
        }
        Task {
            do {
                try await channel.send(try encodeUpstream(.getTicket(.init(id: ticketID))))
            } catch {
                log.warning("没要成确认卡片：\(error)")
                return
            }
            try? await Task.sleep(for: cardWait)
            if wantedTicketID == ticketID {
                wantedTicketID = nil
                isWantedCardGone = true
                log.info("要的确认卡片没来，当它失效了")
            }
        }
    }

    private func appendReply(_ text: String, confirmation: ChatMessage.PermissionPrompt? = nil) {
        if confirmation == nil, let replyBubbleID, messages.last?.id == replyBubbleID {
            messages[messages.count - 1].text += text
            return
        }
        let isReplyToTyping = isAwaitingReply || replyBubbleID != nil
        let bubble = ChatMessage(
            speaker: .argus, text: text, timestamp: .now, via: isReplyToTyping ? .typed : .voice,
            confirmation: confirmation)
        messages.append(bubble)
        replyBubbleID = isReplyToTyping && confirmation == nil ? bubble.id : nil
        isAwaitingReply = false
    }

    private func applyHistory(_ items: [DownstreamMessage.HistoryItem]) {
        let request = pendingHistoryRequest ?? .latest
        pendingHistoryRequest = nil
        let page = items.compactMap(ChatMessage.init(historyItem:))
        switch request {
        case .earlier:
            if items.isEmpty {
                hasEarlierHistory = false
            } else {
                messages = page + messages
            }
        case .latest:
            messages = Self.merge(latestPage: page, into: messages)
            hasEarlierHistory = !items.isEmpty
            replyBubbleID = nil
            if messages.last?.speaker == .argus {
                isAwaitingReply = false
            }
        }
    }

    nonisolated static func merge(latestPage page: [ChatMessage], into current: [ChatMessage]) -> [ChatMessage] {
        let oldestInPage = page.compactMap(\.historyID).min() ?? Int.max
        let earlier = current.filter { message in
            guard let historyID = message.historyID else {
                return false
            }
            return historyID < oldestInPage
        }
        let localOnly = current.filter { message in
            message.historyID == nil && !page.contains { recorded in isSameMessage(recorded: recorded, local: message) }
        }
        let inHandByHistoryID = Dictionary(
            current.compactMap { message in message.historyID.map { historyID in (historyID, message) } },
            uniquingKeysWith: { first, _ in first }
        )
        let pageKeepingIDs = page.map { recorded in
            guard let historyID = recorded.historyID, let messageInHand = inHandByHistoryID[historyID] else {
                return recorded
            }
            return recorded.keepingID(of: messageInHand)
        }
        let combined = earlier + pageKeepingIDs + localOnly
        return combined.enumerated()
            .sorted { ($0.element.timestamp, $0.offset) < ($1.element.timestamp, $1.offset) }
            .map(\.element)
    }

    private nonisolated static func isSameMessage(recorded: ChatMessage, local: ChatMessage) -> Bool {
        guard recorded.speaker == local.speaker else {
            return false
        }
        if let prompt = local.confirmation {
            return recorded.confirmation?.ticketID == prompt.ticketID
        }
        if local.speaker == .argus {
            return recorded.text.hasPrefix(local.text)
        }
        return recorded.text == local.text
    }

    public func voiceSessionWillConnect() {
        closeOwnLink()
        isVoiceSessionRunning = true
        replyBubbleID = nil
    }

    public func voiceSessionConnectionChanged(_ channel: (any HostChannel)?) {
        voiceChannel = channel
        pendingHistoryRequest = nil
        if channel != nil {
            refresh()
            requestWantedCard()
        }
    }

    public func voiceSessionDidEnd() {
        isVoiceSessionRunning = false
        voiceChannel = nil
        pendingHistoryRequest = nil
        refresh()
    }

    private func startOwnLink() {
        guard let hello, let connect else {
            return
        }
        ownLinkGeneration += 1
        let generation = ownLinkGeneration
        ownLinkState = .connecting
        let channel = connect()
        ownChannel = channel
        Task {
            await runOwnLink(channel, hello: hello, generation: generation)
        }
    }

    private func runOwnLink(_ channel: any HostChannel, hello: UpstreamMessage.Hello, generation: Int) async {
        defer {
            channel.close()
            if generation == ownLinkGeneration {
                ownChannel = nil
                ownLinkState = .offline
                pendingHistoryRequest = nil
            }
        }
        do {
            _ = try await shakeHands(over: channel, hello: hello, timeout: connectTimeout)
        } catch {
            if generation == ownLinkGeneration {
                log.warning("文字页没连上主机：\(error)")
            }
            return
        }
        guard generation == ownLinkGeneration else {
            return
        }
        ownLinkState = .online
        log.info("文字页连上主机")
        requestHistory(.latest, before: nil)
        requestWantedCard()
        do {
            while true {
                if case .text(let frameText) = try await channel.receive() {
                    receiveFrame(frameText)
                }
            }
        } catch {
            if generation == ownLinkGeneration {
                log.info("文字页的连接断了")
            }
        }
    }

    private func receiveFrame(_ frameText: String) {
        do {
            receive(try decodeDownstream(frameText))
        } catch {
            log.warning("主机发来的一条消息读不懂，丢掉：\(error)")
        }
    }

    private func closeOwnLink() {
        ownLinkGeneration += 1
        ownChannel?.close()
        ownChannel = nil
        ownLinkState = .offline
        if !isVoiceSessionRunning {
            pendingHistoryRequest = nil
        }
    }
}
