import ArgusCore
import SwiftUI
import UIKit

struct VoiceView: View {
    let settings: HostSettings
    let voiceSession: VoiceSession
    let location: PhoneLocationManager
    let onOpenChat: () -> Void
    let onEditConnection: () -> Void

    @State private var blockingFailure: ConnectFailure?
    @State private var isShowingBlockingFailure = false
    @State private var isShowingSettings = false
    @State private var signatureExpiry: Date?
    @State private var toastMessage: String?
    @AppStorage(PhonePreferences.idleTimeoutMinutesKey) private var idleTimeoutMinutes =
        PhonePreferences.defaultIdleTimeoutMinutes
    @AppStorage(PhonePreferences.startsOnLaunchKey) private var startsOnLaunch = false
    @MainActor private static var hasStartedOnLaunch = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 0) {
            topBar
            if let endingBanner {
                endingBanner
                    .padding(.top, 8)
            }
            if voiceSession.phase == .paused {
                Banner(tone: .warn, message: "通话中，会话已暂停")
                    .padding(.top, 8)
            }
            if voiceSession.phase == .reconnecting {
                Banner(tone: .warn, message: "连接中断，正在重连…")
                    .padding(.top, 8)
                    .transition(.opacity)
            }
            if isSignatureExpiringSoon {
                Banner(tone: .warn, message: "签名快到期了，连家里 Wi-Fi 会自动续签")
                    .padding(.top, 8)
            }
            Spacer()
            ringWithCaption
            Spacer()
            tip
            readouts
                .padding(.bottom, 14)
            bottomButtons
        }
        .padding(.horizontal, 20)
        .padding(.bottom, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .gridBackground()
        .animation(.easeInOut(duration: 0.3), value: voiceSession.phase == .reconnecting)
        .task {
            signatureExpiry = signatureExpiryDate()
            startOnLaunchIfWanted()
        }
        .sheet(isPresented: $isShowingSettings) {
            SettingsView(
                settings: settings, voiceSession: voiceSession, location: location, onEditConnection: onEditConnection
            )
        }
        .onChange(of: voiceSession.connectedAt) { _, newConnectedAt in
            if let newConnectedAt {
                settings.recordConnected(at: newConnectedAt)
            }
        }
        .onChange(of: voiceSession.lastEnding) { _, newEnding in
            if case .connectFailed(let failure) = newEnding, failure == .tokenRejected || failure == .versionMismatch {
                blockingFailure = failure
                isShowingBlockingFailure = true
            }
            if newEnding == .idleTimeout {
                toastMessage = "会话已结束"
            }
        }
        .overlay(alignment: .bottom) {
            if let toastMessage {
                Toast(message: toastMessage)
                    .padding(.bottom, 150)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.3), value: toastMessage)
        .task(id: toastMessage) {
            guard toastMessage != nil else {
                return
            }
            try? await Task.sleep(for: .seconds(2.5))
            if !Task.isCancelled {
                toastMessage = nil
            }
        }
        .alert(
            Text(blockingFailureTitle),
            isPresented: $isShowingBlockingFailure,
            presenting: blockingFailure
        ) { failure in
            if failure == .tokenRejected {
                Button("去设置", action: onEditConnection)
                Button("稍后", role: .cancel) {}
            } else {
                Button("知道了", role: .cancel) {}
            }
        } message: { failure in
            Text(Self.blockingFailureMessage(failure))
        }
    }

    private var isSignatureExpiringSoon: Bool {
        guard let signatureExpiry else {
            return false
        }
        return SignatureUrgency(expiresAt: signatureExpiry, now: .now) == .urgent
    }

    private var blockingFailureTitle: String {
        blockingFailure == .versionMismatch ? "App 需要更新" : "认证失败"
    }

    private static func blockingFailureMessage(_ failure: ConnectFailure) -> String {
        if failure == .tokenRejected {
            return "主机拒绝了这次连接。Token 可能换了，检查主机上的 .env。"
        }
        return "App 和主机的版本对不上，要更新一边。"
    }

    private var topBar: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                HStack(spacing: 8) {
                    Circle()
                        .fill(dotColor)
                        .frame(width: 7, height: 7)
                        .shadow(color: dotColor, radius: 4)
                    Text("阿格斯")
                        .font(.system(size: 15, weight: .bold))
                        .tracking(2)
                        .foregroundStyle(Palette.ink)
                    Text(linkWord)
                        .font(.caption)
                        .foregroundStyle(Palette.ink2)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .overlay(CornerBrackets(length: 8).stroke(Palette.accent, lineWidth: 1.5))
                Spacer()
                Text(hostName)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Palette.ink2)
            }
            TickLine()
        }
        .padding(.top, 4)
        .accessibilityElement(children: .combine)
    }

    private var linkWord: String {
        switch voiceSession.phase {
        case .notConnected: "未连接"
        case .connecting, .reconnecting: "在连"
        case .connected, .paused: "在线"
        }
    }

    private var dotColor: Color {
        switch voiceSession.phase {
        case .notConnected: Palette.ink3
        case .connecting, .reconnecting: Palette.think
        case .connected, .paused: Palette.ok
        }
    }

    private var hostName: String {
        let host = HostAddress.webSocketURL(from: settings.address)?.host() ?? ""
        return host.split(separator: ".").first.map(String.init) ?? ""
    }

    private var endingBanner: Banner? {
        switch voiceSession.lastEnding {
        case nil, .byUser, .idleTimeout:
            return nil
        case .connectFailed(let failure):
            if failure == .tokenRejected || failure == .versionMismatch {
                return nil
            }
            return Banner(tone: .warn, message: failure.message, actionTitle: "重试", action: start)
        case .reconnectFailed:
            return Banner(tone: .danger, message: "重连失败", actionTitle: "重试", action: start)
        case .interrupted:
            return Banner(tone: .warn, message: "通话说完了，会话已结束", actionTitle: "重新开始", action: start)
        case .microphoneDenied:
            return Banner(tone: .danger, message: "没有麦克风权限，语音用不了", actionTitle: "去设置", action: openSystemSettings)
        case .audioUnavailable:
            return Banner(tone: .danger, message: "麦克风打不开", actionTitle: "重试", action: start)
        }
    }

    private var ringWithCaption: some View {
        VStack(spacing: 22) {
            StatusRing(look: ringLook, microphoneLevel: voiceSession.microphoneLevel)
            VStack(spacing: 8) {
                Text(caption.title)
                    .font(.system(size: 30, weight: .bold))
                    .tracking(8)
                    .foregroundStyle(caption.color)
                    .shadow(color: caption.color.opacity(0.5), radius: 9)
                Text(caption.subtitle)
                    .font(.system(size: 14))
                    .foregroundStyle(Palette.ink2)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var ringLook: StatusRing.Look {
        switch voiceSession.phase {
        case .notConnected, .paused, .reconnecting: .off
        case .connecting: .connecting
        case .connected(.idle): .idle
        case .connected(.listening): .listening
        case .connected(.thinking): .thinking
        case .connected(.speaking): .speaking
        }
    }

    private var caption: (title: String, subtitle: String, color: Color) {
        let shown = phaseCaption
        guard voiceSession.isShowingNotYou else {
            return shown
        }
        return (shown.title, "没认出是你的声音，这句没理", shown.color)
    }

    private var phaseCaption: (title: String, subtitle: String, color: Color) {
        switch voiceSession.phase {
        case .notConnected: ("未连接", "点下面开始一段会话", Palette.ink)
        case .connecting: ("连接主机…", "正在建立加密通道", Palette.ink2)
        case .connected(.idle): ("在", "直接说，不用再叫我", Palette.idle)
        case .connected(.listening): ("听着", "说完了我会接", Palette.listen)
        case .connected(.thinking): ("在想", "马上就好", Palette.think)
        case .connected(.speaking): ("说着", "说完一句就能打断", Palette.speak)
        case .paused: ("已暂停", "挂了电话就接着", Palette.ink2)
        case .reconnecting: ("重连中", "连上了接着说", Palette.ink2)
        }
    }

    private var tip: some View {
        Text(voiceSession.phase == .notConnected ? "边走边说也行，锁屏后照常工作" : " ")
            .font(.footnote)
            .foregroundStyle(Palette.ink3)
            .padding(.bottom, 14)
    }

    @ViewBuilder
    private var readouts: some View {
        Group {
            switch voiceSession.phase {
            case .notConnected:
                ReadoutCell(title: "上次连上") {
                    Text(settings.lastConnectedAt.map(describeConnectedAt) ?? "还没连上过")
                }
            case .connecting:
                SlowConnectHint(isConnecting: true)
                    .frame(maxWidth: .infinity)
            case .connected, .paused, .reconnecting:
                HStack(spacing: 8) {
                    ReadoutCell(title: "会话") {
                        if let connectedAt = voiceSession.connectedAt {
                            Text(connectedAt, style: .timer)
                        } else {
                            Text("—")
                        }
                    }
                    ReadoutCell(title: "自动结束") {
                        if let idleDeadline = voiceSession.idleDeadline {
                            Text(idleDeadline, style: .timer)
                        } else {
                            Text("—")
                        }
                    }
                    ReadoutCell(title: "延迟") {
                        Text(voiceSession.lastRoundTrip.map { "\(milliseconds($0))ms" } ?? "—")
                    }
                }
            }
        }
        .frame(height: 52)
    }

    private var bottomButtons: some View {
        HStack(spacing: 8) {
            Button(action: onOpenChat) {
                Image(systemName: "text.bubble")
            }
            .buttonStyle(IconButtonStyle())
            .accessibilityLabel("文字页")
            mainButton
            if voiceSession.phase == .notConnected {
                Button {
                    isShowingSettings = true
                } label: {
                    Image(systemName: "slider.horizontal.3")
                }
                .buttonStyle(IconButtonStyle())
                .accessibilityLabel("设置")
            } else {
                Color.clear.frame(width: 56, height: 56)
            }
        }
    }

    @ViewBuilder
    private var mainButton: some View {
        switch voiceSession.phase {
        case .notConnected:
            Button("开始", action: start)
                .buttonStyle(PrimaryButtonStyle(minHeight: 56))
        case .connecting:
            Button("取消") { voiceSession.end() }
                .buttonStyle(GhostButtonStyle(minHeight: 56))
        case .connected, .paused, .reconnecting:
            Button("结束会话") { voiceSession.end() }
                .buttonStyle(GhostButtonStyle(tint: Palette.danger, textColor: Palette.dangerText, minHeight: 56))
        }
    }

    private func start() {
        guard let url = HostAddress.webSocketURL(from: settings.address), let token = settings.savedToken() else {
            onEditConnection()
            return
        }
        voiceSession.idleTimeout = .seconds(idleTimeoutMinutes * 60)
        voiceSession.start(hello: makeHello(token: token)) {
            openWebSocket(to: url)
        }
    }

    private func startOnLaunchIfWanted() {
        guard startsOnLaunch, !Self.hasStartedOnLaunch, voiceSession.phase == .notConnected else {
            return
        }
        Self.hasStartedOnLaunch = true
        appLog.info("设置里开着打开就开始说，自动开会话")
        start()
    }

    private func openSystemSettings() {
        if let settingsURL = URL(string: UIApplication.openSettingsURLString) {
            openURL(settingsURL)
        }
    }
}
