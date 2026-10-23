import Foundation

/// The default speed's choices in the Text to Speech pane: 0.5× to 2× in steps of 0.1. The
/// server takes more (up to 4×); a value set by hand outside the range is shown as it is, and
/// the first step moves it to the nearest end.
public enum SpeechSpeed {
    public static let range: ClosedRange<Double> = 0.5...2.0
    public static let step = 0.1

    /// `speed` moved one step up (`up`) or down, to the next multiple of the step, kept in
    /// `range`: 1.25 goes to 1.3 or 1.2, and 3 down goes to 2.
    public static func stepped(_ speed: Double, up: Bool) -> Double {
        let position = speed / step
        // A little slack, so 1.2 (which is 11.999... steps in binary) counts as on the grid.
        let next = up ? (position + 1e-6).rounded(.down) + 1 : (position - 1e-6).rounded(.up) - 1
        let clamped = min(max(next * step, range.lowerBound), range.upperBound)
        return (clamped * 100).rounded() / 100
    }

    public static func canStep(_ speed: Double, up: Bool) -> Bool {
        up ? speed < range.upperBound : speed > range.lowerBound
    }
}

/// The line a voice preview speaks: the voice saying its name in its own language, "Hi, I'm
/// Heart." A voice without a name of its own ("Default", or no label) says "Hi, this is my
/// voice." A language without a line here speaks English.
public enum PreviewLine {
    /// Primary language subtag -> (with a name, where `%@` is the name; without one).
    static let lines: [String: (named: String, unnamed: String)] = [
        "en": ("Hi, I'm %@.", "Hi, this is my voice."),
        "fr": ("Bonjour, je m'appelle %@.", "Bonjour, voici ma voix."),
        "de": ("Hallo, ich bin %@.", "Hallo, das ist meine Stimme."),
        "es": ("Hola, soy %@.", "Hola, esta es mi voz."),
        "it": ("Ciao, sono %@.", "Ciao, questa è la mia voce."),
        "pt": ("Olá, eu sou %@.", "Olá, esta é a minha voz."),
        "nl": ("Hallo, ik ben %@.", "Hallo, dit is mijn stem."),
        "ja": ("こんにちは、%@です。", "こんにちは、これが私の声です。"),
        "zh": ("你好，我是%@。", "你好，这是我的声音。"),
        "ko": ("안녕하세요, 저는 %@입니다.", "안녕하세요, 제 목소리입니다."),
        "hi": ("नमस्ते, मैं %@ हूँ।", "नमस्ते, यह मेरी आवाज़ है।"),
        // Arabic, and Levantine Arabic (OmniVoice's `apc`).
        "ar": ("مرحبا، أنا %@.", "مرحبا، هذا صوتي."),
        "apc": ("مرحبا، أنا %@.", "مرحبا، هذا صوتي."),
    ]

    /// The line for `voice` of `engine`, from its label and its language in `engines.list`.
    public static func text(for voice: String, of engine: TTSEngineInfo?) -> String {
        text(name: name(engine?.voiceLabels[voice]), language: engine?.voiceLanguages[voice])
    }

    /// The line in `language` (a BCP-47 tag) naming `name`, or the unnamed line without one.
    public static func text(name: String?, language: String?) -> String {
        let base = language.flatMap { $0.split(whereSeparator: { $0 == "-" || $0 == "_" }).first }
        let line = base.flatMap { lines[$0.lowercased()] } ?? lines["en"]!
        guard let name else { return line.unnamed }
        return line.named.replacingOccurrences(of: "%@", with: name)
    }

    /// "Heart" from "Heart (American English, female)"; nil for no label, an empty one and
    /// "Default", which name no one.
    static func name(_ label: String?) -> String? {
        guard let label else { return nil }
        let name = label.components(separatedBy: " (").first?.trimmingCharacters(in: .whitespaces) ?? ""
        return name.isEmpty || name.caseInsensitiveCompare("Default") == .orderedSame ? nil : name
    }
}
