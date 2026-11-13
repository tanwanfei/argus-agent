
import AVFoundation

public let hostSampleRate = 16_000.0

let packetSeconds = 0.02

let samplesPerPacket = AVAudioFrameCount(hostSampleRate * packetSeconds)

let opusBitRate = 24_000

let maximumOpusPacketBytes = 1275

public enum OpusCodecError: Error, CustomStringConvertible {
    case converterUnavailable
    case conversionFailed(String)
    case emptyPacket

    public var description: String {
        switch self {
        case .converterUnavailable: "系统不给建 Opus 转换器"
        case .conversionFailed(let reason): "Opus 转换出错：\(reason)"
        case .emptyPacket: "空包"
        }
    }
}

private func makeOpusFormat() -> AVAudioFormat? {
    var description = AudioStreamBasicDescription()
    description.mSampleRate = hostSampleRate
    description.mFormatID = kAudioFormatOpus
    description.mChannelsPerFrame = 1
    description.mFramesPerPacket = samplesPerPacket
    return AVAudioFormat(streamDescription: &description)
}

public final class OpusEncoder {
    private let converter: AVAudioConverter
    private let opusFormat: AVAudioFormat
    private let maximumPacketSize: Int
    private let pendingInput: PendingSamples

    public init(inputSampleRate: Double) throws {
        guard let pcmFormat = AVAudioFormat(standardFormatWithSampleRate: inputSampleRate, channels: 1),
              let opusFormat = makeOpusFormat(),
              let converter = AVAudioConverter(from: pcmFormat, to: opusFormat)
        else {
            throw OpusCodecError.converterUnavailable
        }
        if let supportedBitRates = converter.applicableEncodeBitRates?.map(\.intValue),
           supportedBitRates.contains(opusBitRate) {
            converter.bitRate = opusBitRate
        }
        self.converter = converter
        self.opusFormat = opusFormat
        maximumPacketSize = max(converter.maximumOutputPacketSize, maximumOpusPacketBytes)
        pendingInput = PendingSamples(format: pcmFormat)
    }

    public func encode(_ samples: [Float]) throws -> [Data] {
        pendingInput.append(samples)
        var packets: [Data] = []
        while true {
            let packetBuffer = AVAudioCompressedBuffer(
                format: opusFormat, packetCapacity: 1, maximumPacketSize: maximumPacketSize
            )
            var conversionError: NSError?
            let status = converter.convert(to: packetBuffer, error: &conversionError) { [pendingInput] requestedFrames, inputStatus in
                pendingInput.next(maxFrames: requestedFrames, inputStatus: inputStatus)
            }
            if status == .error {
                throw OpusCodecError.conversionFailed(conversionError?.localizedDescription ?? "系统没说原因")
            }
            if packetBuffer.packetCount > 0, packetBuffer.byteLength > 0 {
                packets.append(Data(bytes: packetBuffer.data, count: Int(packetBuffer.byteLength)))
            }
            guard status == .haveData, packetBuffer.packetCount > 0 else {
                return packets
            }
        }
    }
}

private final class PendingSamples: @unchecked Sendable {
    private let format: AVAudioFormat
    private var samples: [Float] = []

    init(format: AVAudioFormat) {
        self.format = format
    }

    func append(_ newSamples: [Float]) {
        samples.append(contentsOf: newSamples)
    }

    func next(
        maxFrames: AVAudioPacketCount,
        inputStatus: UnsafeMutablePointer<AVAudioConverterInputStatus>
    ) -> AVAudioBuffer? {
        let frameCount = min(Int(maxFrames), samples.count)
        guard frameCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameCount)),
              let channel = buffer.floatChannelData?[0]
        else {
            inputStatus.pointee = .noDataNow
            return nil
        }
        for index in 0..<frameCount {
            channel[index] = samples[index]
        }
        samples.removeFirst(frameCount)
        buffer.frameLength = AVAudioFrameCount(frameCount)
        inputStatus.pointee = .haveData
        return buffer
    }
}

public final class OpusDecoder {
    private let converter: AVAudioConverter
    private let opusFormat: AVAudioFormat
    private let pcmFormat: AVAudioFormat

    public init() throws {
        guard let opusFormat = makeOpusFormat(),
              let pcmFormat = AVAudioFormat(standardFormatWithSampleRate: hostSampleRate, channels: 1),
              let converter = AVAudioConverter(from: opusFormat, to: pcmFormat)
        else {
            throw OpusCodecError.converterUnavailable
        }
        self.converter = converter
        self.opusFormat = opusFormat
        self.pcmFormat = pcmFormat
    }

    public func decode(_ packet: Data) throws -> [Float] {
        guard !packet.isEmpty else {
            throw OpusCodecError.emptyPacket
        }
        let source = SinglePacket(packet, format: opusFormat)
        var samples: [Float] = []
        while true {
            guard let output = AVAudioPCMBuffer(pcmFormat: pcmFormat, frameCapacity: samplesPerPacket * 6) else {
                throw OpusCodecError.conversionFailed("分配不了解码用的内存")
            }
            var conversionError: NSError?
            let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
                source.next(inputStatus: inputStatus)
            }
            if status == .error {
                throw OpusCodecError.conversionFailed(conversionError?.localizedDescription ?? "系统没说原因")
            }
            if let channel = output.floatChannelData?[0], output.frameLength > 0 {
                samples.append(contentsOf: UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
            }
            guard status == .haveData, output.frameLength > 0 else {
                return samples
            }
        }
    }
}

private final class SinglePacket: @unchecked Sendable {
    private var packet: Data?
    private let format: AVAudioFormat

    init(_ packet: Data, format: AVAudioFormat) {
        self.packet = packet
        self.format = format
    }

    func next(inputStatus: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        guard let packet else {
            inputStatus.pointee = .noDataNow
            return nil
        }
        self.packet = nil
        let buffer = AVAudioCompressedBuffer(format: format, packetCapacity: 1, maximumPacketSize: packet.count)
        packet.copyBytes(to: buffer.data.assumingMemoryBound(to: UInt8.self), count: packet.count)
        buffer.byteLength = UInt32(packet.count)
        buffer.packetCount = 1
        buffer.packetDescriptions?[0] = AudioStreamPacketDescription(
            mStartOffset: 0, mVariableFramesInPacket: 0, mDataByteSize: UInt32(packet.count)
        )
        inputStatus.pointee = .haveData
        return buffer
    }
}
