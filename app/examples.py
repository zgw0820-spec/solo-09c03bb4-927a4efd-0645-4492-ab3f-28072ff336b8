"""Built-in Swagger samples and data mirrored under ``examples/``.

The rollout sample is deliberately a *sequential-order trap*.  Applying the
three planned Deployments one at a time in YAML order (api, worker, search)
fits the first two steps and gets stuck on the third: the quota is 500m/640Mi,
and after upgrading worker the next single step would reach 550m/704Mi.  The
minimum-batch plan must group api + search in batch 1 and upgrade worker in
batch 2; both peaks land exactly on 500m/640Mi.  A fourth Deployment,
``report``, carries an init container and must be reported as ignored, so its
current 50m/64Mi usage is supplied through ``other_used`` (the namespace has no
other workloads in this sample).

``examples/rollout-demo.request.json`` contains the same payload.
"""
from __future__ import annotations

from typing import Any

ROLLOUT_SAMPLE_CURRENT_DEPLOYMENTS = """\
# Current (old) Deployments, as they run before the rollout. Namespace + name
# pair each one with the Deployment rendered from the new chart.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rollout-demo-api
  namespace: rollout-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: rollout-demo
      app.kubernetes.io/component: api
  template:
    metadata:
      labels:
        app.kubernetes.io/name: rollout-demo
        app.kubernetes.io/component: api
    spec:
      containers:
        - name: api
          image: registry.local/rollout-demo/api:1.0.0
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rollout-demo-worker
  namespace: rollout-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: rollout-demo
      app.kubernetes.io/component: worker
  template:
    metadata:
      labels:
        app.kubernetes.io/name: rollout-demo
        app.kubernetes.io/component: worker
    spec:
      containers:
        - name: worker
          image: registry.local/rollout-demo/worker:1.0.0
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rollout-demo-report
  namespace: rollout-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: rollout-demo
      app.kubernetes.io/component: report
  template:
    metadata:
      labels:
        app.kubernetes.io/name: rollout-demo
        app.kubernetes.io/component: report
    spec:
      initContainers:
        - name: prepare
          image: registry.local/rollout-demo/report-prepare:1.0.0
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
      containers:
        - name: report
          image: registry.local/rollout-demo/report:1.0.0
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rollout-demo-search
  namespace: rollout-demo
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: rollout-demo
      app.kubernetes.io/component: search
  template:
    metadata:
      labels:
        app.kubernetes.io/name: rollout-demo
        app.kubernetes.io/component: search
    spec:
      containers:
        - name: search
          image: registry.local/rollout-demo/search:1.0.0
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
"""


def rollout_sample_request() -> dict[str, Any]:
    """The Swagger sample request (also stored as examples/*.request.json)."""
    return {
        "current_deployments_yaml": ROLLOUT_SAMPLE_CURRENT_DEPLOYMENTS,
        "chart_dir": "/workspace/mounted-charts/rollout-demo/v2",
        "values_path": "/workspace/mounted-charts/rollout-demo/v2/values.yaml",
        "release_name": "rollout-demo",
        "namespace": "rollout-demo",
        "quota": {"cpu": "500m", "memory": "640Mi"},
        # `report` is ignored by the planner but still consumes quota.
        "other_used": {"cpu": "50m", "memory": "64Mi"},
    }
