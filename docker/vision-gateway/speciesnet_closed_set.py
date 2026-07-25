#!/usr/bin/env python3
"""Map SpeciesNet's long-tail taxonomy into the contest's five closed classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


TARGET_CLASSES: Tuple[str, ...] = (
    "elephant",
    "tiger",
    "wolf",
    "monkey",
    "peacock",
)

MONKEY_FAMILIES = frozenset(
    {
        "aotidae",
        "atelidae",
        "callitrichidae",
        "cebidae",
        "cercopithecidae",
        "hylobatidae",
        "pitheciidae",
    }
)


@dataclass(frozen=True)
class Taxon:
    uuid: str
    class_name: str
    order: str
    family: str
    genus: str
    species: str
    common_name: str


@dataclass(frozen=True)
class ClosedSetDecision:
    label: str
    class_id: int
    closed_set_confidence: float
    native_group_mass: float
    margin: float
    accepted: bool
    group_masses: Mapping[str, float]

    def as_dict(self) -> Dict[str, object]:
        return {
            "label": self.label,
            "classId": self.class_id,
            "closedSetConfidence": round(self.closed_set_confidence, 6),
            "nativeGroupMass": round(self.native_group_mass, 6),
            "margin": round(self.margin, 6),
            "accepted": self.accepted,
            "groupMasses": {
                key: round(float(value), 6)
                for key, value in self.group_masses.items()
            },
        }


def parse_taxon(value: str) -> Taxon:
    fields = [item.strip().lower() for item in value.split(";")]
    fields.extend([""] * (7 - len(fields)))
    return Taxon(*fields[:7])


def target_for_taxon(taxon: Taxon) -> str:
    """Return the contest class represented by a SpeciesNet taxonomy label."""

    if taxon.order == "proboscidea":
        return "elephant"
    if taxon.genus == "panthera" or taxon.common_name == "tiger":
        return "tiger"
    if taxon.family == "canidae":
        return "wolf"
    if taxon.order == "primates" and taxon.family in MONKEY_FAMILIES:
        return "monkey"
    if (
        taxon.family == "phasianidae"
        or taxon.genus == "pavo"
        or "peacock" in taxon.common_name
        or "peafowl" in taxon.common_name
    ):
        return "peacock"
    return ""


def build_group_indices(labels: Iterable[str]) -> Dict[str, List[int]]:
    groups: Dict[str, List[int]] = {label: [] for label in TARGET_CLASSES}
    for index, value in enumerate(labels):
        target = target_for_taxon(parse_taxon(value))
        if target:
            groups[target].append(index)
    return groups


def aggregate_probabilities(
    probabilities: Sequence[float],
    group_indices: Mapping[str, Sequence[int]],
) -> Dict[str, float]:
    masses: Dict[str, float] = {}
    for label in TARGET_CLASSES:
        masses[label] = sum(
            float(probabilities[index])
            for index in group_indices.get(label, ())
        )
    return masses


def decide_closed_set(
    probabilities: Sequence[float],
    group_indices: Mapping[str, Sequence[int]],
    detector_confidence: float,
    minimum_detector_confidence: float = 0.20,
    minimum_closed_set_confidence: float = 0.80,
    minimum_native_group_mass: float = 0.45,
    minimum_margin: float = 0.30,
) -> ClosedSetDecision:
    masses = aggregate_probabilities(probabilities, group_indices)
    ranked = sorted(masses.items(), key=lambda item: item[1], reverse=True)
    label, top_mass = ranked[0]
    second_mass = ranked[1][1]
    target_mass = sum(masses.values())
    closed_confidence = top_mass / target_mass if target_mass > 0.0 else 0.0
    second_confidence = second_mass / target_mass if target_mass > 0.0 else 0.0
    margin = closed_confidence - second_confidence
    accepted = (
        detector_confidence >= minimum_detector_confidence
        and closed_confidence >= minimum_closed_set_confidence
        and top_mass >= minimum_native_group_mass
        and margin >= minimum_margin
    )
    return ClosedSetDecision(
        label=label,
        class_id=TARGET_CLASSES.index(label),
        closed_set_confidence=closed_confidence,
        native_group_mass=top_mass,
        margin=margin,
        accepted=accepted,
        group_masses=masses,
    )
