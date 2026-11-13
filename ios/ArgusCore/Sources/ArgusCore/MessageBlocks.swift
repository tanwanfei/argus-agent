
import Foundation

public enum MessageBlock: Equatable, Sendable {
    case paragraph(String)
    case heading(String)
    case bulletList([String])
    case numberedList([String])
    case code(String)
}

public func messageBlocks(_ text: String) -> [MessageBlock] {
    var blocks: [MessageBlock] = []
    var paragraphLines: [String] = []
    var bulletItems: [String] = []
    var numberedItems: [String] = []
    var codeLines: [String]?

    func finishOpenBlock() {
        if !paragraphLines.isEmpty {
            blocks.append(.paragraph(paragraphLines.joined(separator: "\n")))
            paragraphLines = []
        }
        if !bulletItems.isEmpty {
            blocks.append(.bulletList(bulletItems))
            bulletItems = []
        }
        if !numberedItems.isEmpty {
            blocks.append(.numberedList(numberedItems))
            numberedItems = []
        }
    }

    for line in text.components(separatedBy: "\n") {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if let openCode = codeLines {
            if trimmed.hasPrefix("```") {
                blocks.append(.code(openCode.joined(separator: "\n")))
                codeLines = nil
            } else {
                codeLines = openCode + [line]
            }
            continue
        }
        if trimmed.hasPrefix("```") {
            finishOpenBlock()
            codeLines = []
        } else if trimmed.isEmpty {
            finishOpenBlock()
        } else if let heading = trimmed.wholeMatch(of: #/#{1,6}\s+(.+)/#) {
            finishOpenBlock()
            blocks.append(.heading(String(heading.1)))
        } else if let bullet = trimmed.wholeMatch(of: #/[-*•]\s+(.+)/#) {
            if bulletItems.isEmpty {
                finishOpenBlock()
            }
            bulletItems.append(String(bullet.1))
        } else if let numbered = trimmed.wholeMatch(of: #/[0-9]+(?:[.)]\s+|、\s*)(.+)/#) {
            if numberedItems.isEmpty {
                finishOpenBlock()
            }
            numberedItems.append(String(numbered.1))
        } else {
            if !bulletItems.isEmpty || !numberedItems.isEmpty {
                finishOpenBlock()
            }
            paragraphLines.append(line)
        }
    }
    if let openCode = codeLines {
        blocks.append(.code(openCode.joined(separator: "\n")))
    }
    finishOpenBlock()
    return blocks
}
