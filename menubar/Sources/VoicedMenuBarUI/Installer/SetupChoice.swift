import Foundation
import VoicedControl

/// One service on the setup screen: whether it runs, and with which catalogued model.
public struct ServiceChoice: Sendable, Equatable {
    public var isOn: Bool
    /// The model's id in the catalogue; nil when the catalogue offers none for the service.
    public var model: String?

    public init(isOn: Bool, model: String?) {
        self.isOn = isOn
        self.model = model
    }
}

/// What the first-run setup chose (ADR 0018, "The setup"): which services run, and with
/// which models. `voiced install` writes it to the config before its models step, which then
/// downloads these models and no others; a service that is off downloads nothing and is
/// installed with `enabled = false` (ADR 0033).
public struct SetupChoice: Sendable, Equatable {
    public var stt: ServiceChoice
    public var tts: ServiceChoice

    public init(stt: ServiceChoice, tts: ServiceChoice) {
        self.stt = stt
        self.tts = tts
    }

    public subscript(kind: ServiceKind) -> ServiceChoice {
        get { kind == .stt ? stt : tts }
        set {
            if kind == .stt { stt = newValue } else { tts = newValue }
        }
    }

    /// Both services on, each with the model the catalogue recommends: what an install chose
    /// before there was a setup.
    public static func recommended(in catalogue: ModelList) -> SetupChoice {
        func choice(_ kind: ServiceKind) -> ServiceChoice {
            let models = options(in: catalogue, for: kind).compactMap(\.model)
            return ServiceChoice(isOn: true, model: (models.first(where: \.recommended) ?? models.first)?.id)
        }
        return SetupChoice(stt: choice(.stt), tts: choice(.tts))
    }

    /// The models the setup offers for `kind`, as rows of the Models pane: every catalogued
    /// model but those behind a Hugging Face token, which need a login first and stay in
    /// Settings.
    public static func options(in catalogue: ModelList, for kind: ServiceKind) -> [ModelRow] {
        catalogue.engines(of: kind).flatMap { engine in
            engine.models.filter { !$0.gated }.map { ModelRow(engine: engine, model: $0, state: .notDownloaded) }
        }
    }

    /// Nothing to install: both services are off.
    public var isEmpty: Bool { !stt.isOn && !tts.isOn }

    /// The `voiced install --set` arguments: each service's `enabled`, and for one that is on,
    /// its engine and model.
    public func settings(in catalogue: ModelList) -> [String] {
        ServiceKind.allCases.flatMap { kind -> [String] in
            let choice = self[kind]
            var settings = ["\(kind.rawValue).enabled=\(choice.isOn)"]
            if choice.isOn, let id = choice.model, let (engine, _) = catalogue.model(id) {
                settings += ["\(kind.rawValue).engine=\(engine.name)", "\(kind.rawValue).model=\(id)"]
            }
            return settings
        }
    }

    /// What the install downloads: each chosen model of a service that is on, then its
    /// engine's shared models, each once.
    public func downloads(in catalogue: ModelList) -> [CatalogueModel] {
        var found: [CatalogueModel] = []
        for kind in ServiceKind.allCases where self[kind].isOn {
            guard let id = self[kind].model, let (engine, model) = catalogue.model(id) else { continue }
            for needed in [model] + engine.shared where !found.contains(where: { $0.id == needed.id }) {
                found.append(needed)
            }
        }
        return found
    }
}
