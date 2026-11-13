import ArgusCore
import SwiftUI

struct ConfirmCardView: View {
    let card: DownstreamMessage.ConfirmCard
    let waitingBehind: Int
    let chat: ChatSession
    @State private var isWorking = false
    @State private var errorMessage: String?

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let remaining = secondsLeft(at: context.date)
            VStack(alignment: .leading, spacing: 16) {
                Text(waitingBehind > 0 ? "需要你确认 · 后面还有 \(waitingBehind) 张" : "需要你确认")
                    .font(.caption)
                    .foregroundStyle(Palette.warnText)
                Text(card.title)
                    .font(.system(size: 22, weight: .bold))
                    .foregroundStyle(Palette.ink)
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(card.lines.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(size: 15))
                                .foregroundStyle(Palette.ink)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .hudPanel(tint: Palette.warnText, fill: Palette.background)
                }
                Text("内容指纹 \(card.argsHash.prefix(8))…")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Palette.ink3)
                if remaining > 0 {
                    Text("\(remaining / 60):\(String(format: "%02d", remaining % 60)) 后失效")
                        .font(.caption)
                        .foregroundStyle(Palette.ink3)
                    buttons
                    Button("先关掉，回去看看") { chat.closeCard() }
                        .buttonStyle(GhostButtonStyle())
                        .disabled(isWorking)
                    Text("关掉不算拒绝，在文字页点「权限操作确认」那一块能再打开")
                        .font(.caption2)
                        .foregroundStyle(Palette.ink3)
                } else {
                    Text("这张确认单已失效")
                        .font(.callout)
                        .foregroundStyle(Palette.warnText)
                    Button("关闭") { chat.closeCard() }
                        .buttonStyle(GhostButtonStyle())
                }
                if let errorMessage {
                    Text(errorMessage)
                        .font(.caption)
                        .foregroundStyle(Palette.dangerText)
                }
                Text("内容由主机生成，和你看到的一致才会执行")
                    .font(.caption2)
                    .foregroundStyle(Palette.ink3)
            }
            .padding(20)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Palette.surfaceRaised)
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(Palette.warnText)
                    .frame(height: 3)
            }
        }
    }

    private var buttons: some View {
        HStack(spacing: 12) {
            Button("拒绝") {
                Task { await answer(approved: false) }
            }
            .buttonStyle(GhostButtonStyle(minHeight: 52))
            Button(isWorking ? "验证中…" : "用 Face ID 批准") {
                Task { await answer(approved: true) }
            }
            .buttonStyle(PrimaryButtonStyle())
        }
        .disabled(isWorking || !chat.canAnswerCard)
    }

    private func secondsLeft(at now: Date) -> Int {
        guard let expiry = card.expiryDate else { return 0 }
        return max(0, Int(expiry.timeIntervalSince(now).rounded(.down)))
    }

    private func answer(approved: Bool) async {
        isWorking = true
        defer { isWorking = false }
        errorMessage = nil
        var signature: String?
        if approved {
            do {
                let ticketID = card.ticketID
                let argsHash = card.argsHash
                let reason = "批准：\(card.title)"
                let signatureDER = try await Task.detached {
                    try ConfirmKey.sign(ticketID: ticketID, argsHash: argsHash, reason: reason)
                }.value
                signature = signatureDER.base64EncodedString()
            } catch {
                errorMessage = "未通过验证，请重试"
                appLog.warning("批准确认卡片时没签成：\(error)")
                return
            }
        }
        do {
            try await chat.answer(card, approved: approved, signature: signature)
        } catch {
            errorMessage = "确认没发出去，连上后再按一次"
            appLog.warning("确认卡片没发出去：\(error)")
        }
    }
}
