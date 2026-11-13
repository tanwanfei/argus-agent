import ArgusCore
import SwiftUI

enum Palette {
    static let background = Color(hex: 0x05080D)
    static let surface = Color(hex: 0x08141E)
    static let surfaceRaised = Color(hex: 0x0C1C29)
    static let line = Color(hex: 0x3BE8FF).opacity(0.22)
    static let grid = Color(hex: 0x3BE8FF).opacity(0.045)
    static let ink = Color(hex: 0xE6F6FF)
    static let ink2 = Color(hex: 0x9CC3D3)
    static let ink3 = Color(hex: 0x6E8C9C)
    static let accent = Color(hex: 0x3BE8FF)
    static let onAccent = Color(hex: 0x04121A)
    static let danger = Color(hex: 0xFF6E6E)
    static let ok = Color(hex: 0x2EF2D0)

    static let idle = Color(hex: 0x5B8CFF)
    static let listen = Color(hex: 0x2EF2D0)
    static let think = Color(hex: 0xFFB547)
    static let speak = Color(hex: 0xC77DFF)
    static let ringOff = Color(hex: 0x2A3A46)

    static let warnText = Color(hex: 0xFFC77A)
    static let dangerText = Color(hex: 0xFF9A9A)
    static let infoText = Color(hex: 0x8FE9FF)

    static let bubbleEdge = Color(hex: 0x3BE8FF).opacity(0.2)
    static let mine = Color(hex: 0x3BE8FF).opacity(0.16)
    static let codeBackground = Color(hex: 0x03060A)
    static let codeText = Color(hex: 0xB9D4E0)
}

extension Color {
    init(hex: UInt32) {
        self.init(
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255
        )
    }
}

struct ChamferedRectangle: Shape {
    var cut: CGFloat = 10

    func path(in rect: CGRect) -> Path {
        let cut = min(self.cut, rect.width / 2, rect.height / 2)
        var path = Path()
        path.move(to: CGPoint(x: rect.minX + cut, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - cut))
        path.addLine(to: CGPoint(x: rect.maxX - cut, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + cut))
        path.closeSubpath()
        return path
    }
}

struct CornerBrackets: Shape {
    var length: CGFloat = 10

    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.minY + length))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.minX + length, y: rect.minY))
        path.move(to: CGPoint(x: rect.maxX - length, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - length))
        return path
    }
}

extension View {
    func hudPanel(tint: Color = Palette.accent, fill: Color = Palette.surface, padding: CGFloat = 14) -> some View {
        self
            .padding(padding)
            .background(fill)
            .overlay(Rectangle().stroke(tint.opacity(0.22), lineWidth: 1))
            .overlay(CornerBrackets().stroke(tint, lineWidth: 2))
    }

    func gridBackground() -> some View {
        background {
            GridBackground()
                .ignoresSafeArea()
        }
    }
}

struct GridBackground: View {
    static let spacing: CGFloat = 26

    var body: some View {
        Canvas { context, size in
            var lines = Path()
            var x: CGFloat = 0
            while x <= size.width {
                lines.move(to: CGPoint(x: x, y: 0))
                lines.addLine(to: CGPoint(x: x, y: size.height))
                x += Self.spacing
            }
            var y: CGFloat = 0
            while y <= size.height {
                lines.move(to: CGPoint(x: 0, y: y))
                lines.addLine(to: CGPoint(x: size.width, y: y))
                y += Self.spacing
            }
            context.stroke(lines, with: .color(Palette.grid), lineWidth: 1)
        }
        .background(Palette.background)
        .accessibilityHidden(true)
    }
}

struct TickLine: View {
    var body: some View {
        Canvas { context, size in
            var ticks = Path()
            ticks.move(to: .zero)
            ticks.addLine(to: CGPoint(x: size.width, y: 0))
            var x: CGFloat = 0
            while x <= size.width {
                ticks.move(to: CGPoint(x: x, y: 0))
                ticks.addLine(to: CGPoint(x: x, y: size.height))
                x += 10
            }
            context.stroke(ticks, with: .color(Palette.accent.opacity(0.4)), lineWidth: 1)
        }
        .frame(height: 6)
        .accessibilityHidden(true)
    }
}

struct ReadoutCell<Value: View>: View {
    let title: String
    @ViewBuilder let value: Value

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 11))
                .foregroundStyle(Palette.ink3)
            value
                .font(.system(size: 16, weight: .medium, design: .monospaced))
                .foregroundStyle(Palette.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(Palette.accent.opacity(0.04))
        .overlay(Rectangle().stroke(Palette.accent.opacity(0.22)))
        .accessibilityElement(children: .combine)
    }
}

struct HUDSectionHeader: View {
    let number: Int
    let title: String

    var body: some View {
        HStack(spacing: 8) {
            Text(String(format: "[%02d]", number))
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(Palette.accent)
            Text(title)
                .font(.system(size: 13))
                .tracking(2)
                .foregroundStyle(Palette.ink2)
            LinearGradient(colors: [Palette.accent.opacity(0.4), .clear], startPoint: .leading, endPoint: .trailing)
                .frame(height: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    var minHeight: CGFloat = 52
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 17, weight: .bold))
            .tracking(4)
            .foregroundStyle(Palette.onAccent)
            .frame(maxWidth: .infinity, minHeight: minHeight)
            .background(Palette.accent, in: ChamferedRectangle())
            .shadow(color: Palette.accent.opacity(isEnabled ? 0.45 : 0), radius: 10)
            .opacity(isEnabled ? (configuration.isPressed ? 0.8 : 1) : 0.4)
    }
}

struct GhostButtonStyle: ButtonStyle {
    var tint: Color = Palette.accent
    var textColor: Color = Palette.ink2
    var minHeight: CGFloat = 44

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .semibold))
            .tracking(2)
            .foregroundStyle(textColor)
            .frame(maxWidth: .infinity, minHeight: minHeight)
            .background(Palette.surface, in: ChamferedRectangle())
            .overlay(ChamferedRectangle().stroke(tint.opacity(0.5), lineWidth: 1))
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}

struct IconButtonStyle: ButtonStyle {
    var size: CGFloat = 56

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 20))
            .foregroundStyle(Palette.accent)
            .frame(width: size, height: size)
            .background(Palette.surface, in: ChamferedRectangle(cut: size > 48 ? 10 : 8))
            .overlay(ChamferedRectangle(cut: size > 48 ? 10 : 8).stroke(Palette.accent.opacity(0.45), lineWidth: 1))
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}

struct HUDToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 12) {
                configuration.label
                Spacer(minLength: 12)
                Capsule()
                    .fill(configuration.isOn ? Palette.accent.opacity(0.25) : Color.white.opacity(0.04))
                    .overlay(Capsule().stroke(configuration.isOn ? Palette.accent : Palette.ink2.opacity(0.4)))
                    .frame(width: 48, height: 24)
                    .overlay(alignment: configuration.isOn ? .trailing : .leading) {
                        Circle()
                            .fill(configuration.isOn ? Palette.accent : Palette.ink3)
                            .frame(width: 16, height: 16)
                            .padding(.horizontal, 4)
                    }
                    .shadow(color: Palette.accent.opacity(configuration.isOn ? 0.45 : 0), radius: 6)
                    .animation(.easeInOut(duration: 0.15), value: configuration.isOn)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(.isToggle)
        .accessibilityValue(configuration.isOn ? "开" : "关")
    }
}

struct FieldBox<Content: View>: View {
    let label: String
    var isInvalid = false
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.caption)
                .foregroundStyle(isInvalid ? Palette.danger : Palette.ink3)
            content
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .overlay(Rectangle().stroke(isInvalid ? Palette.danger.opacity(0.6) : Palette.line))
        .overlay(CornerBrackets(length: 8).stroke(isInvalid ? Palette.danger : Palette.accent, lineWidth: 1.5))
    }
}

struct Banner: View {
    enum Tone {
        case warn
        case danger
        case info
    }

    let tone: Tone
    let message: String
    var systemImage = "exclamationmark.circle"
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: systemImage)
                .font(.system(size: 15))
            Text(message)
                .font(.system(size: 13))
                .frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .font(.system(size: 13, weight: .semibold))
            }
        }
        .foregroundStyle(textColor)
        .tint(textColor)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .hudPanel(tint: edgeColor, fill: edgeColor.opacity(0.10), padding: 0)
    }

    private var textColor: Color {
        switch tone {
        case .warn: Palette.warnText
        case .danger: Palette.dangerText
        case .info: Palette.infoText
        }
    }

    private var edgeColor: Color {
        switch tone {
        case .warn: Palette.think
        case .danger: Palette.danger
        case .info: Palette.accent
        }
    }
}

struct Toast: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.footnote)
            .foregroundStyle(Palette.ink)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Palette.surfaceRaised.opacity(0.96))
            .overlay(Rectangle().stroke(Palette.line))
            .overlay(CornerBrackets(length: 8).stroke(Palette.accent, lineWidth: 1.5))
    }
}

struct SlowConnectHint: View {
    let isConnecting: Bool
    @State private var isSlow = false

    var body: some View {
        Text("连接比平时慢")
            .font(.caption)
            .foregroundStyle(Palette.ink3)
            .opacity(isSlow ? 1 : 0)
            .task(id: isConnecting) {
                isSlow = false
                guard isConnecting else { return }
                try? await Task.sleep(for: slowConnectThreshold)
                if !Task.isCancelled {
                    isSlow = true
                }
            }
    }
}
