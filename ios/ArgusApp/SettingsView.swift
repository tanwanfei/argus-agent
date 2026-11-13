import ArgusCore
import AVFoundation
import SwiftUI
import UIKit

struct SettingsView: View {
    let settings: HostSettings
    let voiceSession: VoiceSession
    let location: PhoneLocationManager
    let onEditConnection: () -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @AppStorage(PhonePreferences.idleTimeoutMinutesKey) private var idleTimeoutMinutes =
        PhonePreferences.defaultIdleTimeoutMinutes
    @AppStorage(PhonePreferences.startsOnLaunchKey) private var startsOnLaunch = false
    @AppStorage(PhonePreferences.usesEarpieceKey) private var usesEarpiece = false
    @AppStorage("hasAskedAlwaysLocation") private var hasAskedAlwaysLocation = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    SettingsGroup(number: 1, title: "连接") {
                        Button(action: editConnection) {
                            SettingsRow(title: "主机", value: settings.address, isMonospaced: true, showsChevron: true)
                        }
                        Button(action: editConnection) {
                            SettingsRow(title: "访问 Token", value: "••••••••", isMonospaced: true, showsChevron: true)
                        }
                        SettingsRow(title: "上次连上", value: lastConnectedDescription)
                        NavigationLink {
                            DiagnosticsView(voiceSession: voiceSession)
                        } label: {
                            SettingsRow(title: "连接诊断", value: roundTripDescription, isMonospaced: true, showsChevron: true)
                        }
                    }
                    SettingsGroup(number: 2, title: "权限") {
                        if microphonePermission == .denied {
                            Button(action: openSystemSettings) {
                                SettingsRow(title: "麦克风", value: "没允许 · 去系统设置打开", valueColor: Palette.danger)
                            }
                        } else {
                            SettingsRow(
                                title: "麦克风",
                                value: microphonePermission == .granted ? "已允许" : "第一次按「开始」时问",
                                valueColor: microphonePermission == .granted ? Palette.ok : Palette.ink2
                            )
                        }
                        locationRow
                        SettingsRow(title: "通知", value: "由 Bark 发送")
                    }
                    SettingsGroup(number: 3, title: "会话") {
                        HStack(spacing: 12) {
                            Text("空闲多久自动结束")
                                .font(.system(size: 15))
                                .foregroundStyle(Palette.ink)
                            Spacer(minLength: 12)
                            Picker("空闲多久自动结束", selection: $idleTimeoutMinutes) {
                                ForEach(PhonePreferences.idleTimeoutChoices, id: \.self) { minutes in
                                    Text("\(minutes) 分钟").tag(minutes)
                                }
                            }
                            .labelsHidden()
                            .pickerStyle(.menu)
                        }
                        Toggle(isOn: $startsOnLaunch) {
                            Text("打开 App 就开始说")
                                .font(.system(size: 15))
                                .foregroundStyle(Palette.ink)
                        }
                        .toggleStyle(HUDToggleStyle())
                        HStack(spacing: 12) {
                            Text("声音从哪出")
                                .font(.system(size: 15))
                                .foregroundStyle(Palette.ink)
                            Spacer(minLength: 12)
                            Picker("声音从哪出", selection: $usesEarpiece) {
                                Text("扬声器").tag(false)
                                Text("听筒").tag(true)
                            }
                            .labelsHidden()
                            .pickerStyle(.menu)
                        }
                        SettingsRow(title: "减弱动态效果", value: "跟随系统")
                    }
                    SettingsGroup(number: 4, title: "关于") {
                        NavigationLink {
                            AboutView(settings: settings)
                        } label: {
                            SettingsRow(title: "关于阿格斯", value: appVersion(), isMonospaced: true, showsChevron: true)
                        }
                        SettingsRow(title: "协议版本", value: "v\(protocolVersion)", isMonospaced: true)
                        SettingsRow(title: "日志回传主机", value: "会话中回传", valueColor: Palette.ok)
                    }
                    Text("本地自用 · 不上架")
                        .font(.caption)
                        .foregroundStyle(Palette.ink3)
                        .frame(maxWidth: .infinity)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            .gridBackground()
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .tint(Palette.accent)
    }

    private var microphonePermission: AVAudioApplication.recordPermission {
        AVAudioApplication.shared.recordPermission
    }

    @ViewBuilder
    private var locationRow: some View {
        switch location.permission {
        case .allowed where location.isAlwaysAllowed:
            SettingsRow(title: "定位", value: "始终", valueColor: Palette.ok)
        case .allowed where hasAskedAlwaysLocation:
            Button(action: openSystemSettings) {
                SettingsRow(title: "定位", value: "使用期间 · 去设置改成始终", showsChevron: true)
            }
        case .allowed:
            Button(action: askAlwaysLocation) {
                SettingsRow(title: "定位", value: "使用期间 · 点这里改成始终", showsChevron: true)
            }
        case .denied:
            Button(action: openSystemSettings) {
                SettingsRow(title: "定位", value: "没允许 · 去系统设置打开", valueColor: Palette.danger)
            }
        case .notAsked:
            Button(action: location.requestPermission) {
                SettingsRow(title: "定位", value: "还没允许 · 点这里允许", showsChevron: true)
            }
        }
    }

    private var lastConnectedDescription: String {
        guard let lastConnectedAt = settings.lastConnectedAt else {
            return "还没连上过"
        }
        return describeConnectedAt(lastConnectedAt)
    }

    private var roundTripDescription: String {
        guard let roundTrip = voiceSession.lastRoundTrip else {
            return ""
        }
        return "延迟 \(milliseconds(roundTrip))ms"
    }

    private func editConnection() {
        dismiss()
        onEditConnection()
    }

    private func askAlwaysLocation() {
        hasAskedAlwaysLocation = true
        location.requestAlwaysPermission()
    }

    private func openSystemSettings() {
        if let settingsURL = URL(string: UIApplication.openSettingsURLString) {
            openURL(settingsURL)
        }
    }
}

struct SettingsRow: View {
    let title: String
    let value: String
    var valueColor: Color = Palette.ink2
    var isMonospaced = false
    var showsChevron = false

    var body: some View {
        HStack(spacing: 12) {
            Text(title)
                .font(.system(size: 15))
                .foregroundStyle(Palette.ink)
            Spacer(minLength: 12)
            Text(value)
                .font(isMonospaced ? .system(size: 13, design: .monospaced) : .system(size: 14))
                .foregroundStyle(valueColor)
                .lineLimit(1)
                .truncationMode(.middle)
            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Palette.accent.opacity(0.8))
            }
        }
        .contentShape(Rectangle())
    }
}

struct SettingsGroup<Content: View>: View {
    let number: Int
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HUDSectionHeader(number: number, title: title)
            VStack(spacing: 0) {
                Group(subviews: content) { rows in
                    ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                        row
                            .padding(.horizontal, 14)
                            .frame(maxWidth: .infinity, minHeight: 46, alignment: .leading)
                            .overlay(alignment: .top) {
                                if index > 0 {
                                    Rectangle()
                                        .fill(Palette.accent.opacity(0.1))
                                        .frame(height: 1)
                                }
                            }
                    }
                }
            }
            .buttonStyle(.plain)
            .hudPanel(padding: 0)
        }
    }
}

func describeConnectedAt(_ date: Date) -> String {
    let time = date.formatted(date: .omitted, time: .shortened)
    if Calendar.current.isDateInToday(date) {
        return "今天 \(time)"
    }
    return date.formatted(.dateTime.month().day()) + " " + time
}

func milliseconds(_ duration: Duration) -> Int {
    Int((duration / .milliseconds(1)).rounded())
}
