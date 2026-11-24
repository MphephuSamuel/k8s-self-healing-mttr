import os, textwrap

OUT = "manifests/profiles"
os.makedirs(OUT, exist_ok=True)

PROBES_DEFAULT = """\
          readinessProbe:
            httpGet: { path: /healthz/ready, port: 8080 }
            periodSeconds: 10
            timeoutSeconds: 1
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: /healthz/live, port: 8080 }
            periodSeconds: 10
            timeoutSeconds: 1
            failureThreshold: 3"""

PROBES_DECOUPLED = """\
          startupProbe:
            httpGet: { path: /healthz/startup, port: 8080 }
            periodSeconds: 2
            failureThreshold: 30
          readinessProbe:
            httpGet: { path: /healthz/ready, port: 8080 }
            periodSeconds: 1
            timeoutSeconds: 1
            failureThreshold: 2
          livenessProbe:
            httpGet: { path: /healthz/live, port: 8080 }
            periodSeconds: 5
            timeoutSeconds: 1
            failureThreshold: 3"""

RES_GUARANTEED = """\
          resources:
            requests: { cpu: "500m", memory: "512Mi" }
            limits:   { cpu: "500m", memory: "512Mi" }"""

RES_BURSTABLE = """\
          resources:
            requests: { cpu: "100m", memory: "128Mi" }
            limits:   { cpu: "500m", memory: "256Mi" }"""

TOL_DEFAULT = ""
TOL_TUNED = """\
      tolerations:
        - key: "node.kubernetes.io/not-ready"
          operator: "Exists"
          effect: "NoExecute"
          tolerationSeconds: 15
        - key: "node.kubernetes.io/unreachable"
          operator: "Exists"
          effect: "NoExecute"
          tolerationSeconds: 15"""

TEMPLATE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mttr-app
  namespace: mttr-lab
spec:
  replicas: 2
  selector:
    matchLabels:
      app: mttr-app
  template:
    metadata:
      labels:
        app: mttr-app
        profile: {name}
    spec:
      terminationGracePeriodSeconds: 5
{tolerations}
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: mttr-app
                topologyKey: kubernetes.io/hostname
      containers:
        - name: app
          image: mttr-microservice:v1
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
          env:
            - name: POD_NAME
              valueFrom: {{ fieldRef: {{ fieldPath: metadata.name }} }}
            - name: POD_UID
              valueFrom: {{ fieldRef: {{ fieldPath: metadata.uid }} }}
            - name: NODE_NAME
              valueFrom: {{ fieldRef: {{ fieldPath: spec.nodeName }} }}
            - name: WORKING_SET_MB
              value: "{working_set}"
{resources}
{probes}
"""

profiles = [
    ("C0_F1", PROBES_DEFAULT,   RES_GUARANTEED, TOL_DEFAULT, 0),
    ("C1_F1", PROBES_DECOUPLED, RES_GUARANTEED, TOL_DEFAULT, 0),
    ("C0_F2", PROBES_DECOUPLED, RES_BURSTABLE,  TOL_DEFAULT, 0),
    ("C2_F2", PROBES_DECOUPLED, RES_GUARANTEED, TOL_DEFAULT, 0),
    ("C0_F2b", PROBES_DECOUPLED, RES_BURSTABLE, TOL_DEFAULT, 280),
    ("C2_F2b", PROBES_DECOUPLED, RES_GUARANTEED, TOL_DEFAULT, 280),
    ("C0_F3", PROBES_DECOUPLED, RES_GUARANTEED, TOL_DEFAULT, 0),
    ("C3_F3", PROBES_DECOUPLED, RES_GUARANTEED, TOL_TUNED,   0),
]

for name, probes, res, tol, ws in profiles:
    yaml_text = TEMPLATE.format(name=name, probes=probes, resources=res, tolerations=tol, working_set=ws)
    path = os.path.join(OUT, f"{name}.yaml")
    with open(path, "w") as f:
        f.write(yaml_text)
    print("Wrote:", path)
