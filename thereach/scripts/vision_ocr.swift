import Foundation
import Vision
import ImageIO

// Uses only the system Vision framework; no model or dependency downloads.
do {
    guard CommandLine.arguments.count >= 2 else { throw NSError(domain: "arguments", code: 1) }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "image", code: 2)
    }
    let classify = CommandLine.arguments.count > 2 && CommandLine.arguments[2] == "vision"
    var output: [String: Any]
    if classify {
        let request = VNClassifyImageRequest()
        try VNImageRequestHandler(cgImage: image).perform([request])
        let labels = Array((request.results ?? []).filter { $0.confidence >= 0.05 }.prefix(8))
            .map { ["label": $0.identifier, "confidence": $0.confidence] as [String: Any] }
        let text = labels.map { "\($0["label"]!): \($0["confidence"]!)" }.joined(separator: "\n")
        output = ["ok": true, "text": text, "labels": labels, "engine": "apple-vision-classifier"]
    } else {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = true
        try VNImageRequestHandler(cgImage: image).perform([request])
        let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
        output = ["ok": true, "text": text, "engine": "apple-vision"]
    }
    let data = try JSONSerialization.data(withJSONObject: output)
    print(String(decoding: data, as: UTF8.self))
} catch {
    let failure: [String: Any] = ["ok": false, "error": ["code": "UPSTREAM_BROKEN", "message": "Apple Vision failed: \(error)"]]
    if let data = try? JSONSerialization.data(withJSONObject: failure) {
        print(String(decoding: data, as: UTF8.self))
    }
    exit(1)
}
