"""Unit tests for the strict request quantity parser."""
import pytest

from app.quantities import (
    QuantityError,
    Requests,
    parse_cpu,
    parse_memory,
    parse_request_map,
)


def test_parse_canonical_units():
    assert parse_cpu("250m", "cpu") == 250
    assert parse_cpu("0m", "cpu") == 0
    assert parse_memory("256Mi", "memory") == 256
    assert parse_memory("0Mi", "memory") == 0


@pytest.mark.parametrize(
    "value",
    ["250", "0.25", "1", "250u", "1e3m", "", "m", "-50m", " 250m", "250M", 250],
)
def test_parse_cpu_rejects_non_canonical_forms(value):
    with pytest.raises(QuantityError):
        parse_cpu(value, "cpu")


@pytest.mark.parametrize(
    "value",
    ["256", "1Gi", "0.5Mi", "256mi", "1 Mi", "", 256, "-1Mi"],
)
def test_parse_memory_rejects_non_canonical_forms(value):
    with pytest.raises(QuantityError):
        parse_memory(value, "memory")


def test_parse_request_map_requires_both_keys():
    with pytest.raises(QuantityError, match="memory"):
        parse_request_map({"cpu": "100m"}, "quota")
    with pytest.raises(QuantityError, match="cpu"):
        parse_request_map({"memory": "64Mi"}, "quota")
    with pytest.raises(QuantityError):
        parse_request_map("100m", "quota")


def test_requests_arithmetic_and_formatting():
    a = Requests(150, 192)
    b = Requests(50, 64)
    assert (a + b) == Requests(200, 256)
    assert (a - b) == Requests(100, 128)
    assert b.scaled(3) == Requests(150, 192)
    assert a.fits_within(Requests(150, 192))
    assert not Requests(151, 192).fits_within(a)
    assert b.cpu == "50m" and b.memory == "64Mi"
