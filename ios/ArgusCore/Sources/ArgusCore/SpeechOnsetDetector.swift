
private let loudEnough: Float = 0.45
private let quietEnough: Float = 0.3
private let loudFramesForOnset = 3
private let quietFramesForEnd = 15

public struct SpeechOnsetDetector: Sendable {
    private let samplesPerFrame: Int
    private var leftover: [Float] = []
    private var isSpeaking = false
    private var streak = 0

    public init(sampleRate: Double) {
        samplesPerFrame = max(Int(sampleRate * packetSeconds), 1)
    }

    public mutating func feed(_ samples: [Float]) -> Bool {
        leftover.append(contentsOf: samples)
        var heardOnset = false
        var frameStart = 0
        while frameStart + samplesPerFrame <= leftover.count {
            let frame = Array(leftover[frameStart..<frameStart + samplesPerFrame])
            if update(loudness: loudness(of: frame)) {
                heardOnset = true
            }
            frameStart += samplesPerFrame
        }
        leftover.removeFirst(frameStart)
        return heardOnset
    }

    private mutating func update(loudness: Float) -> Bool {
        if isSpeaking {
            streak = loudness < quietEnough ? streak + 1 : 0
            if streak >= quietFramesForEnd {
                isSpeaking = false
                streak = 0
            }
            return false
        }
        streak = loudness >= loudEnough ? streak + 1 : 0
        if streak >= loudFramesForOnset {
            isSpeaking = true
            streak = 0
            return true
        }
        return false
    }
}
