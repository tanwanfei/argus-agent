import ArgusCore
import SwiftUI

@main
struct ArgusApp: App {
    @State private var settings: HostSettings
    @State private var location: PhoneLocationManager
    @State private var voiceSession: VoiceSession
    @State private var chat: ChatSession
    @State private var backgroundLocation: BackgroundLocationReporter
    @Environment(\.scenePhase) private var scenePhase
    @State private var hasReportedConfirmKey = false

    init() {
        let location = PhoneLocationManager()
        let voiceSession = VoiceSession(audio: PhoneAudioEngine(), location: location)
        let chat = ChatSession(location: location)
        voiceSession.chat = chat
        _location = State(initialValue: location)
        _voiceSession = State(initialValue: voiceSession)
        _chat = State(initialValue: chat)
        let settings = HostSettings()
        _settings = State(initialValue: settings)
        _backgroundLocation = State(
            initialValue: BackgroundLocationReporter(settings: settings) {
                voiceSession.phase != .notConnected || chat.linkState != .offline
            }
        )
    }

    var body: some Scene {
        WindowGroup {
            RootView(settings: settings, location: location, voiceSession: voiceSession, chat: chat)
                .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase) { _, newPhase in
            let sessionNote = voiceSession.phase == .notConnected ? "没在会话中" : "会话还在"
            switch newPhase {
            case .background:
                appLog.info("App 进了后台（锁屏或切到别的 App），\(sessionNote)")
            case .active:
                appLog.info("App 回到前台，\(sessionNote)")
                reportConfirmKeyIfNeeded()
            default:
                break
            }
        }
    }
}

extension ArgusApp {
    private func reportConfirmKeyIfNeeded() {
        guard !hasReportedConfirmKey, settings.isConfigured else { return }
        hasReportedConfirmKey = true
        Task {
            hasReportedConfirmKey = await reportConfirmKey(settings: settings)
        }
    }
}

struct RootView: View {
    let settings: HostSettings
    let location: PhoneLocationManager
    let voiceSession: VoiceSession
    let chat: ChatSession
    @State private var isEditingConnection = false
    @State private var isShowingChat = false
    @AppStorage("hasSeenPermissionGuide") private var hasSeenPermissionGuide = false

    var body: some View {
        page
            .onOpenURL(perform: open)
            .sheet(isPresented: isShowingCard) {
                if let card = chat.pendingCard {
                    ConfirmCardView(card: card, waitingBehind: chat.cardQueue.count - 1, chat: chat)
                        .id(card.ticketID)
                        .presentationDetents([.fraction(0.65), .large])
                        .presentationBackground(Palette.surfaceRaised)
                        .interactiveDismissDisabled()
                }
            }
            .alert("这张确认单已失效", isPresented: isShowingCardGone) {
                Button("知道了", role: .cancel) {}
            } message: {
                Text("可能已经过了 2 分钟，或者已经批过了。还要做的话再说一遍。")
            }
    }

    private var isShowingCard: Binding<Bool> {
        Binding(
            get: { chat.pendingCard != nil },
            set: { _ in }
        )
    }

    private var isShowingCardGone: Binding<Bool> {
        Binding(
            get: { chat.isWantedCardGone },
            set: { isShowing in
                if !isShowing {
                    chat.dismissCardGone()
                }
            }
        )
    }

    private func open(_ url: URL) {
        guard let link = DeepLink(url: url) else {
            appLog.warning("打开 App 的地址认不出来，不管")
            return
        }
        guard settings.isConfigured, !isEditingConnection, hasSeenPermissionGuide else {
            appLog.info("从通知打开，还没设置好，先不跳")
            return
        }
        switch link {
        case .chat:
            appLog.info("从通知打开文字页")
            isShowingChat = true
        case .confirm(let ticketID):
            appLog.info("从通知打开确认请求")
            isShowingChat = true
            chat.requestCard(ticketID: ticketID)
        }
    }

    @ViewBuilder
    private var page: some View {
        if !settings.isConfigured || isEditingConnection {
            ConnectView(settings: settings, onClose: closeConnectView)
        } else if !hasSeenPermissionGuide {
            PermissionsView(location: location) {
                isEditingConnection = true
            } onDone: {
                hasSeenPermissionGuide = true
            }
        } else if isShowingChat {
            ChatView(
                settings: settings,
                chat: chat,
                voiceSession: voiceSession,
                location: location,
                onBack: { isShowingChat = false },
                onEditConnection: { isEditingConnection = true }
            )
        } else {
            VoiceView(
                settings: settings,
                voiceSession: voiceSession,
                location: location,
                onOpenChat: { isShowingChat = true },
                onEditConnection: { isEditingConnection = true }
            )
        }
    }

    private var closeConnectView: (() -> Void)? {
        guard settings.isConfigured else {
            return nil
        }
        return { isEditingConnection = false }
    }
}
