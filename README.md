# Kubernetes self-healing and recovery-time experiments

This repository contains a small HTTP service, Kubernetes configuration profiles, experiment runners, and recorded results. The experiments measure how Kubernetes detects and recovers from pod and node failures under different probe, memory, and node-timer settings.

This is an experimental test setup, not a production deployment or a general-purpose Kubernetes operator.

## Academic context and citation

This repository accompanies the Bachelor of ICT Honours (BICT431) minor dissertation at the **University of Mpumalanga**:

- **Title:** *Evaluating Kubernetes Self-Healing Mechanisms Using Mean Time to Recovery (MTTR) Under Controlled Failure Conditions in Cloud-Native Microservices*
- **Author:** Mphephu Tshifhiwa Samuel (222012773)
- **Supervisor:** Dr Hope Mauwa
- **Year:** 2026

## What is measured

The runners send steady HTTP traffic to `mttr-app-svc` while injecting a fault into the replica scheduled on `minikube-m02`. A second replica runs on `minikube-m03`. Each trial resets the deployment, checks node and endpoint readiness, runs a steady-state load check, then observes the recovery.

The trial CSVs include:

| Field | Meaning in the runner |
| --- | --- |
| `tti_es_s` | Seconds from fault injection until the target is no longer ready in the EndpointSlices. |
| `tti_last_req_s` | Seconds from fault injection until the last successful request attributed to the faulty pod. |
| `ttr_service_s` | Seconds until the runner observes a five-second window of successful requests after an error. |
| `t5_ready_s` | Seconds until the runner observes the recovered pod ready. |
| `ttr_instance_s` | Seconds from fault injection until a successful request reaches the recovered pod after readiness. |
| `restarts`, `oom_killed` | Container restart count and whether the runner observed an OOM kill. |
| `valid`, `notes` | Whether the runner accepted the trial and, if not, the recorded reason. |

These are runner-defined observation times, not Kubernetes API event timestamps. The load generator samples at a configured request rate, and the runner polls Kubernetes state every 150 ms during fault observation.

## Experiment profiles

Profiles are Kubernetes Deployments in [manifests/profiles/](manifests/profiles/). They keep two replicas and prefer to spread them across nodes.

| Comparison | Profiles | Fault or intervention | Configuration difference |
| --- | --- | --- | --- |
| H1 | `C0_F1` vs `C1_F1` | The runner sends `POST /hang` to the target pod. | Default readiness/liveness probes vs startup, faster readiness, and faster liveness probes. |
| H1 check | `C1_F1` | Unfaulted load, then external `SIGSTOP` to the container process. | Checks false restarts under load and behavior for a process that stops responding. |
| H2a | `C0_F2` vs `C2_F2` | Chaos Mesh `StressChaos` memory stress at 200, 300, 400, 500, and 600 MiB. | Burstable vs Guaranteed QoS resource settings. |
| H2b | `C0_F2b` vs `C2_F2b` | The runner sends `POST /oom_window`; the service allocates memory. | Burstable vs Guaranteed resources, with a 280 MiB allocation retried after restarts while the 60-second fault window remains active. The window timestamp is stored in the `/fault` `emptyDir` volume. |
| H3 | `C0_F3` vs `C3_F3` | The runner pauses the `minikube-m02` Docker container. | Default node-failure handling vs 15-second `NoExecute` tolerations; the runner switches node-monitor and lease timers between conditions. |

`C0_F1` uses 10-second readiness and liveness probes. `C1_F1` and the later profiles use a startup probe, 1-second readiness checks with a failure threshold of two, and 5-second liveness checks. H3 timer values are managed by [scripts/switch_h3_timers.sh](scripts/switch_h3_timers.sh).

## Repository map

| Path | Purpose |
| --- | --- |
| [app/server.py](app/server.py) | Async HTTP service, health endpoints, and fault endpoints. |
| [app/Dockerfile](app/Dockerfile) | Image definition based on `python:3.12-slim`. |
| [manifests/app-smoke.yaml](manifests/app-smoke.yaml) | Basic two-replica Deployment and NodePort Service. |
| [manifests/loadgen-pod.yaml](manifests/loadgen-pod.yaml) | Load-generator pod pinned to the control-plane node. |
| [manifests/monitoring-values.yaml](manifests/monitoring-values.yaml) | Prometheus-related Helm values; disables Alertmanager and Grafana. |
| [manifests/profiles/](manifests/profiles/) | Deployment configurations used by the experiments. |
| [scripts/load_driver.py](scripts/load_driver.py) | Fixed-rate HTTP client writing timestamp, status, latency, and pod identity to CSV. |
| [scripts/run_experiment.py](scripts/run_experiment.py) | Trial reset, fault injection, polling, and CSV output. |
| [scripts/run_main_study.py](scripts/run_main_study.py) | Main study orchestration, reruns, H1 checks, and H3 blocks. |
| [scripts/run_f2a.py](scripts/run_f2a.py) | H2a memory dose-response runner. |
| [scripts/run_h1_sigstop.py](scripts/run_h1_sigstop.py) | Standalone H1 `SIGSTOP` check runner. |
| [scripts/analyze_results.py](scripts/analyze_results.py) | Descriptive statistics, hypothesis tests, robustness checks, and JSON summary generation. |
| [scripts/pilot_summary.py](scripts/pilot_summary.py) | Pilot CSV summary and rough sample-size calibration. |
| [scripts/gen_manifests.py](scripts/gen_manifests.py) | Generates YAML profiles from an embedded template. |
| [results/](results/) | Trial CSVs, protocol metadata, analysis output, and a results archive. |

## Runtime requirements

The experiment scripts assume an already configured cluster and working local tooling. They call `kubectl` and Docker directly. They also expect:

- A namespace named `mttr-lab`.
- Worker nodes named `minikube-m02` and `minikube-m03`, plus the Minikube control-plane node.
- `mttr-app-svc`, `mttr-loadgen`, the target Deployment, and the app image referenced by the selected profile to be available.
- The load-generator pod to have `load_driver.py` at `/tmp/load_driver.py`.
- Chaos Mesh `StressChaos` support for H2a.
- For analysis, Python packages `numpy`, `pandas`, and `scipy`.

The repository does not include a cluster bootstrap script or a complete image-build/load-generator provisioning workflow. The profile manifests reference `mttr-microservice:v2`; the Dockerfile specifies how to build the service image, but the repository does not automate building or loading it into Minikube.

## Running experiments

Run commands from the repository root. These commands operate on the active Kubernetes context; confirm that it points to the intended test cluster before running them.

Run one pilot trial:

```sh
python3 scripts/run_experiment.py --cell C1_F1 --trials 1 --phase pilot
```

`--phase` selects the output filename suffix. The runner appends to `results/<cell>_<phase>.csv`. For the main study, use the orchestrator:

```sh
python3 scripts/run_main_study.py --exp H1 --n 50
python3 scripts/run_main_study.py --exp H1_checks
python3 scripts/run_h1_sigstop.py
python3 scripts/run_main_study.py --exp H2a
python3 scripts/run_main_study.py --exp H2b --n 50
python3 scripts/run_main_study.py --exp H3
```

The `--exp` choices are `H1`, `H1_checks`, `H2a`, `H2b`, and `H3`. H1 and H2b default to 50 valid trials per arm. H3 runs three alternating default/tuned blocks of ten trials per arm. H1 checks run 30 unfaulted trials and ten valid `SIGSTOP` trials (use `scripts/run_h1_sigstop.py` directly for the 10 `SIGSTOP` trials; it resolves the container host PID via `crictl` go-template without requiring `jq` inside `minikube-m02`). H2a runs ten trials at each of five memory doses for both profiles.

Run H2a separately with a different trial count or output phase:

```sh
python3 scripts/run_f2a.py --trials-per-dose 3 --phase pilot
```

Analyze the checked-in main-study data:

```sh
python3 scripts/analyze_results.py
python3 scripts/pilot_summary.py
```

The analysis script reads the named main-study CSVs from `results/` and rewrites `results/analysis_summary.json`. It is not a generic analyzer for arbitrary CSV files.

## Recorded results

The checked-in `analysis_summary.json` reports the following mean `ttr_instance_s` values for the confirmatory comparisons:

| Comparison | Baseline mean | Tuned mean | Relative reduction reported |
| --- | ---: | ---: | ---: |
| H1: `C0_F1` vs `C1_F1` (50 valid trials per arm) | 47.15 s | 26.18 s | 44.5% |
| H2b: `C0_F2b` vs `C2_F2b` (50 valid trials per arm) | 102.32 s | 8.32 s | 91.9% |
| H3: `C0_F3` vs `C3_F3` (30 valid trials per arm) | 327.90 s | 32.87 s | 90.0% |

These figures summarize the checked-in dataset, not a guaranteed result from rerunning the experiments. See the CSVs and JSON summary for the full metrics, confidence intervals, and checks.

## Operational cautions and known drift

- The runners are intrusive. They scale and replace the `mttr-app` Deployment, inject memory pressure, pause a Minikube worker, and for H3 edit node timer configuration and restart kubelets. Use a disposable experiment cluster.
- H3's timer-switch script restores the default timer values at the end of a successful run. Interruptions can leave the cluster in the tuned state; inspect and restore it before using the cluster for anything else.
- The checked-in profiles and `gen_manifests.py` are not equivalent. The profiles reference `mttr-microservice:v2` and mount `/fault`; the generator template uses `mttr-microservice:v1` and does not define that volume mount. Running it would overwrite the profile files with different Deployments. Do not regenerate the checked-in profiles without reconciling the template against the experiment configuration.
- `manifests/app-smoke.yaml` is a separate smoke deployment. It uses `mttr-microservice:v1` and does not represent the experiment profiles.
- The repository records the environment and protocol amendments in [results/protocol_freeze.json](results/protocol_freeze.json). That file describes the original recorded environment; verify the current cluster rather than assuming it matches.
