
public enum UpstreamMessage: Sendable {
    case hello(Hello)
    case location(Location)
    case bargeIn(BargeIn)
    case getTicket(GetTicket)
    case pair(Pair)
    case confirm(Confirm)
    case end(End)
    case log(Log)
    case text(Text)
    case getHistory(GetHistory)

    static let messageTypes = [
        Hello.messageType, Location.messageType, BargeIn.messageType, GetTicket.messageType,
        Pair.messageType, Confirm.messageType, End.messageType, Log.messageType,
        Text.messageType, GetHistory.messageType,
    ]

    var payload: any WireMessage {
        switch self {
        case .hello(let message): return message
        case .location(let message): return message
        case .bargeIn(let message): return message
        case .getTicket(let message): return message
        case .pair(let message): return message
        case .confirm(let message): return message
        case .end(let message): return message
        case .log(let message): return message
        case .text(let message): return message
        case .getHistory(let message): return message
        }
    }
}

public func encodeUpstream(_ message: UpstreamMessage) throws -> String {
    try encodeWithType(message.payload)
}

extension UpstreamMessage {
    public struct Hello: WireMessage {
        public static let messageType = "hello"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var v: Int
        public var token: String
        public var appVersion: String
        public var sigExpiresAt: String

        public init(v: Int, token: String, appVersion: String, sigExpiresAt: String) {
            self.v = v
            self.token = token
            self.appVersion = appVersion
            self.sigExpiresAt = sigExpiresAt
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case v, token
            case appVersion = "app_version"
            case sigExpiresAt = "sig_expires_at"
        }

        public func validate() throws {
            try check(v >= 1, "v", "要 ≥ 1")
            try check(!token.isEmpty, "token", "不能为空")
            try check(!appVersion.isEmpty, "app_version", "不能为空")
            try checkTimestamp(sigExpiresAt, "sig_expires_at")
        }
    }

    public struct Location: WireMessage {
        public static let messageType = "location"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var lat: Double
        public var lng: Double
        public var accuracy: Double
        public var ts: String
        public var moving: Bool

        public init(lat: Double, lng: Double, accuracy: Double, ts: String, moving: Bool) {
            self.lat = lat
            self.lng = lng
            self.accuracy = accuracy
            self.ts = ts
            self.moving = moving
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case lat, lng, accuracy, ts, moving
        }

        public func validate() throws {
            try check((-90...90).contains(lat), "lat", "要在 -90～90 之间")
            try check((-180...180).contains(lng), "lng", "要在 -180～180 之间")
            try check(accuracy >= 0, "accuracy", "不能是负数")
            try checkTimestamp(ts, "ts")
        }
    }

    public struct BargeIn: WireMessage {
        public static let messageType = "barge_in"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var ts: String

        public init(ts: String) {
            self.ts = ts
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case ts
        }

        public func validate() throws {
            try checkTimestamp(ts, "ts")
        }
    }

    public struct GetTicket: WireMessage {
        public static let messageType = "get_ticket"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var id: String

        public init(id: String) {
            self.id = id
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case id
        }

        public func validate() throws {
            try checkTicketID(id, "id")
        }
    }

    public struct Pair: WireMessage {
        public static let messageType = "pair"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var pubkey: String

        public init(pubkey: String) {
            self.pubkey = pubkey
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case pubkey
        }

        public func validate() throws {
            try checkBase64(pubkey, "pubkey")
        }
    }

    public struct Confirm: WireMessage {
        public static let messageType = "confirm"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var ticketID: String
        public var argsHash: String
        public var approved: Bool
        public var sig: String?

        public init(ticketID: String, argsHash: String, approved: Bool, sig: String?) {
            self.ticketID = ticketID
            self.argsHash = argsHash
            self.approved = approved
            self.sig = sig
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case ticketID = "ticket_id"
            case argsHash = "args_hash"
            case approved, sig
        }

        public func validate() throws {
            try checkTicketID(ticketID, "ticket_id")
            try checkArgsHash(argsHash, "args_hash")
            if approved {
                guard let sig else {
                    throw FieldProblem(field: "sig", problem: "批准时必须带签名")
                }
                try checkBase64(sig, "sig")
            } else {
                try check(sig == nil, "sig", "拒绝时不许带签名")
            }
        }
    }

    public struct End: WireMessage {
        public static let messageType = "end"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var reason: Reason

        public init(reason: Reason) {
            self.reason = reason
        }

        public enum Reason: String, Codable, Sendable {
            case idleTimeout = "idle_timeout"
            case user
            case phoneCall = "phone_call"
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case reason
        }

        public func validate() throws {}
    }

    public struct Log: WireMessage {
        public static let messageType = "log"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var level: Level
        public var msg: String

        public init(level: Level, msg: String) {
            self.level = level
            self.msg = msg
        }

        public enum Level: String, Codable, Sendable {
            case debug, info, warning, error
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case level, msg
        }

        public func validate() throws {
            try check(!msg.isEmpty, "msg", "不能为空")
        }
    }

    public struct Text: WireMessage {
        public static let messageType = "text"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var text: String

        public init(text: String) {
            self.text = text
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case text
        }

        public func validate() throws {
            try check(!text.isEmpty, "text", "不能为空")
        }
    }

    public struct GetHistory: WireMessage {
        public static let messageType = "get_history"
        public static var fieldNames: [String] { CodingKeys.allCases.map(\.rawValue) }

        public var before: Int?

        public init(before: Int?) {
            self.before = before
        }

        enum CodingKeys: String, CodingKey, CaseIterable {
            case before
        }

        public func validate() throws {
            if let before {
                try check(before >= 1, "before", "要 ≥ 1")
            }
        }
    }
}
