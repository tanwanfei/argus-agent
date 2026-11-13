import AVFoundation
import SwiftUI
import UIKit

struct PermissionsView: View {
    let location: PhoneLocationManager
    let onBack: () -> Void
    let onDone: () -> Void

    @State private var microphonePermission = AVAudioApplication.shared.recordPermission
    @State private var hasReadNotificationNote = false
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Button(action: onBack) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 20, weight: .semibold))
                        .foregroundStyle(Palette.ink2)
                        .frame(width: 44, height: 44, alignment: .leading)
                }
                .accessibilityLabel("返回")

                Text("还差三件事")
                    .font(.system(size: 24, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .padding(.top, 40)
                Text("都是 App 自己用，不会上传到别处。")
                    .font(.footnote)
                    .foregroundStyle(Palette.ink3)
                    .padding(.top, 8)

                VStack(spacing: 12) {
                    PermissionCard(
                        systemImage: "mic",
                        title: "麦克风",
                        description: "随身语音全靠它。锁屏后也在后台工作。",
                        action: microphoneAction
                    )
                    PermissionCard(
                        systemImage: "location",
                        title: "定位 · 使用 App 期间",
                        description: "会话中它要知道你在家、在公司，还是路上。坐标只给本地功能，发给模型时只到区或路名。",
                        action: locationAction
                    )
                    PermissionCard(
                        systemImage: "bell",
                        title: "通知",
                        description: "App 自己不发推送。提醒由 Bark 发，戴着 AirPods 会直接念出来。",
                        action: hasReadNotificationNote
                            ? .done("知道了")
                            : .ask("知道了") { hasReadNotificationNote = true }
                    )
                }
                .padding(.top, 24)

                Text("定位可以稍后再开，但「附近」「天气」这类会答不准。")
                    .font(.footnote)
                    .foregroundStyle(Palette.ink3)
                    .lineSpacing(4)
                    .padding(.top, 18)

                Button("继续", action: onDone)
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(microphonePermission == .undetermined)
                    .padding(.top, 24)
                Button("稍后再说", action: onDone)
                    .font(.system(size: 14))
                    .foregroundStyle(Palette.ink3)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .padding(.top, 4)
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
        }
        .gridBackground()
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                microphonePermission = AVAudioApplication.shared.recordPermission
            }
        }
    }

    private var microphoneAction: PermissionCard.Action {
        switch microphonePermission {
        case .granted:
            .done("已允许")
        case .denied:
            .goToSettings(openSystemSettings)
        default:
            .ask("允许") {
                Task {
                    _ = await AVAudioApplication.requestRecordPermission()
                    microphonePermission = AVAudioApplication.shared.recordPermission
                }
            }
        }
    }

    private var locationAction: PermissionCard.Action {
        switch location.permission {
        case .allowed: .done("已允许")
        case .denied: .goToSettings(openSystemSettings)
        case .notAsked: .ask("允许", location.requestPermission)
        }
    }

    private func openSystemSettings() {
        if let settingsURL = URL(string: UIApplication.openSettingsURLString) {
            openURL(settingsURL)
        }
    }
}

private struct PermissionCard: View {
    enum Action {
        case ask(String, @MainActor () -> Void)
        case done(String)
        case goToSettings(@MainActor () -> Void)
    }

    let systemImage: String
    let title: String
    let description: String
    let action: Action

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: systemImage)
                .font(.system(size: 20))
                .foregroundStyle(Palette.accent)
                .frame(width: 40, height: 40)
                .background(Palette.accent.opacity(0.10), in: ChamferedRectangle(cut: 8))
            VStack(alignment: .leading, spacing: 5) {
                Text(title)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Text(description)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Palette.ink3)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            actionButton
                .font(.system(size: 13, weight: .semibold))
                .padding(.top, 11)
        }
        .padding(16)
        .hudPanel(padding: 0)
    }

    @ViewBuilder
    private var actionButton: some View {
        switch action {
        case .ask(let title, let perform):
            Button(title, action: perform)
                .foregroundStyle(Palette.accent)
        case .done(let title):
            Text(title)
                .foregroundStyle(Palette.ok)
        case .goToSettings(let perform):
            Button("去设置", action: perform)
                .foregroundStyle(Palette.think)
        }
    }
}
