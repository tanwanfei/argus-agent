
import Foundation

private let quietDecibels: Float = -50
private let loudDecibels: Float = -10

public func loudness(of samples: [Float]) -> Float {
    guard !samples.isEmpty else {
        return 0
    }
    let sumOfSquares = samples.reduce(Float(0)) { $0 + $1 * $1 }
    let rootMeanSquare = (sumOfSquares / Float(samples.count)).squareRoot()
    guard rootMeanSquare > 0 else {
        return 0
    }
    let decibels = 20 * log10(rootMeanSquare)
    return min(max((decibels - quietDecibels) / (loudDecibels - quietDecibels), 0), 1)
}
