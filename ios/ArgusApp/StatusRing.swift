import SwiftUI

struct StatusRing: View {
    enum Look: Equatable {
        case off
        case connecting
        case idle
        case listening
        case thinking
        case speaking
    }

    let look: Look
    let microphoneLevel: @MainActor () -> Float

    static let size: CGFloat = 290

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(paused: reduceMotion || look == .off)) { timeline in
            let seconds = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate
            Canvas { context, _ in
                draw(in: &context, seconds: seconds)
            }
        }
        .frame(width: Self.size, height: Self.size)
        .accessibilityHidden(true)
    }

    private var center: CGPoint { CGPoint(x: Self.size / 2, y: Self.size / 2) }

    private var tint: Color {
        switch look {
        case .off: Palette.ringOff
        case .connecting: Palette.accent
        case .idle: Palette.idle
        case .listening: Palette.listen
        case .thinking: Palette.think
        case .speaking: Palette.speak
        }
    }

    private var arcPeriod: Double {
        switch look {
        case .off: 0
        case .connecting: 1.2
        case .idle: 8
        case .listening: 3
        case .thinking: 0.8
        case .speaking: 2
        }
    }

    private func draw(in context: inout GraphicsContext, seconds: Double) {
        let isOff = look == .off
        drawCornerBrackets(in: &context)
        drawTicks(in: &context, turn: seconds / 40)
        if !isOff {
            drawSweep(in: &context, turn: seconds / 3.2)
        }
        strokeCircle(in: &context, radius: 111, color: tint.opacity(0.35), width: 1)
        if !isOff {
            let arcTurn = -seconds / arcPeriod
            strokeArc(in: &context, radius: 97, startTurn: arcTurn, length: 0.5, color: tint.opacity(0.25), width: 8)
            strokeArc(in: &context, radius: 97, startTurn: arcTurn, length: 0.5, color: tint, width: 3)
        }
        strokeCircle(in: &context, radius: 81, color: tint.opacity(0.35), width: 1, dash: [3, 4])
        if !isOff {
            drawCore(in: &context, seconds: seconds)
        }
    }

    private func drawCornerBrackets(in context: inout GraphicsContext) {
        let size = Self.size
        let length: CGFloat = 26
        var brackets = Path()
        let corners: [(x: CGFloat, y: CGFloat, directionX: CGFloat, directionY: CGFloat)] = [
            (0, 0, 1, 1), (size, 0, -1, 1), (0, size, 1, -1), (size, size, -1, -1),
        ]
        for (cornerX, cornerY, directionX, directionY) in corners {
            brackets.move(to: CGPoint(x: cornerX + directionX * length, y: cornerY))
            brackets.addLine(to: CGPoint(x: cornerX, y: cornerY))
            brackets.addLine(to: CGPoint(x: cornerX, y: cornerY + directionY * length))
        }
        context.stroke(brackets, with: .color(tint.opacity(0.7)), lineWidth: 2)
    }

    private func drawTicks(in context: inout GraphicsContext, turn: Double) {
        var ticks = Path()
        for index in 0..<60 {
            let angle = (Double(index) / 60 + turn) * 2 * Double.pi
            let directionX = CGFloat(cos(angle))
            let directionY = CGFloat(sin(angle))
            ticks.move(to: CGPoint(x: center.x + directionX * 123, y: center.y + directionY * 123))
            ticks.addLine(to: CGPoint(x: center.x + directionX * 131, y: center.y + directionY * 131))
        }
        context.stroke(ticks, with: .color(tint.opacity(look == .off ? 0.5 : 0.75)), lineWidth: 1.2)
    }

    private func drawSweep(in context: inout GraphicsContext, turn: Double) {
        var ring = Path()
        ring.addEllipse(in: circleRect(radius: 111))
        ring.addEllipse(in: circleRect(radius: 46))
        let gradient = Gradient(stops: [
            .init(color: tint.opacity(0.4), location: 0),
            .init(color: tint.opacity(0), location: 70.0 / 360),
            .init(color: tint.opacity(0), location: 1),
        ])
        context.fill(
            ring,
            with: .conicGradient(gradient, center: center, angle: .degrees(turn * 360)),
            style: FillStyle(eoFill: true)
        )
    }

    private func drawCore(in context: inout GraphicsContext, seconds: Double) {
        var scale = 1.0
        var brightness = 1.0
        switch look {
        case .off:
            return
        case .connecting:
            scale = 0.8
            brightness = 0.5
        case .idle:
            scale = 0.94 + 0.09 * (0.5 + 0.5 * sin(2 * Double.pi * seconds / 4))
        case .listening:
            let loudness = Double(microphoneLevel())
            scale = 0.9 + 0.08 * abs(sin(seconds * 3.1)) + 0.22 * loudness
        case .thinking:
            brightness = 0.65 + 0.35 * abs(sin(seconds * 5.2) * cos(seconds * 1.7))
        case .speaking:
            scale = 0.95 + 0.11 * abs(sin(seconds * Double.pi / 0.4))
        }
        let radius = 59 * CGFloat(scale)
        let halo = Gradient(colors: [tint.opacity(0.35 * brightness), tint.opacity(0)])
        context.fill(
            Path(ellipseIn: circleRect(radius: radius * 1.7)),
            with: .radialGradient(halo, center: center, startRadius: 0, endRadius: radius * 1.7)
        )
        let core = Gradient(stops: [
            .init(color: Color.white.opacity(0.85 * brightness), location: 0),
            .init(color: tint.opacity(brightness), location: 0.38),
            .init(color: tint.opacity(0), location: 0.72),
        ])
        context.fill(
            Path(ellipseIn: circleRect(radius: radius)),
            with: .radialGradient(core, center: center, startRadius: 0, endRadius: radius)
        )
    }

    private func circleRect(radius: CGFloat) -> CGRect {
        CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
    }

    private func strokeCircle(
        in context: inout GraphicsContext, radius: CGFloat, color: Color, width: CGFloat, dash: [CGFloat] = []
    ) {
        context.stroke(
            Path(ellipseIn: circleRect(radius: radius)),
            with: .color(color),
            style: StrokeStyle(lineWidth: width, dash: dash)
        )
    }

    private func strokeArc(
        in context: inout GraphicsContext, radius: CGFloat, startTurn: Double, length: Double, color: Color, width: CGFloat
    ) {
        let start = Angle(degrees: startTurn * 360 - 90)
        let end = Angle(degrees: (startTurn + length) * 360 - 90)
        var arc = Path()
        arc.addArc(center: center, radius: radius, startAngle: start, endAngle: end, clockwise: false)
        context.stroke(arc, with: .color(color), style: StrokeStyle(lineWidth: width, lineCap: .round))
    }
}
