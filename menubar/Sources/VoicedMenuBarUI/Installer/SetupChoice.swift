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

/// What the first-run setup chose (ADR 0018, "Amendment (2026-10-23)"): which services run, and with
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
            let models = options(in: catalogue, for: kind).flatMap(\.models)
            return ServiceChoice(isOn: true, model: (models.first(where: \.recommended) ?? models.first)?.id)
        }
        return SetupChoice(stt: choice(.stt), tts: choice(.tts))
    }

    /// What runs now, as the services' settings say.
    public init(running settings: SettingsPayload) {
        stt = ServiceChoice(isOn: settings.isEnabled(.stt), model: settings.stt.model)
        tts = ServiceChoice(isOn: settings.isEnabled(.tts), model: settings.tts.model)
    }

    /// The engines the setup offers for `kind`, with their models but those behind a
    /// Hugging Face token, which need a login first and stay in Settings.
    public static func options(in catalogue: ModelList, for kind: ServiceKind) -> [ModelEngine] {
        catalogue.engines(of: kind).compactMap { engine in
            var offered = engine
            offered.models = engine.models.filter { !$0.gated }
            return offered.models.isEmpty ? nil : offered
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

/// What one `voiced install` run is asked for: the settings it writes at the switch (the
/// setup's on a first install, none for Update, Repair or Roll back) and the models it
/// fetches, which a failed download names for fetching by hand.
struct InstallRequest: Sendable, Equatable {
    var settings: [String]
    var models: [CatalogueModel]

    var arguments: [String] {
        ["install", "--origin", "app", "--json-progress"] + settings.flatMap { ["--set", $0] }
    }

    /// The manual fallback a failed download names (ADR 0018, "Download goes wrong"): each
    /// model at its pinned revision; one that is not pinned has none to name.
    var manualDownload: String {
        let pinned = models.compactMap { model in model.revision.map { "\(model.repo) at revision \($0)" } }
        return "To download the models by hand, fetch \(pinned.joined(separator: " and ")) into the Hugging Face cache, then try again."
    }
}
