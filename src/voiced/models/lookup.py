"""Questions about the catalogue: an engine by name, a model by id."""

from __future__ import annotations

from voiced.models.catalogue import CATALOGUE, DEFAULT_ENGINES, Engine, Model


def engine(name: str) -> Engine | None:
    return next((e for e in CATALOGUE if e.name == name), None)


def default_model(engine_name: str) -> Model:
    """The first model of a catalogued engine (KeyError if there is none)."""
    found = engine(engine_name)
    if found is None:
        raise KeyError(engine_name)
    return found.models[0]


def defaults() -> list[Model]:
    """What a new install runs and the model lists recommend: each kind's
    default engine's first model."""
    return [default_model(name) for name in DEFAULT_ENGINES.values()]


def find(model_id: str) -> tuple[Engine, Model] | None:
    """The catalogued model ``model_id`` and its engine; None for a shared
    model or one the catalogue does not know."""
    for e in CATALOGUE:
        for m in e.models:
            if m.id == model_id:
                return e, m
    return None


def catalogued() -> list[Model]:
    """Every catalogued model, shared ones included, once each."""
    seen: dict[str, Model] = {}
    for e in CATALOGUE:
        for m in (*e.models, *e.shared):
            seen.setdefault(m.id, m)
    return list(seen.values())


def is_shared(model_id: str) -> bool:
    """A model that comes with its engine's models (Phonon's voices), never alone."""
    return find(model_id) is None and model_id in {m.id for m in catalogued()}


def required(model_id: str) -> list[str]:
    """What a load of ``model_id`` needs: the model, then its engine's shared models."""
    found = find(model_id)
    return [model_id, *(s.id for s in found[0].shared)] if found is not None else [model_id]


def license_of(model_id: str) -> str | None:
    found = find(model_id)
    return found[1].license if found is not None else None
