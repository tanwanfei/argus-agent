
import Foundation

public enum DownstreamMessage: Sendable {
    case welcome(Welcome)
    case state(State)
    case playBegin(PlayBegin)
    case playEnd(PlayEnd)
    case notYou(NotYou)
    case confirmCard(ConfirmCard)
    case text(Text)
    case textEnd(TextEnd)
    case history(History)
    case bye(Bye)

    static let messageTypes = [
        Welcome.messageType, State.messageType, PlayBegin.messageType, PlayEnd.messageType,
        NotYou.messageType, ConfirmCard.messageType, Text.messageType, TextEnd.messageType, History.messageType,
        Bye.messageType,
    ]

    var payload: any WireMessage {
        switch self {
        case .welcome(let message): return message
        case .state(let message): return message
        case .playBegin(let message): return message
        case .playEnd(let message): return message
        case .notYou(let message): return message
        case .confirmCard(let message): return message
        case .text(let message): return message
        case .textEnd(let message): return message
        case .history(let message): return message
        case .bye(let message): return message
        }
    }
}

public func decodeDownstream(_ frameText: String) throws -> DownstreamMessage {
    switch try messageType(of: frameText) {
    case DownstreamMessage.Welcome.messageType:
        return try .welcome(decodeStrictly(DownstreamMessage.Welcome.self, from: frameText))
    case DownstreamMessage.State.messageType:
        return try .state(decodeStrictly(DownstreamMessage.State.self, from: frameText))
    case DownstreamMessage.PlayBegin.messageType:
        return try .playBegin(decodeStrictly(DownstreamMessage.PlayBegin.self, from: frameText))
    case DownstreamMessage.PlayEnd.messageType:
        return try .playEnd(decodeStrictly(DownstreamMessage.PlayEnd.self, from: frameText))
    case DownstreamMessage.NotYou.messageType:
        return try .notYou(decodeStrictly(DownstreamMessage.NotYou.self, from: frameText))
    case DownstreamMessage.ConfirmCard.messageType:
        return try .confirmCard(decodeStrictly(DownstreamMessage.ConfirmCard.self, from: frameText))
    case DownstreamMessage.Text.messageType:
        return try .text(decodeStrictly(DownstreamMessage.Text.self, from: frameText))
    case DownstreamMessage.TextEnd.messageType:
        return try .textEnd(decodeStrictly(DownstreamMessage.TextEnd.self, from: frameText))
    case DownstreamMessage.History.messageType:
        return try .history(decodeStrictly(DownstreamMessage.History.self, from: frameText))
    case DownstreamMessage.Bye.messageType:
        return try .bye(decodeStrictly(DownstreamMessage.Bye.self, from: frameText))
    default:
        throw ProtocolError(description: "不认识的下行消息")
    }
}

extension DownstreamMessage {
    public struct Welcome: WireMessage {
        public static let messageType = "welcome"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let v: Int

        enum CodingKeys: String, CodingKey, CaseIterable {
            case v
        }

        public func validate() throws {
            try check(v >= 1, "v", "要 ≥ 1")
        }
    }

    public enum ShownState: String, Codable, Sendable {
        case idle, listening, thinking, speaking
    }

    public struct State: WireMessage {
        public static let messageType = "state"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let state: ShownState

        enum CodingKeys: String, CodingKey, CaseIterable {
            case state
        }

        public func validate() throws {}
    }

    public struct PlayBegin: WireMessage {
        public static let messageType = "play_begin"
        public static var fieldNames: [String] { [] }

        public func validate() throws {}
    }

    public struct PlayEnd: WireMessage {
        public static let messageType = "play_end"
        public static var fieldNames: [String] { [] }

        public func validate() throws {}
    }

    public struct NotYou: WireMessage {
        public static let messageType = "not_you"
        public static var fieldNames: [String] { [] }

        public func validate() throws {}
    }

    public struct TextEnd: WireMessage {
        public static let messageType = "text_end"
        public static var fieldNames: [String] { [] }

        public func validate() throws {}
    }

    public struct ConfirmCard: WireMessage {
        public static let messageType = "confirm_card"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let ticketID: String
        public let title: String
        public let lines: [String]
        public let argsHash: String
        public let expiresAt: String

        public var expiryDate: Date? {
            parseWireTimestamp(expiresAt)
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case ticketID = "ticket_id"
            case title, lines
            case argsHash = "args_hash"
            case expiresAt = "expires_at"
        }

        public func validate() throws {
            try checkTicketID(ticketID, "ticket_id")
            try check(!title.isEmpty, "title", "不能为空")
            for (index, line) in lines.enumerated() {
                try check(!line.isEmpty, "lines.\(index)", "不能为空")
            }
            try checkArgsHash(argsHash, "args_hash")
            try checkTimestamp(expiresAt, "expires_at")
        }
    }

    public struct Confirmation: Codable, Sendable, Equatable {
        public let ticketID: String
        public let title: String
        public let expiresAt: String
        public let state: State

        public enum State: String, Codable, Sendable {
            case pending, approved, rejected, expired
        }

        public var expiryDate: Date? {
            parseWireTimestamp(expiresAt)
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case ticketID = "ticket_id"
            case title
            case expiresAt = "expires_at"
            case state
        }

        public init(from decoder: any Decoder) throws {
            try requireOnlyFields(CodingKeys.allCases.map(\.rawValue), in: decoder)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            ticketID = try container.decode(String.self, forKey: .ticketID)
            title = try container.decode(String.self, forKey: .title)
            expiresAt = try container.decode(String.self, forKey: .expiresAt)
            state = try container.decode(State.self, forKey: .state)
        }

        func validate(at field: String) throws {
            try checkTicketID(ticketID, "\(field).ticket_id")
            try check(!title.isEmpty, "\(field).title", "不能为空")
            try checkTimestamp(expiresAt, "\(field).expires_at")
        }
    }

    public struct Text: WireMessage {
        public static let messageType = "text"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let text: String
        public let confirmation: Confirmation?

        enum CodingKeys: String, CodingKey, CaseIterable {
            case text, confirmation
        }

        public func validate() throws {
            try check(!text.isEmpty, "text", "不能为空")
            try confirmation?.validate(at: "confirmation")
        }
    }

    public struct History: WireMessage {
        public static let messageType = "history"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let items: [HistoryItem]

        enum CodingKeys: String, CodingKey, CaseIterable {
            case items
        }

        public func validate() throws {
            for (index, item) in items.enumerated() {
                try check(item.id >= 1, "items.\(index).id", "要 ≥ 1")
                try checkTimestamp(item.ts, "items.\(index).ts")
                try item.confirmation?.validate(at: "items.\(index).confirmation")
            }
        }
    }

    public struct HistoryItem: Codable, Sendable {
        public let id: Int
        public let who: Speaker
        public let text: String
        public let ts: String
        public let via: Via
        public let confirmation: Confirmation?

        public enum Speaker: String, Codable, Sendable {
            case owner, other, argus
        }

        public enum Via: String, Codable, Sendable {
            case voice, typed
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case id, who, text, ts, via, confirmation
        }

        public init(from decoder: any Decoder) throws {
            try requireOnlyFields(CodingKeys.allCases.map(\.rawValue), in: decoder)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            id = try container.decode(Int.self, forKey: .id)
            who = try container.decode(Speaker.self, forKey: .who)
            text = try container.decode(String.self, forKey: .text)
            ts = try container.decode(String.self, forKey: .ts)
            via = try container.decode(Via.self, forKey: .via)
            confirmation = try container.decodeIfPresent(Confirmation.self, forKey: .confirmation)
        }
    }

    public struct Bye: WireMessage {
        public static let messageType = "bye"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public let reason: Reason

        public enum Reason: String, Codable, Sendable {
            case upgrade
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case reason
        }

        public func validate() throws {}
    }
}
