"""Template matching: prefixes, regexes, metadata, and tie-break determinism."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Device, MacPolicy, TemplateSpec
from app.templates import (
    TemplateMatcher,
    TemplateRepository,
    parse_template,
    prefix_matches,
)


def spec(name: str, **kwargs) -> TemplateSpec:
    return TemplateSpec(name=name, path=Path(name), raw="esphome:\n  name: x\n", **kwargs)


def dev(node_name: str, **kwargs) -> Device:
    return Device(node_name=node_name, **kwargs)


# -- prefix boundary --------------------------------------------------------


@pytest.mark.parametrize(
    "node_name,expected",
    [
        ("cloudbay-t", True),           # exact
        ("cloudbay-t-livingroom", True),  # the spec's example
        ("cloudbay-t-a1b2c3", True),      # MAC-suffixed by name_add_mac_suffix
        ("CloudBay-T-Kitchen", True),     # case-insensitive
        ("cloudbay-tx-porch", False),     # different product, must not match
        ("cloudbay-television", False),
        ("cloudbay", False),
        ("t-cloudbay", False),
        ("xcloudbay-t-1", False),
    ],
)
def test_prefix_matches_on_hyphen_boundary(node_name: str, expected: bool) -> None:
    assert prefix_matches(node_name, "cloudbay-t") is expected


def test_empty_prefix_never_matches() -> None:
    assert prefix_matches("anything", "") is False
    assert prefix_matches("anything", "   ") is False


# -- rules ------------------------------------------------------------------


def test_filename_stem_is_the_implicit_prefix() -> None:
    """Dropping in cloudbay-t.yaml is enough; no directives needed."""
    matcher = TemplateMatcher([spec("cloudbay-t.yaml")])
    match = matcher.match(dev("cloudbay-t-livingroom"))
    assert match is not None
    assert match.rule == "filename-prefix"
    assert match.template.name == "cloudbay-t.yaml"


def test_explicit_prefix_beats_filename_stem() -> None:
    generic = spec("generic.yaml", match_prefixes=("cloudbay-t",))
    unrelated = spec("cloudbay-t.yaml", match_prefixes=("something-else",))
    matcher = TemplateMatcher([generic, unrelated])

    match = matcher.match(dev("cloudbay-t-livingroom"))
    assert match is not None
    assert match.template.name == "generic.yaml"
    assert match.rule == "prefix"


def test_regex_beats_prefix() -> None:
    by_prefix = spec("a.yaml", match_prefixes=("cb",))
    by_regex = spec("b.yaml", match_regexes=(r"^cb-t-\d+$",))
    matcher = TemplateMatcher([by_prefix, by_regex])

    match = matcher.match(dev("cb-t-42"))
    assert match is not None
    assert match.template.name == "b.yaml"
    assert match.rule == "regex"


def test_model_metadata_match() -> None:
    matcher = TemplateMatcher([spec("board.yaml", match_models=("Switchboard",))])
    match = matcher.match(dev("kitchen-lights", model="Switchboard v2"))
    assert match is not None
    assert match.rule == "model"


def test_model_match_also_searches_manufacturer() -> None:
    matcher = TemplateMatcher([spec("board.yaml", match_models=("cloudbay",))])
    match = matcher.match(dev("random-name", manufacturer="CloudBay Systems"))
    assert match is not None


def test_no_match_returns_none() -> None:
    matcher = TemplateMatcher([spec("cloudbay-t.yaml")])
    assert matcher.match(dev("totally-unrelated")) is None


def test_empty_repository_returns_none() -> None:
    assert TemplateMatcher([]).match(dev("cloudbay-t-livingroom")) is None


# -- precedence and determinism ---------------------------------------------


def test_longer_prefix_wins() -> None:
    short = spec("cloud.yaml", match_prefixes=("cloudbay",))
    long = spec("cloudt.yaml", match_prefixes=("cloudbay-t",))
    matcher = TemplateMatcher([short, long])

    match = matcher.match(dev("cloudbay-t-livingroom"))
    assert match is not None
    assert match.template.name == "cloudt.yaml"


def test_priority_overrides_prefix_length() -> None:
    short = spec("cloud.yaml", match_prefixes=("cloudbay",), priority=50)
    long = spec("cloudt.yaml", match_prefixes=("cloudbay-t",))
    matcher = TemplateMatcher([short, long])

    match = matcher.match(dev("cloudbay-t-livingroom"))
    assert match is not None
    assert match.template.name == "cloud.yaml"


def test_ties_break_on_name_and_ignore_input_order() -> None:
    """Two equally specific templates must always resolve the same way."""
    alpha = spec("alpha.yaml", match_prefixes=("dev",))
    beta = spec("beta.yaml", match_prefixes=("dev",))

    forwards = TemplateMatcher([alpha, beta]).match(dev("dev-1"))
    backwards = TemplateMatcher([beta, alpha]).match(dev("dev-1"))

    assert forwards is not None and backwards is not None
    assert forwards.template.name == backwards.template.name == "alpha.yaml"


def test_matching_is_repeatable() -> None:
    matcher = TemplateMatcher(
        [spec("a.yaml", match_prefixes=("dev",)), spec("b.yaml", match_prefixes=("dev",))]
    )
    target = dev("dev-1")
    names = {matcher.match(target).template.name for _ in range(20)}
    assert len(names) == 1


def test_multiple_prefixes_on_one_template() -> None:
    matcher = TemplateMatcher([spec("s.yaml", match_prefixes=("switchboard", "swb"))])
    assert matcher.match(dev("swb-hallway")) is not None
    assert matcher.match(dev("switchboard-2")) is not None
    assert matcher.match(dev("other")) is None


# -- directive parsing ------------------------------------------------------


def test_parses_header_directives() -> None:
    raw = (
        "# x-match-prefix: cloudbay-t, cb-t\n"
        "# x-match-regex: ^cb-\\d+$\n"
        "# x-match-model: CloudBay T\n"
        "# x-mac-policy: full\n"
        "# x-priority: 12\n"
        "esphome:\n  name: x\n"
    )
    template = parse_template(Path("t.yaml"), raw)

    assert template.match_prefixes == ("cloudbay-t", "cb-t")
    assert template.match_regexes == (r"^cb-\d+$",)
    assert template.match_models == ("CloudBay T",)
    assert template.mac_policy is MacPolicy.FULL
    assert template.priority == 12
    assert template.warnings == ()


def test_directives_below_the_header_are_ignored() -> None:
    """An `# x-...` line inside a config must not change matching silently."""
    raw = "esphome:\n  name: x\n# x-match-prefix: sneaky\n"
    assert parse_template(Path("t.yaml"), raw).match_prefixes == ()


def test_invalid_directives_become_warnings_not_crashes() -> None:
    raw = (
        "# x-match-regex: [unclosed\n"
        "# x-priority: not-a-number\n"
        "# x-mac-policy: nonsense\n"
        "esphome:\n  name: x\n"
    )
    template = parse_template(Path("t.yaml"), raw)

    assert template.match_regexes == ()
    assert template.priority == 0
    assert template.mac_policy is None
    assert len(template.warnings) == 3


def test_bad_regex_in_a_template_does_not_break_matching() -> None:
    matcher = TemplateMatcher(
        [spec("bad.yaml", match_regexes=("[unclosed",)), spec("good.yaml", match_prefixes=("dev",))]
    )
    match = matcher.match(dev("dev-1"))
    assert match is not None
    assert match.template.name == "good.yaml"


# -- repository -------------------------------------------------------------


def test_repository_loads_the_example_parents(templates_repo: TemplateRepository) -> None:
    names = {t.name for t in templates_repo.load_all()}
    assert {"cloudbay-t.yaml", "switchboard.yaml"} <= names


def test_repository_handles_a_missing_directory(tmp_path: Path) -> None:
    assert TemplateRepository(tmp_path / "nope").load_all() == []


def test_repository_returns_only_parents(tmp_path: Path) -> None:
    """A per-device config sharing the directory must not become a template."""
    tmp_path.joinpath("notes.txt").write_text("hello")
    tmp_path.joinpath("child.yaml").write_text("esphome:\n  name: real-device\n")
    tmp_path.joinpath("parent.yaml").write_text(
        "esphome:\n  name: fam-${mac}\n"
    )
    assert [t.name for t in TemplateRepository(tmp_path).load_all()] == ["parent.yaml"]
