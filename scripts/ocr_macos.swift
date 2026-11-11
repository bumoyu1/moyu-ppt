import Foundation
import Vision
import ImageIO

// Optional local adapter: never sends slide contents over the network.
guard CommandLine.arguments.count == 2 else { exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let textRequest = VNRecognizeTextRequest()
textRequest.recognitionLevel = .accurate
textRequest.recognitionLanguages = ["zh-Hans", "en-US"]
textRequest.usesLanguageCorrection = false
let qrRequest = VNDetectBarcodesRequest()
qrRequest.symbologies = [.qr]
do {
    let handler = VNImageRequestHandler(url: url, options: [:])
    try handler.perform([textRequest, qrRequest])
    let lines: [[String: Any]] = (textRequest.results ?? []).compactMap {
        guard let candidate = $0.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": candidate.confidence]
    }
    let qr = (qrRequest.results ?? []).compactMap { $0.payloadStringValue }
    let data = try JSONSerialization.data(withJSONObject: ["lines": lines, "qr_payloads": qr], options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
} catch {
    fputs("Local OCR failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
