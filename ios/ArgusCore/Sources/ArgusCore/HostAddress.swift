
import Foundation

public enum HostAddress {
    static let webSocketPath = "/app/ws"

    public static func webSocketURL(from input: String) -> URL? {
        var address = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.contains(where: \.isWhitespace) else {
            return nil
        }
        if !address.contains("://") {
            address = "wss://" + address
        }
        guard var components = URLComponents(string: address),
              components.scheme?.lowercased() == "wss",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil
        else {
            return nil
        }
        components.scheme = "wss"
        if components.path.isEmpty || components.path == "/" {
            components.path = webSocketPath
        }
        return components.url
    }
}
