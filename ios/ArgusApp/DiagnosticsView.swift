import ArgusCore
import Network
import SwiftUI
import UIKit

struct DiagnosticsView: View {
    let voiceSession: VoiceSession

    @State private var entries: [DiagnosticLog.Entry] = []
    @State private var networkDescription = "…"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                summaryCard
                HStack {
                    Text("最近日志 · \(entries.count) 条")
                        .font(.caption)
                        .foregroundStyle(Palette.ink3)
                    Spacer()
                    Button("复制全部") {
                        UIPasteboard.general.string = allLogText
                    }
                    ShareLink("分享", item: allLogText)
                        .padding(.leading, 12)
                }
                .font(.caption)
                .tint(Palette.accent)
                .padding(.top, 22)
                .padding(.bottom, 10)
                .padding(.horizontal, 4)
                logCard
                Text("日志会同时回传主机，便于排查；不含录音内容。")
                    .font(.caption)
                    .foregroundStyle(Palette.ink3)
                    .padding(.top, 12)
            }
            .padding(20)
        }
        .defaultScrollAnchor(.bottom)
        .gridBackground()
        .navigationTitle("连接诊断")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            while !Task.isCancelled {
                entries = appLog.recentEntries()
                try? await Task.sleep(for: .seconds(1))
            }
        }
        .task {
            for await description in Self.networkDescriptions() {
                networkDescription = description
            }
        }
    }

    private nonisolated static func networkDescriptions() -> AsyncStream<String> {
        AsyncStream { continuation in
            let monitor = NWPathMonitor()
            monitor.pathUpdateHandler = { path in
                continuation.yield(describe(path))
            }
            continuation.onTermination = { _ in
                monitor.cancel()
            }
            monitor.start(queue: DispatchQueue(label: "local.argus.app.network"))
        }
    }

    private var summaryCard: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("最近一次往返")
                        .font(.caption)
                        .foregroundStyle(Palette.ink3)
                    HStack(alignment: .firstTextBaseline, spacing: 3) {
                        Text(voiceSession.lastRoundTrip.map { "\(milliseconds($0))" } ?? "—")
                            .font(.system(size: 26, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Palette.ink)
                        Text("ms")
                            .font(.footnote)
                            .foregroundStyle(Palette.ink2)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 6) {
                    Text("连接耗时")
                        .font(.caption)
                        .foregroundStyle(Palette.ink3)
                    Text(voiceSession.lastConnectDuration.map(Self.describeSeconds) ?? "—")
                        .font(.system(size: 16, weight: .semibold, design: .monospaced))
                        .foregroundStyle(Palette.ink)
                }
            }
            HStack(spacing: 8) {
                Chip(text: networkDescription)
                Chip(text: "协议 v\(protocolVersion)")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .hudPanel(padding: 0)
    }

    private var logCard: some View {
        LazyVStack(alignment: .leading, spacing: 4) {
            if entries.isEmpty {
                Text("还没有日志")
                    .foregroundStyle(Palette.ink3)
            }
            ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(Self.time(of: entry))
                        .foregroundStyle(Palette.ink3)
                    Text(Self.tag(of: entry.level))
                        .foregroundStyle(Self.color(of: entry.level))
                    Text(entry.message)
                        .foregroundStyle(Palette.ink2)
                        .textSelection(.enabled)
                }
            }
        }
        .font(.system(size: 11.5, design: .monospaced))
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .hudPanel(padding: 0)
    }

    private var allLogText: String {
        entries
            .map { "\(Self.time(of: $0)) \(Self.tag(of: $0.level)) \($0.message)" }
            .joined(separator: "\n")
    }

    private static func time(of entry: DiagnosticLog.Entry) -> String {
        entry.at.formatted(date: .omitted, time: .standard)
    }

    private static func tag(of level: DiagnosticLog.Level) -> String {
        switch level {
        case .debug: "DEBUG"
        case .info: "INFO"
        case .warning: "WARN"
        case .error: "ERROR"
        }
    }

    private static func color(of level: DiagnosticLog.Level) -> Color {
        switch level {
        case .debug: Palette.ink2
        case .info: Palette.ok
        case .warning: Palette.think
        case .error: Palette.danger
        }
    }

    private static func describeSeconds(_ duration: Duration) -> String {
        (duration / .seconds(1)).formatted(.number.precision(.fractionLength(1))) + "s"
    }

    private nonisolated static func describe(_ path: NWPath) -> String {
        guard path.status == .satisfied else {
            return "没有网络"
        }
        if path.usesInterfaceType(.wifi) {
            return "Wi-Fi"
        }
        if path.usesInterfaceType(.cellular) {
            return "蜂窝网络"
        }
        return "其他网络"
    }
}

private struct Chip: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption2)
            .foregroundStyle(Palette.ink2)
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
            .background(Palette.surfaceRaised)
            .overlay(Rectangle().stroke(Palette.line))
    }
}
