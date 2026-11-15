import Foundation
import Speech
import AVFoundation
import CoreMedia

// System models only. Never calls AssetInventory's download/install APIs.
struct Segment: Codable, Sendable {
    let start: Double
    let end: Double
    let text: String
}
struct ASRError: Error { let code: String; let message: String }
func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        print(String(decoding: data, as: UTF8.self))
    }
}

@available(macOS 26.0, *)
func run() async throws {
    let args = Array(CommandLine.arguments.dropFirst())
    let locale = Locale(identifier: "zh_CN")
    let supported = await SpeechTranscriber.supportedLocales
    let installed = await SpeechTranscriber.installedLocales
    let ready = SpeechTranscriber.isAvailable && installed.contains { $0.identifier == locale.identifier }
    if args.first == "--check" {
        emit(["ok": true, "engine": "apple-speech-analyzer", "ready": ready,
              "locale": locale.identifier, "installed_locales": installed.map(\.identifier).sorted(),
              "supported": supported.contains { $0.identifier == locale.identifier },
              "model_downloaded": false, "uploads": false])
        return
    }
    guard ready else {
        throw ASRError(code: "NEED_SETUP", message: "Apple 中文语音资源尚未安装或当前设备不支持；不会自动下载模型")
    }
    guard args.count == 1 else {
        throw ASRError(code: "BAD_INPUT", message: "需要一个本地音频文件路径或 --check")
    }
    let file = try AVAudioFile(forReading: URL(fileURLWithPath: args[0]))
    let duration = Double(file.length) / file.processingFormat.sampleRate
    let started = Date()
    let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
        reportingOptions: [], attributeOptions: [.audioTimeRange])
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    let reader = Task { () throws -> [Segment] in
        var segments: [Segment] = []
        for try await result in transcriber.results {
            let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                segments.append(Segment(start: result.range.start.seconds,
                    end: CMTimeRangeGetEnd(result.range).seconds, text: text))
            }
        }
        return segments
    }
    do {
        if let last = try await analyzer.analyzeSequence(from: file) {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            await analyzer.cancelAndFinishNow()
        }
        let segments = try await reader.value
        guard !segments.isEmpty else {
            throw ASRError(code: "EMPTY_CONTENT", message: "Apple 转写没有返回语音文本")
        }
        let encoded = try JSONEncoder().encode(segments)
        emit(["ok": true, "text": segments.map(\.text).joined(separator: "\n"),
              "segments": try JSONSerialization.jsonObject(with: encoded),
              "engine": "apple-speech-analyzer", "locale": locale.identifier,
              "duration_seconds": duration, "elapsed_seconds": Date().timeIntervalSince(started),
              "scope": "asr", "uploads": false, "model_downloaded": false])
    } catch {
        reader.cancel()
        await analyzer.cancelAndFinishNow()
        throw error
    }
}

do {
    if #available(macOS 26.0, *) { try await run() }
    else { throw ASRError(code: "NEED_SETUP", message: "Apple SpeechAnalyzer 需要 macOS 26 或更新版本") }
} catch let error as ASRError {
    emit(["ok": false, "error": ["code": error.code, "message": error.message]])
    exit(2)
} catch {
    // Audio paths and system internals are deliberately omitted from errors.
    let error = error as NSError
    emit(["ok": false, "error": ["code": "UPSTREAM_BROKEN",
        "message": "Apple 语音处理失败（\(error.domain)，\(error.code)）；检查音频格式和系统语音服务"]])
    exit(2)
}
