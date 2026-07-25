import importlib.util
from pathlib import Path
import sys


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "docker"
    / "vision-gateway"
    / "speciesnet_closed_set.py"
)
SPEC = importlib.util.spec_from_file_location("speciesnet_closed_set", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_taxonomy_mapping_covers_contest_groups() -> None:
    labels = [
        "id;mammalia;proboscidea;elephantidae;loxodonta;africana;african elephant",
        "id;mammalia;carnivora;felidae;panthera;tigris;tiger",
        "id;mammalia;carnivora;canidae;canis;lupus;grey wolf",
        "id;mammalia;primates;cercopithecidae;macaca;arctoides;stump-tailed macaque",
        "id;aves;galliformes;phasianidae;pavo;cristatus;indian peafowl",
    ]

    groups = MODULE.build_group_indices(labels)

    assert groups == {
        "elephant": [0],
        "tiger": [1],
        "wolf": [2],
        "monkey": [3],
        "peacock": [4],
    }


def test_human_and_lemur_do_not_map_to_monkey() -> None:
    labels = [
        "id;mammalia;primates;hominidae;homo;sapiens;human",
        "id;mammalia;primates;lemuridae;eulemur;fulvus;brown lemur",
    ]

    groups = MODULE.build_group_indices(labels)

    assert groups["monkey"] == []


def test_closed_set_decision_requires_native_mass_and_margin() -> None:
    groups = {
        "elephant": [0],
        "tiger": [1],
        "wolf": [2],
        "monkey": [3],
        "peacock": [4],
    }

    accepted = MODULE.decide_closed_set(
        [0.01, 0.02, 0.90, 0.02, 0.01],
        groups,
        detector_confidence=0.95,
    )
    rejected = MODULE.decide_closed_set(
        [0.02, 0.03, 0.30, 0.01, 0.01],
        groups,
        detector_confidence=0.95,
    )

    assert accepted.label == "wolf"
    assert accepted.accepted is True
    assert rejected.label == "wolf"
    assert rejected.closed_set_confidence > 0.80
    assert rejected.accepted is False
