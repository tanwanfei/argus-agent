import ArgusCore
import SwiftUI

struct AboutView: View {
    let settings: HostSettings

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                AppMark()
                    .padding(.bottom, 14)
                Text("阿格斯")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Text("\(appVersion()) · 协议 v\(protocolVersion)")
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Palette.ink3)
                    .padding(.top, 4)

                signatureCard
                    .padding(.top, 26)

                VStack(spacing: 0) {
                    SettingsRow(title: "Bundle ID", value: Bundle.main.bundleIdentifier ?? "", isMonospaced: true)
                    Divider().overlay(Palette.line)
                    SettingsRow(title: "最低系统", value: "iOS 18")
                    Divider().overlay(Palette.line)
                    SettingsRow(title: "协议版本", value: "v\(protocolVersion)", isMonospaced: true)
                    Divider().overlay(Palette.line)
                    SettingsRow(title: "主机", value: settings.address, isMonospaced: true)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .hudPanel(padding: 0)
                .padding(.top, 16)

                Text("自用侧载版本 · 只连你自己的主机\n不会上架，也没有任何账号")
                    .font(.caption)
                    .foregroundStyle(Palette.ink3)
                    .multilineTextAlignment(.center)
                    .padding(.top, 16)
            }
            .padding(20)
        }
        .gridBackground()
        .navigationTitle("关于阿格斯")
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private var signatureCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let expiresAt = signatureExpiryDate() {
                let urgency = SignatureUrgency(expiresAt: expiresAt, now: .now)
                HStack {
                    Text("签名到期")
                        .font(.footnote)
                        .foregroundStyle(Palette.ink2)
                    Spacer()
                    Text(describeRemaining(until: expiresAt, now: .now) + " · " + expiresAt.formatted(.dateTime.month().day()))
                        .font(.system(size: 13, design: .monospaced))
                        .foregroundStyle(Self.color(of: urgency))
                }
                ProgressView(value: Self.remainingFraction(until: expiresAt))
                    .tint(Self.color(of: urgency))
                    .padding(.top, 12)
            } else {
                Text("读不到签名到期时间")
                    .font(.footnote)
                    .foregroundStyle(Palette.ink2)
            }
            Text("免费签名 7 天有效。连上家里的 Wi-Fi 时，AltStore 会在后台自动续签。")
                .font(.caption)
                .foregroundStyle(Palette.ink3)
                .padding(.top, 10)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .hudPanel(padding: 0)
    }

    private static func color(of urgency: SignatureUrgency) -> Color {
        switch urgency {
        case .fine: Palette.ok
        case .soon: Palette.think
        case .urgent: Palette.danger
        }
    }

    private static func remainingFraction(until expiresAt: Date) -> Double {
        min(max(expiresAt.timeIntervalSinceNow / signatureLifetime, 0), 1)
    }
}

private struct AppMark: View {
    var body: some View {
        ChamferedRectangle(cut: 14)
            .stroke(Palette.accent.opacity(0.45), lineWidth: 1.3)
            .frame(width: 56, height: 56)
            .overlay(
                Circle()
                    .stroke(Palette.accent, lineWidth: 1.3)
                    .frame(width: 22, height: 22)
            )
    }
}
