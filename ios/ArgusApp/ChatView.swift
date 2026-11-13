import ArgusCore
import SwiftUI
import UIKit

struct ChatView: View {
    let settings: HostSettings
    let chat: ChatSession
    let voiceSession: VoiceSession
    let location: PhoneLocationManager
    let onBack: () -> Void
    let onEditConnection: () -> Void

    @State private var draft = ""
    @State private var isShowingSettings = false
    @State private var expandedMessageIDs: Set<UUID> = []
    @State private var messageAtTopBeforeLoading: UUID?
    @State private var hasScrolledToLatest = false
    @State private var isNearTop = false
    @State private var isUserScrolling = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(spacing: 0) {
            topBar
            Rectangle()
                .fill(Palette.line)
                .frame(height: 1)
            if chat.linkState == .offline {
                Banner(tone: .warn, message: "未连接主机，消息发不出去", actionTitle: "重连", action: chat.refresh)
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
            }
            messageList
            bottomHint
                .padding(.horizontal, 12)
            composer
        }
        .gridBackground()
        .onAppear(perform: openChat)
        .onDisappear {
            chat.close()
            hasScrolledToLatest = false
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                chat.refresh()
            }
        }
        .sheet(isPresented: $isShowingSettings) {
            SettingsView(
                settings: settings, voiceSession: voiceSession, location: location, onEditConnection: onEditConnection
            )
        }
    }

    private var topBar: some View {
        HStack(spacing: 0) {
            Button(action: onBack) {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 16, weight: .semibold))
                    Text("语音")
                        .font(.system(size: 15))
                }
                .foregroundStyle(Palette.accent)
                .frame(minWidth: 64, minHeight: 44, alignment: .leading)
            }
            .accessibilityLabel("返回语音页")
            Spacer()
            HStack(spacing: 8) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 7, height: 7)
                    .shadow(color: dotColor, radius: 4)
                Text("阿格斯")
                    .font(.system(size: 16, weight: .bold))
                    .tracking(3)
                    .foregroundStyle(Palette.ink)
            }
            .accessibilityElement(children: .combine)
            Spacer()
            if voiceSession.phase == .notConnected {
                Button {
                    isShowingSettings = true
                } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.system(size: 19))
                        .foregroundStyle(Palette.accent)
                        .frame(minWidth: 64, minHeight: 44, alignment: .trailing)
                }
                .accessibilityLabel("设置")
            } else {
                Color.clear.frame(width: 64, height: 44)
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 48)
    }

    private var dotColor: Color {
        switch chat.linkState {
        case .online: Palette.ok
        case .connecting: Palette.think
        case .offline: Palette.ink3
        }
    }

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 14) {
                    historyHeader
                    ForEach(chat.messages) { message in
                        MessageRow(
                            message: message,
                            isAwaitingReply: isAwaitingReply(after: message),
                            isExpanded: expandedMessageIDs.contains(message.id),
                            onExpand: { _ = expandedMessageIDs.insert(message.id) },
                            onRetry: { chat.retry(message.id) },
                            onOpenCard: { ticketID in chat.reopenCard(ticketID: ticketID) }
                        )
                        .id(message.id)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .overlay {
                if chat.messages.isEmpty && !chat.isLoadingHistory && chat.linkState == .online {
                    EmptyChatView()
                }
            }
            .onAppear {
                scrollToLatest(proxy)
            }
            .onChange(of: chat.isLoadingHistory) { _, isLoading in
                if !isLoading && !hasScrolledToLatest {
                    scrollToLatest(proxy)
                    hasScrolledToLatest = true
                }
            }
            .onChange(of: chat.messages.last) { _, lastMessage in
                if let lastMessage, hasScrolledToLatest {
                    withAnimation {
                        proxy.scrollTo(lastMessage.id, anchor: .bottom)
                    }
                }
            }
            .onChange(of: chat.messages.first?.id) { _, _ in
                if let messageAtTopBeforeLoading {
                    proxy.scrollTo(messageAtTopBeforeLoading, anchor: .top)
                    self.messageAtTopBeforeLoading = nil
                }
            }
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.contentInsets.top < geometry.containerSize.height / 5
            } action: { _, nearTop in
                isNearTop = nearTop
                loadEarlierIfNearTop()
            }
            .onScrollPhaseChange { _, newPhase in
                isUserScrolling = newPhase == .interacting || newPhase == .decelerating
                loadEarlierIfNearTop()
            }
        }
    }

    @ViewBuilder
    private var historyHeader: some View {
        if chat.isLoadingHistory {
            ProgressView()
                .tint(Palette.accent)
        }
        if !chat.messages.isEmpty {
            Text("历史只保留最近 7 天")
                .font(.caption)
                .foregroundStyle(Palette.ink3)
        }
    }

    private func scrollToLatest(_ proxy: ScrollViewProxy) {
        if let lastID = chat.messages.last?.id {
            proxy.scrollTo(lastID, anchor: .bottom)
        }
    }

    private func loadEarlierIfNearTop() {
        guard isNearTop, isUserScrolling, hasScrolledToLatest, chat.hasEarlierHistory, !chat.isLoadingHistory,
              chat.messages.contains(where: { $0.historyID != nil })
        else {
            return
        }
        messageAtTopBeforeLoading = chat.messages.first?.id
        chat.loadEarlier()
    }

    private func isAwaitingReply(after message: ChatMessage) -> Bool {
        chat.isAwaitingReply && message.speaker == .owner && message.id == chat.messages.last?.id
    }

    @ViewBuilder
    private var bottomHint: some View {
        if chat.isAwaitingReply && voiceSession.phase == .notConnected {
            Banner(tone: .info, message: "回复到了会通知你，点通知回到这里", systemImage: "bell")
        } else if voiceSession.phase != .notConnected {
            Banner(tone: .info, message: "语音页在后台听着，这里打字不会开麦克风", systemImage: "mic")
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Button(action: onBack) {
                Image(systemName: "mic")
            }
            .buttonStyle(IconButtonStyle(size: 44))
            .accessibilityLabel("回语音页")
            TextField("", text: $draft, prompt: Text("打一句…").foregroundStyle(Palette.ink3), axis: .vertical)
                .lineLimit(1...5)
                .font(.system(size: 15))
                .foregroundStyle(Palette.ink)
                .tint(Palette.accent)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .frame(minHeight: 44)
                .background(Palette.surface)
                .overlay(Rectangle().stroke(Palette.accent.opacity(0.3)))
            Button(action: send) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(canSend ? Palette.onAccent : Palette.accent)
                    .frame(width: 44, height: 44)
                    .background(canSend ? Palette.accent : Palette.accent.opacity(0.18), in: ChamferedRectangle(cut: 8))
                    .shadow(color: Palette.accent.opacity(canSend ? 0.5 : 0), radius: 8)
            }
            .disabled(!canSend)
            .accessibilityLabel("发送")
        }
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .overlay(alignment: .top) {
            Rectangle()
                .fill(Palette.line)
                .frame(height: 1)
        }
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func openChat() {
        guard let url = HostAddress.webSocketURL(from: settings.address), let token = settings.savedToken() else {
            onEditConnection()
            return
        }
        chat.open(hello: makeHello(token: token)) {
            openWebSocket(to: url)
        }
    }

    private func send() {
        chat.send(draft)
        draft = ""
    }
}

private struct PermissionBlock: View {
    let prompt: ChatMessage.PermissionPrompt
    let timestamp: Date
    let onOpen: (String) -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let state = prompt.shownState(at: context.date)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: Self.icon(state))
                    Text("权限操作确认 · \(Self.label(state))")
                }
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Self.tint(state))
                Text(prompt.title)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .multilineTextAlignment(.leading)
                Text(state == .pending
                    ? "点一下打开卡片 · \(describeMessageTime(timestamp))"
                    : "阿格斯 · \(describeMessageTime(timestamp))")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Palette.ink3)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: 300, alignment: .leading)
            .background(Palette.surfaceRaised)
            .overlay(Rectangle().stroke(Self.tint(state).opacity(0.5)))
            .overlay(alignment: .leading) {
                Rectangle()
                    .fill(Self.tint(state))
                    .frame(width: 3)
            }
            .contentShape(Rectangle())
            .onTapGesture {
                if state == .pending {
                    onOpen(prompt.ticketID)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(state == .pending ? AccessibilityTraits.isButton : [])
            .accessibilityHint(state == .pending ? "打开确认卡片" : "")
        }
    }

    private static func label(_ state: ChatMessage.PermissionPrompt.State) -> String {
        switch state {
        case .pending: "待批"
        case .approved: "已批准"
        case .rejected: "已拒绝"
        case .expired: "已过期"
        }
    }

    private static func icon(_ state: ChatMessage.PermissionPrompt.State) -> String {
        switch state {
        case .pending: "lock.shield"
        case .approved: "checkmark.shield"
        case .rejected: "xmark.shield"
        case .expired: "clock"
        }
    }

    private static func tint(_ state: ChatMessage.PermissionPrompt.State) -> Color {
        switch state {
        case .pending: Palette.warnText
        case .approved: Palette.ok
        case .rejected: Palette.dangerText
        case .expired: Palette.ink3
        }
    }
}

private struct MessageRow: View {
    let message: ChatMessage
    let isAwaitingReply: Bool
    let isExpanded: Bool
    let onExpand: () -> Void
    let onRetry: () -> Void
    let onOpenCard: (String) -> Void

    private static let collapsedLineLimit = 12
    private static let collapsedHeight: CGFloat = 12 * 22

    private var isMine: Bool {
        message.speaker != .argus
    }

    private var isCollapsed: Bool {
        !isExpanded && estimatedLineCount(message.text) > Self.collapsedLineLimit
    }

    var body: some View {
        VStack(alignment: isMine ? .trailing : .leading, spacing: 5) {
            if isMine {
                mine
                metaLine
                    .padding(.horizontal, 4)
            } else if let prompt = message.confirmation {
                PermissionBlock(prompt: prompt, timestamp: message.timestamp, onOpen: onOpenCard)
            } else {
                argus
            }
        }
        .frame(maxWidth: .infinity, alignment: isMine ? .trailing : .leading)
    }

    private var mine: some View {
        content
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .frame(maxWidth: 300, alignment: .leading)
            .background(Palette.mine, in: ChamferedRectangle())
            .opacity(message.isFailed ? 0.5 : 1)
            .copyable(message.text)
    }

    private var argus: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("阿格斯 · \(describeMessageTime(message.timestamp))")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(Palette.accent.opacity(0.8))
            content
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: 300, alignment: .leading)
        .background(Palette.surface)
        .overlay(Rectangle().stroke(Palette.bubbleEdge))
        .overlay(CornerBrackets().stroke(Palette.accent, lineWidth: 2))
        .copyable(message.text)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 8) {
            MessageBody(text: message.text)
                .frame(maxHeight: isCollapsed ? Self.collapsedHeight : nil, alignment: .top)
                .clipped()
            if isCollapsed {
                Button("展开全文", action: onExpand)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Palette.accent)
            }
        }
    }

    @ViewBuilder
    private var metaLine: some View {
        if message.isFailed {
            Button(action: onRetry) {
                Label("发送失败 · 重试", systemImage: "arrow.clockwise")
            }
            .font(.system(size: 11))
            .foregroundStyle(Palette.warnText)
        } else if isAwaitingReply {
            TypingDots()
        } else {
            HStack(spacing: 4) {
                Image(systemName: message.via == .typed ? "keyboard" : "mic")
                Text(describeMessageTime(message.timestamp))
                Text("·")
                Text(message.via == .typed ? "打的" : "说的")
            }
            .font(.system(size: 11, design: .monospaced))
            .foregroundStyle(Palette.ink3)
            .accessibilityElement(children: .combine)
        }
    }
}

private extension View {
    func copyable(_ text: String) -> some View {
        contextMenu {
            Button("复制", systemImage: "doc.on.doc") {
                UIPasteboard.general.string = text
            }
        }
    }
}

private struct MessageBody: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(messageBlocks(text).enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .font(.system(size: 15))
        .foregroundStyle(Palette.ink)
        .lineSpacing(4)
    }

    @ViewBuilder
    private func blockView(_ block: MessageBlock) -> some View {
        switch block {
        case .paragraph(let paragraph):
            Text(inlineFormatted(paragraph))
        case .heading(let heading):
            Text(inlineFormatted(heading))
                .font(.system(size: 15, weight: .semibold))
        case .bulletList(let items):
            listView(items) { _ in "•" }
        case .numberedList(let items):
            listView(items) { index in "\(index + 1)." }
        case .code(let code):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Palette.codeText)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
            }
            .background(Palette.codeBackground)
            .overlay(Rectangle().stroke(Palette.line))
        }
    }

    private func listView(_ items: [String], marker: @escaping (Int) -> String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(marker(index))
                        .monospacedDigit()
                    Text(inlineFormatted(item))
                }
            }
        }
    }

    private func inlineFormatted(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

private struct TypingDots: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isBright = false

    var body: some View {
        Text("· · ·")
            .font(.system(size: 13, weight: .bold))
            .foregroundStyle(Palette.accent)
            .opacity(isBright || reduceMotion ? 1 : 0.35)
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.7).repeatForever(), value: isBright)
            .onAppear {
                isBright = true
            }
            .accessibilityLabel("在等回复")
    }
}

private struct EmptyChatView: View {
    var body: some View {
        VStack(spacing: 0) {
            Image(systemName: "text.bubble")
                .font(.system(size: 24))
                .foregroundStyle(Palette.accent)
                .frame(width: 56, height: 56)
                .overlay(Rectangle().stroke(Palette.accent.opacity(0.25)))
                .overlay(CornerBrackets(length: 8).stroke(Palette.accent, lineWidth: 1.5))
                .padding(.bottom, 16)
            Text("还没有对话")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(Palette.ink)
            Text("说一句，或者打一句。")
                .font(.footnote)
                .foregroundStyle(Palette.ink3)
                .padding(.top, 8)
            Text("家里音箱说的那几轮，也会出现在这里。")
                .font(.footnote)
                .foregroundStyle(Palette.ink3.opacity(0.8))
                .padding(.top, 6)
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 60)
    }
}

private func describeMessageTime(_ date: Date) -> String {
    let time = date.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
    if Calendar.current.isDateInToday(date) {
        return time
    }
    return date.formatted(.dateTime.month().day()) + " " + time
}

private func estimatedLineCount(_ text: String) -> Int {
    text.components(separatedBy: "\n").reduce(0) { total, line in
        total + max(1, Int((Double(line.count) / 18).rounded(.up)))
    }
}
