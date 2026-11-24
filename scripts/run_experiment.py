import argparse, csv, json, os, subprocess, sys, time

NS = "mttr-lab"

def sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, check=check)

def get_pods():
    out = sh(f"kubectl get pods -n {NS} -l app=mttr-app -o json").stdout
    return json.loads(out)["items"]

def get_ready_endpoints():
    out = sh(f"kubectl get endpointslices -n {NS} -l kubernetes.io/service-name=mttr-app-svc -o json").stdout
    eps = {}
    for item in json.loads(out)["items"]:
        for ep in item.get("endpoints", []):
            name = (ep.get("targetRef") or {}).get("name", "")
            ready = ep.get("conditions", {}).get("ready", False)
            if name:
                eps[name] = ready
    return eps

def five_gate_reset(profile_yaml, rps, steady_sec):
    # Gate 1: Unpause m02 if needed & delete any Chaos Mesh CRDs
    sh("docker unpause minikube-m02 2>/dev/null || true", check=False)
    sh(f"kubectl delete stresschaos --all -n {NS} --ignore-not-found", check=False)
    for _ in range(60):
        nodes = json.loads(sh("kubectl get nodes -o json").stdout)["items"]
        ready = {n["metadata"]["name"]: any(c["type"]=="Ready" and c["status"]=="True" for c in n["status"]["conditions"]) for n in nodes}
        if ready.get("minikube-m02") and ready.get("minikube-m03"):
            break
        time.sleep(2)
    else:
        return None, "Gate 1 timeout: worker nodes not Ready"

    # Gate 2: Clean scale 0 -> 2 for fresh Pod UIDs (restartCount=0) & clean m02/m03 split
    sh(f"kubectl scale deployment/mttr-app -n {NS} --replicas=0", check=False)
    sh(f"kubectl wait --for=delete pod -l app=mttr-app -n {NS} --timeout=60s", check=False)
    sh(f"kubectl apply -f {profile_yaml}")
    sh(f"kubectl scale deployment/mttr-app -n {NS} --replicas=2")
    res = sh(f"kubectl rollout status deployment/mttr-app -n {NS} --timeout=120s", check=False)
    if res.returncode != 0:
        return None, "Gate 2 timeout: rollout failed"

    # Gate 3: Verify exact placement (1 Ready on m02 AND 1 Ready on m03)
    target_m02, peer_m03 = None, None
    for _ in range(30):
        pods = [p for p in get_pods() if p["status"].get("phase")=="Running" and not p["metadata"].get("deletionTimestamp")]
        m02 = [p for p in pods if p["spec"].get("nodeName")=="minikube-m02" and p["status"].get("containerStatuses", [{}])[0].get("ready")]
        m03 = [p for p in pods if p["spec"].get("nodeName")=="minikube-m03" and p["status"].get("containerStatuses", [{}])[0].get("ready")]
        if len(m02) == 1 and len(m03) == 1:
            target_m02, peer_m03 = m02[0], m03[0]
            break
        time.sleep(1)
    if not target_m02:
        return None, "Gate 3 failed: replicas not split 1-on-m02 and 1-on-m03"

    # Gate 4: Verify both Pod IPs are ready in EndpointSlices
    for _ in range(20):
        eps = get_ready_endpoints()
        if eps.get(target_m02["metadata"]["name"]) and eps.get(peer_m03["metadata"]["name"]):
            break
        time.sleep(0.5)
    else:
        return None, "Gate 4 failed: EndpointSlices not ready"

    time.sleep(3.0)  # Allow kube-proxy iptables sync after EndpointSlices ready
    # Gate 5: Steady-state verification window (0% HTTP 5xx/timeouts)
    sh(f"kubectl exec -n {NS} mttr-loadgen -- python3 /tmp/load_driver.py {rps} {steady_sec} /tmp/steady.csv")
    csv_txt = sh(f"kubectl exec -n {NS} mttr-loadgen -- cat /tmp/steady.csv").stdout.splitlines()
    rows_g5 = [line.split(",") for line in csv_txt[1:] if len(line.split(",")) >= 2]
    http_5xx = sum(1 for r in rows_g5 if r[1].isdigit() and int(r[1]) >= 500)
    non_200 = sum(1 for r in rows_g5 if r[1] != "200")
    if http_5xx > 0 or (len(rows_g5) > 0 and non_200 / len(rows_g5) > 0.02):
        return None, f"Gate 5 failed: {http_5xx} HTTP 5xx, {non_200}/{len(rows_g5)} non-200"

    return target_m02, None

def analyze_locust_log(log_lines, t0, target_pod, recovered_pod, t_ready_ts):
    reqs = []
    for line in log_lines[1:]:
        line = line.replace("\x00", "")
        line = line.replace("\x00", "")
        parts = line.strip().split(",")
        if len(parts) == 5:
            reqs.append((float(parts[0]), int(parts[1]), float(parts[2]), parts[3], parts[4]))
    reqs.sort(key=lambda x: x[0])

    faulty_after = [r[0] for r in reqs if r[0] >= t0 and (t_ready_ts is None or r[0] < t_ready_ts - 1.0) and r[3] == target_pod and r[1] == 200]
    t_last_faulty = max(faulty_after) if faulty_after else t0

    post = [r for r in reqs if r[0] >= t0]
    err_times = [r[0] for r in post if r[1] != 200]
    if not err_times:
        ttr_service = 0.0
    else:
        t_service_slo = None
        first_err = err_times[0]
        for r in post:
            if r[0] < first_err:
                continue
            win = [x for x in post if r[0] <= x[0] <= r[0] + 5.0]
            if win and all(x[1] == 200 for x in win):
                t_service_slo = r[0]
                break
        ttr_service = round(t_service_slo - t0, 3) if t_service_slo else -1.0

    t6 = None
    if t_ready_ts:
        for r in post:
            if r[0] >= t_ready_ts and r[1] == 200 and r[3] == recovered_pod:
                t6 = r[0]
                break
    return round(t_last_faulty - t0, 3), ttr_service, (round(t6 - t0, 3) if t6 else -1.0)

def run_trial(cell, trial_idx, steady_sec):
    is_f1 = cell.endswith("F1")
    is_f3 = cell.endswith("F3")
    rps = 80 if "F2" in cell else 50
    yaml_path = f"manifests/profiles/{cell}.yaml"

    target, err = five_gate_reset(yaml_path, rps, steady_sec)
    if err:
        return {"trial": trial_idx, "cell": cell, "valid": 0, "notes": err}

    target_name = target["metadata"]["name"]
    obs_dur = 480 if cell == "C0_F3" else (150 if (is_f3 or cell == "C0_F2b") else 95)

    load_proc = subprocess.Popen(
        f"kubectl exec -n {NS} mttr-loadgen -- python3 /tmp/load_driver.py {rps} {obs_dur} /tmp/trial.csv",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(3.0)

    t0 = time.time()
    if is_f1:
        sh(f"kubectl exec -n {NS} {target_name} -- python3 -c 'import urllib.request; urllib.request.urlopen(urllib.request.Request(\"http://127.0.0.1:8080/hang\", method=\"POST\"))'")
    elif is_f3:
        sh("docker pause minikube-m02")
    elif cell in ("C0_F2b", "C2_F2b"):
        sh(f"kubectl exec -n {NS} {target_name} -- python3 -c 'import urllib.request; urllib.request.urlopen(urllib.request.Request(\"http://127.0.0.1:8080/oom_window\", method=\"POST\"))'")

    t_isolated_es, t_ready_ts, recovered_pod = None, None, target_name
    restarts, oom_killed = 0, 0
    deadline = t0 + obs_dur - 8
    while time.time() < deadline:
        now = time.time()
        eps = get_ready_endpoints()
        if t_isolated_es is None and not eps.get(target_name, False):
            t_isolated_es = now

        pods = get_pods()
        if is_f3:
            repl = [p for p in pods if p["metadata"]["name"] != target_name and p["spec"].get("nodeName") == "minikube-m03"
                    and p["metadata"]["creationTimestamp"] and p["status"].get("containerStatuses", [{}])[0].get("ready")]
            if len(repl) >= 2:
                newest = sorted(repl, key=lambda p: p["metadata"]["creationTimestamp"])[-1]
                recovered_pod = newest["metadata"]["name"]
                t_ready_ts = now
                break
        else:
            p = next((x for x in pods if x["metadata"]["name"] == target_name), None)
            if p:
                cs = p["status"].get("containerStatuses", [{}])[0]
                restarts = cs.get("restartCount", 0)
                last_reason = (cs.get("lastState", {}).get("terminated") or {}).get("reason", "")
                if last_reason == "OOMKilled":
                    oom_killed = 1
                if restarts >= 1 and cs.get("ready") and eps.get(target_name, False):
                    t_ready_ts = now
                    break
        time.sleep(0.15)

    time.sleep(6.0)
    load_proc.terminate()
    load_proc.wait()
    sh("kubectl exec -n mttr-lab mttr-loadgen -- pkill -TERM -f load_driver.py 2>/dev/null || true", check=False)
    time.sleep(1.0)

    log_lines = sh(f"kubectl exec -n {NS} mttr-loadgen -- cat /tmp/trial.csv", check=False).stdout.splitlines()
    tti_last_req, ttr_service, ttr_instance = analyze_locust_log(log_lines, t0, target_name, recovered_pod, t_ready_ts)
    tti_es = round(t_isolated_es - t0, 3) if t_isolated_es else -1.0
    t5_rel = round(t_ready_ts - t0, 3) if t_ready_ts else -1.0

    if is_f3:
        sh("docker unpause minikube-m02 2>/dev/null || true", check=False)

    return {
        "trial": trial_idx, "cell": cell, "target_pod": target_name, "recovered_pod": recovered_pod,
        "tti_es_s": tti_es, "tti_last_req_s": tti_last_req, "ttr_service_s": ttr_service,
        "t5_ready_s": t5_rel, "ttr_instance_s": ttr_instance,
        "restarts": restarts, "oom_killed": oom_killed, "valid": 1 if ttr_instance > 0 else 0, "notes": "ok"
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", required=True)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--phase", default="pilot")
    ap.add_argument("--steady-sec", type=int, default=30)
    args = ap.parse_args()

    out_csv = f"results/{args.cell}_{args.phase}.csv"
    fields = ["trial","cell","target_pod","recovered_pod","tti_es_s","tti_last_req_s","ttr_service_s","t5_ready_s","ttr_instance_s","restarts","oom_killed","valid","notes"]
    write_hdr = not os.path.exists(out_csv)

    with open(out_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_hdr:
            w.writeheader()
        for i in range(1, args.trials + 1):
            print(f"[{args.cell} | {args.phase}] Starting trial {i}/{args.trials}...")
            row = run_trial(args.cell, i, args.steady_sec)
            w.writerow(row)
            f.flush()
            print("  Result:", row)

if __name__ == "__main__":
    main()
