import argparse, csv, json, os, subprocess, time

NS = "mttr-lab"
DOSES = [200, 300, 400, 500, 600]

def sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, check=check)

def run_dose(cell, dose_mi, trial_idx):
    sh(f"kubectl delete stresschaos --all -n {NS} --ignore-not-found", check=False)
    sh(f"kubectl apply -f manifests/profiles/{cell}.yaml")
    sh(f"kubectl rollout restart deployment/mttr-app -n {NS}")
    sh(f"kubectl rollout status deployment/mttr-app -n {NS} --timeout=120s")

    pods = json.loads(sh(f"kubectl get pods -n {NS} -l app=mttr-app -o json").stdout)["items"]
    m02 = [p for p in pods if p["spec"].get("nodeName")=="minikube-m02" and p["status"].get("phase")=="Running" and not p["metadata"].get("deletionTimestamp")]
    if not m02:
        return {"trial": trial_idx, "cell": cell, "dose_mi": dose_mi, "oom_killed": -1, "restarts": -1, "exit_code": -1, "notes": "no_m02_pod"}
    pod = m02[0]["metadata"]["name"]

    crd = f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: f2a-{dose_mi}
  namespace: {NS}
spec:
  mode: one
  selector:
    namespaces: [{NS}]
    pods:
      {NS}: [{pod}]
  stressors:
    memory:
      workers: 1
      size: "{dose_mi}MiB"
  duration: "20s"
"""
    subprocess.run("kubectl apply -f -", input=crd, shell=True, text=True, check=True, capture_output=True)
    time.sleep(22)

    p = json.loads(sh(f"kubectl get pod -n {NS} {pod} -o json").stdout)
    cs = p["status"]["containerStatuses"][0]
    restarts = cs.get("restartCount", 0)
    term = cs.get("lastState", {}).get("terminated") or {}
    reason = term.get("reason", "")
    exit_code = term.get("exitCode", 0)
    oom = 1 if reason == "OOMKilled" or exit_code == 137 else 0

    sh(f"kubectl delete stresschaos f2a-{dose_mi} -n {NS} --ignore-not-found", check=False)
    return {"trial": trial_idx, "cell": cell, "dose_mi": dose_mi, "oom_killed": oom, "restarts": restarts, "exit_code": exit_code, "notes": reason or "survived"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials-per-dose", type=int, default=3)
    ap.add_argument("--phase", default="pilot")
    args = ap.parse_args()

    out_csv = f"results/F2a_{args.phase}.csv"
    fields = ["trial", "cell", "dose_mi", "oom_killed", "restarts", "exit_code", "notes"]
    write_hdr = not os.path.exists(out_csv)
    with open(out_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_hdr:
            w.writeheader()
        for cell in ["C0_F2", "C2_F2"]:
            for dose in DOSES:
                for t in range(1, args.trials_per_dose + 1):
                    row = run_dose(cell, dose, t)
                    w.writerow(row); f.flush()
                    print("F2a:", row)

if __name__ == "__main__":
    main()
