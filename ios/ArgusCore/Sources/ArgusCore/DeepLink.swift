
import Foundation

public enum DeepLink: Equatable, Sendable {
    case chat
    case confirm(ticketID: String)

    public init?(url: URL) {
        guard url.scheme?.lowercased() == "argus" else {
            return nil
        }
        switch url.host()?.lowercased() {
        case "chat":
            self = .chat
        case "confirm":
            let ticketID = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?
                .first { $0.name == "id" }?
                .value
            guard let ticketID, ticketID.wholeMatch(of: #/ticket_[0-9a-f]{1,32}/#) != nil else {
                return nil
            }
            self = .confirm(ticketID: ticketID)
        default:
            return nil
        }
    }
}
