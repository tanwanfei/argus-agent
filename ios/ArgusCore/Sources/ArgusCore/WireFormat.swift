
import Foundation

public protocol WireMessage: Codable, Sendable {
    static var messageType: String { get }
    static var fieldNames: [String] { get }
    func validate() throws
}

public struct ProtocolError: Error, CustomStringConvertible {
    public let description: String

    init(description: String) {
        self.description = description
    }

    init(messageType: String, field: String, problem: String) {
        description = field.isEmpty ? "\(messageType): \(problem)" : "\(messageType).\(field): \(problem)"
    }
}

struct FieldProblem: Error {
    let field: String
    let problem: String
}

func check(_ condition: Bool, _ field: String, _ problem: String) throws {
    if !condition {
        throw FieldProblem(field: field, problem: problem)
    }
}

func checkTimestamp(_ value: String, _ field: String) throws {
    let pattern = #/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})/#
    try check(value.wholeMatch(of: pattern) != nil, field, "不是带时区的 ISO 8601 时间")
}

func checkTicketID(_ value: String, _ field: String) throws {
    try check(value.wholeMatch(of: #/ticket_[0-9a-f]{1,32}/#) != nil, field, "不是 ticket_ 开头的编号")
}

func checkArgsHash(_ value: String, _ field: String) throws {
    try check(value.wholeMatch(of: #/[0-9a-f]{64}/#) != nil, field, "不是 64 位小写十六进制")
}

func checkBase64(_ value: String, _ field: String) throws {
    try check(value.wholeMatch(of: #/[A-Za-z0-9+/]+={0,2}/#) != nil, field, "不是标准 base64")
}

public func wireTimestamp(_ date: Date) -> String {
    date.formatted(.iso8601)
}

func parseWireTimestamp(_ value: String) -> Date? {
    let withoutFraction = value.replacing(#/\.[0-9]+/#, with: "")
    return ISO8601DateFormatter().date(from: withoutFraction)
}

func encodeWithType(_ message: any WireMessage) throws -> String {
    let messageType = type(of: message).messageType
    do {
        try message.validate()
    } catch {
        throw protocolError(messageType, describing: error)
    }
    let frame = try JSONEncoder().encode(TypedMessage(message: message))
    return String(decoding: frame, as: UTF8.self)
}

private struct TypedMessage: Encodable {
    let message: any WireMessage

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: FieldName.self)
        try container.encode(type(of: message).messageType, forKey: FieldName("t"))
        try message.encode(to: encoder)
    }
}

func messageType(of frameText: String) throws -> String {
    do {
        return try JSONDecoder().decode(MessageEnvelope.self, from: Data(frameText.utf8)).t
    } catch {
        throw ProtocolError(description: "不是带 t 的 JSON 对象")
    }
}

private struct MessageEnvelope: Decodable {
    let t: String
}

func decodeStrictly<Message: WireMessage>(_: Message.Type, from frameText: String) throws -> Message {
    do {
        return try JSONDecoder().decode(StrictlyDecoded<Message>.self, from: Data(frameText.utf8)).message
    } catch {
        throw protocolError(Message.messageType, describing: error)
    }
}

private struct StrictlyDecoded<Message: WireMessage>: Decodable {
    let message: Message

    init(from decoder: any Decoder) throws {
        try requireOnlyFields(Message.fieldNames + ["t"], in: decoder)
        let container = try decoder.container(keyedBy: FieldName.self)
        let messageType = try container.decode(String.self, forKey: FieldName("t"))
        try check(messageType == Message.messageType, "t", "不是这种消息")
        message = try Message(from: decoder)
        try message.validate()
    }
}

func requireOnlyFields(_ allowedFields: [String], in decoder: any Decoder) throws {
    let container = try decoder.container(keyedBy: FieldName.self)
    for key in container.allKeys where !allowedFields.contains(key.stringValue) {
        throw FieldProblem(field: fieldPath(decoder.codingPath + [key]), problem: "多出来的字段")
    }
}

private func protocolError(_ messageType: String, describing error: any Error) -> ProtocolError {
    if let fieldProblem = error as? FieldProblem {
        return ProtocolError(messageType: messageType, field: fieldProblem.field, problem: fieldProblem.problem)
    }
    guard let decodingError = error as? DecodingError else {
        return ProtocolError(messageType: messageType, field: "", problem: "读不出来")
    }
    switch decodingError {
    case .typeMismatch(_, let context):
        let problem = context.codingPath.isEmpty ? "不是 JSON 对象" : "类型不对"
        return ProtocolError(messageType: messageType, field: fieldPath(context.codingPath), problem: problem)
    case .valueNotFound(_, let context):
        return ProtocolError(messageType: messageType, field: fieldPath(context.codingPath), problem: "不能是 null")
    case .keyNotFound(let key, let context):
        return ProtocolError(messageType: messageType, field: fieldPath(context.codingPath + [key]), problem: "缺这个字段")
    case .dataCorrupted(let context):
        let problem = context.codingPath.isEmpty ? "不是合法的 JSON" : "取值不认识"
        return ProtocolError(messageType: messageType, field: fieldPath(context.codingPath), problem: problem)
    @unknown default:
        return ProtocolError(messageType: messageType, field: "", problem: "读不出来")
    }
}

private func fieldPath(_ codingPath: [any CodingKey]) -> String {
    let names = codingPath.map { key in
        if let index = key.intValue {
            return String(index)
        }
        return key.stringValue
    }
    return names.joined(separator: ".")
}

struct FieldName: CodingKey {
    let stringValue: String
    let intValue: Int? = nil

    init(_ name: String) {
        stringValue = name
    }

    init?(stringValue: String) {
        self.stringValue = stringValue
    }

    init?(intValue: Int) {
        return nil
    }
}
