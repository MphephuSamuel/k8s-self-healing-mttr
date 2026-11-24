import csv, json, os, subprocess, sys, time
sys.path.insert(0, "scripts")
import run_experiment as rexp

OUT = "results/H1_sigstop_main.csv"
FIELDS = ["trial","cell","target_pod","recovered_pod","tti_es_s","tti_last_req_s","ttr_service_s","t5_ready_s","ttr_instance_s","restarts","oom_killed","valid","notes"]

done = 0
if os.path.exists(OUT):
    done = sum(1 for r in csv.DictReader(open(OUT)) if str(r.get("valid")) == "1")

print(f"=== H1 External kill -STOP Robustness Check ({done}/10 done) ===", flush=True)
for i in range(done + 1, 11):
    while True:
        target, err = rexp.five_gate_reset("manifests/profiles/C1_F1.yaml", 50, 15)
        if err:
            print(f"  [retry] {err}", flush=True); continue
        tname = target["metadata"]["name"]
        cid = target["status"]["containerStatuses"][0]["containerID"].replace("containerd://", "")
        pid_res = rexp.sh(f"docker exec minikube-m02 crictl inspect --output go-template --template '{{{{.info.pid}}}}' {cid}", check=False)
        pid = pid_res.stdout.strip()
        if not pid.isdigit():
            print(f"  [retry] Could not get PID: {pid_res.stdout} {pid_res.stderr}", flush=True); time.sleep(2); continue

        load_proc = subprocess.Popen("kubectl exec -n mttr-lab mttr-loadgen -- python3 /tmp/load_driver.py 50 75 /tmp/trial.csv", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3.0)
        t0 = time.time()
        print(f"  [Trial {i}/10] Sending kill -STOP to host PID {pid} ({tname})...", flush=True)
        rexp.sh(f"docker exec minikube-m02 kill -STOP {pid}", check=False)

        t_iso, t_ready, restarts = None, None, 0
        while time.time() < t0 + 65:
            now = time.time()
            eps = rexp.get_ready_endpoints()
            if t_iso is None and not eps.get(tname, False):
                t_iso = now
            p = next((x for x in rexp.get_pods() if x["metadata"]["name"] == tname), None)
            if p:
                cs = p["status"]["containerStatuses"][0]
                restarts = cs.get("restartCount", 0)
                if restarts >= 1 and cs.get("ready") and eps.get(tname, False):
                    t_ready = now; break
            time.sleep(0.15)

        time.sleep(5.0)
        load_proc.terminate(); load_proc.wait()
        rexp.sh("kubectl exec -n mttr-lab mttr-loadgen -- pkill -TERM -f load_driver.py 2>/dev/null || true", check=False)
        time.sleep(1.0)
        logs = rexp.sh("kubectl exec -n mttr-lab mttr-loadgen -- cat /tmp/trial.csv", check=False).stdout.splitlines()
        tti_last, ttr_svc, ttr_inst = rexp.analyze_locust_log(logs, t0, tname, tname, t_ready)
        row = {"trial": i, "cell": "C1_F1_sigstop", "target_pod": tname, "recovered_pod": tname,
               "tti_es_s": round(t_iso - t0, 3) if t_iso else -1.0, "tti_last_req_s": tti_last,
               "ttr_service_s": ttr_svc, "t5_ready_s": round(t_ready - t0, 3) if t_ready else -1.0,
               "ttr_instance_s": ttr_inst, "restarts": restarts, "oom_killed": 0, "valid": 1 if ttr_inst > 0 else 0, "notes": "kill-STOP"}
        print(f"  -> Result: TTI={row['tti_es_s']}s, TTR_svc={row['ttr_service_s']}s, TTR_inst={row['ttr_instance_s']}s, valid={row['valid']}", flush=True)
        if row["valid"] == 1:
            write_hdr = not os.path.exists(OUT)
            with open(OUT, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if write_hdr: w.writeheader()
                w.writerow(row)
            break
