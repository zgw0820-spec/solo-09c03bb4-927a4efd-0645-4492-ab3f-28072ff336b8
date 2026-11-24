"""Unit tests for manifest normalization rules."""
from app.normalize import manifests_equal, normalize_manifests


DOC_A = """
# leading comment must be ignored
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-a
  namespace: ns
data:
  z: "1"
  a: "2"
---
apiVersion: v1
kind: Secret
metadata:
  name: sec-b
  namespace: ns
stringData:
  token: x
"""

# Same content, different document order, different mapping key order, extra comments.
DOC_A_SHUFFLED = """
apiVersion: v1
kind: Secret
metadata:
  namespace: ns
  name: sec-b
stringData:
  token: x  # inline comment
---
# another comment
apiVersion: v1
kind: ConfigMap
metadata:
  namespace: ns
  name: cm-a
data:
  a: "2"
  z: "1"
"""

# List element order differs -> must NOT be equal.
DOC_LIST_ORDER_1 = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-list
data:
  items:
    - one
    - two
"""

DOC_LIST_ORDER_2 = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-list
data:
  items:
    - two
    - one
"""

# Scalar value differs.
DOC_SCALAR_DIFF = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-a
  namespace: ns
data:
  z: "9"
  a: "2"
"""


def test_comments_doc_order_and_key_order_ignored():
    assert manifests_equal(DOC_A, DOC_A_SHUFFLED)


def test_normalized_documents_sorted_by_identity():
    normalized = normalize_manifests(DOC_A)
    identities = [f"{d['kind']}/{d['metadata']['name']}" for d in normalized]
    assert identities == ["ConfigMap/cm-a", "Secret/sec-b"]


def test_list_order_is_significant():
    assert not manifests_equal(DOC_LIST_ORDER_1, DOC_LIST_ORDER_2)


def test_scalar_diff_detected():
    assert not manifests_equal(DOC_A, DOC_SCALAR_DIFF)


def test_empty_documents_dropped():
    with_blank = "---\n" + DOC_A + "\n---\n"
    assert manifests_equal(with_blank, DOC_A)
