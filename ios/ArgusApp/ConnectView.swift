import ArgusCore
import SwiftUI
import UIKit

struct ConnectView: View {
    let settings: HostSettings
    let onClose: (() -> Void)?

    @State private var addressInput: String
    @State private var tokenInput = ""
    @State private var showsToken = false
    @State private var isConnecting = false
    @State private var failureMessage: String?

    init(settings: HostSettings, onClose: (() -> Void)?) {
        self.settings = settings
        self.onClose = onClose
        _addressInput = State(initialValue: settings.address)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let onClose {
                    Button("取消", action: onClose)
                        .foregroundStyle(Palette.accent)
                        .disabled(isConnecting)
                }

                Text("连接到你的主机")
                    .font(.system(size: 26, weight: .bold))
                    .tracking(2)
                    .foregroundStyle(Palette.ink)
                    .padding(.top, 48)
                Text("只连你自己的设备，不经过任何第三方服务器。")
                    .font(.footnote)
                    .foregroundStyle(Palette.ink3)
                    .padding(.top, 10)

                VStack(spacing: 12) {
                    addressField
                    tokenField
                }
                .padding(.top, 32)

                if let shownError {
                    Text(shownError)
                        .font(.caption)
                        .foregroundStyle(Palette.danger)
                        .padding(.top, 10)
                }

                Button(action: connect) {
                    if isConnecting {
                        ProgressView().tint(Palette.onAccent)
                    } else {
                        Text("连接")
                    }
                }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!canConnect)
                .padding(.top, 26)

                SlowConnectHint(isConnecting: isConnecting)
                    .frame(maxWidth: .infinity)
                    .padding(.top, 10)
                Text("家里开了 Tailscale 才能连上")
                    .font(.caption)
                    .foregroundStyle(Palette.ink3)
                    .frame(maxWidth: .infinity)
                    .padding(.top, 6)
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
        }
        .gridBackground()
    }

    private var addressField: some View {
        FieldBox(label: "主机地址", isInvalid: addressLooksWrong) {
            TextField("", text: $addressInput, prompt: Text("argus.xxxx.ts.net").foregroundStyle(Palette.ink3))
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.system(size: 14, design: .monospaced))
                .foregroundStyle(Palette.ink)
        }
    }

    private var tokenField: some View {
        FieldBox(label: "访问 Token") {
            HStack(spacing: 12) {
                Group {
                    if showsToken {
                        TextField("", text: $tokenInput, prompt: tokenPrompt)
                    } else {
                        SecureField("", text: $tokenInput, prompt: tokenPrompt)
                    }
                }
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.system(size: 15, design: .monospaced))
                .foregroundStyle(Palette.ink)

                Button(showsToken ? "隐藏" : "显示") {
                    showsToken.toggle()
                }
                Button("粘贴") {
                    if let pasted = UIPasteboard.general.string {
                        tokenInput = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
                    }
                }
            }
            .buttonStyle(.borderless)
            .font(.footnote)
            .tint(Palette.accent)
        }
    }

    private var tokenPrompt: Text {
        let prompt = settings.hasToken ? "不改就留空" : "主机上 new-app-token 生成的"
        return Text(prompt).foregroundStyle(Palette.ink3)
    }

    private var addressLooksWrong: Bool {
        !addressInput.isEmpty && HostAddress.webSocketURL(from: addressInput) == nil
    }

    private var shownError: String? {
        addressLooksWrong ? "地址不对：填 argus.xxxx.ts.net 这样的主机名" : failureMessage
    }

    private var typedToken: String {
        tokenInput.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canConnect: Bool {
        !isConnecting
            && HostAddress.webSocketURL(from: addressInput) != nil
            && (!typedToken.isEmpty || settings.hasToken)
    }

    private func connect() {
        let tokenToUse = typedToken.isEmpty ? settings.savedToken() : typedToken
        guard let url = HostAddress.webSocketURL(from: addressInput), let token = tokenToUse else {
            return
        }
        failureMessage = nil
        isConnecting = true
        Task {
            let connectFailure = await connectOnce(url: url, token: token)
            isConnecting = false
            if let connectFailure {
                failureMessage = connectFailure
                return
            }
            if settings.save(address: addressInput, token: token, connectedAt: .now) {
                onClose?()
            } else {
                failureMessage = "连上了，但 Token 存不进钥匙串，再点一次试试"
            }
        }
    }
}
