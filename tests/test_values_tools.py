"""Unit tests for values YAML path handling."""
import pytest

from app.values_tools import (
    leaf_paths,
    parse_path,
    remove_paths,
    render_path,
)


SAMPLE = {
    "nameOverride": "",
    "image": {
        "repository": "repo",
        "tag": "v1",
        "pullSecrets": [],
    },
    "tolerations": [],
    "pod": {"security": {"runAsUser": 0, "runAsNonRoot": False}},
}


def test_leaf_paths_are_non_contained():
    paths = leaf_paths(SAMPLE)
    texts = {render_path(p) for p in paths}
    # Nested mapping keys are leaves; no path is a strict prefix of another.
    assert "image.repository" in texts
    assert "pod.security.runAsUser" in texts
    assert "image" not in texts
    for p in paths:
        for q in paths:
            assert not (p != q and q[: len(p)] == p)


def test_remove_paths_and_prune_empty_parents():
    result = remove_paths(
        SAMPLE,
        [("pod", "security", "runAsUser"), ("pod", "security", "runAsNonRoot")],
    )
    assert "pod" not in result  # emptied parent pruned
    assert result["image"]["repository"] == "repo"
    # Original untouched.
    assert "pod" in SAMPLE


def test_remove_unknown_path_raises():
    with pytest.raises(KeyError):
        remove_paths(SAMPLE, [("image", "missing")])


def test_path_round_trip_with_dotted_key():
    tricky = ("podAnnotations", "checksum/legacy")
    text = render_path(tricky)
    assert parse_path(text) == tricky


def test_parse_path_rejects_empty_segment():
    with pytest.raises(ValueError):
        parse_path("a..b")
