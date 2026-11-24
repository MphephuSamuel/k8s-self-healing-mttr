import argparse, csv, json, os, random, subprocess, sys, time
sys.path.insert(0, "scripts")
import run_experiment as rexp

FIELDS = ["trial","cell","target_pod","recovered_pod","tti_es_s","tti_last_req_s","ttr_service_s","t5_ready_s","ttr_instance_s","restarts","oom_killed","valid","notes"]
INVALID_CSV = "results/invalid_trials_main.csv"

def append_row(path, row, fields=FIELDS):
    write_hdr = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_hdr:
            w.writeheader()
        w.writerow(row)

def count_valid(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for r in csv.DictReader(f) if str(r.get("valid")) == "1")

def run_until_valid(cell, trial_num, steady_sec=30):
    while True:
        row = rexp.run_trial(cell, trial_num, steady_sec)
        if row.get("valid") == 1:
            return row
        print(f"  [INVALID -> LOGGED & RERUNNING] {cell} trial {trial_num}: {row.get('notes')}")
        append_row(INVALID_CSV, row)
        time.sleep(3)

def run_randomized_pair(c0, c1, n_target, seed=42, steady_sec=30):
    p0, p1 = f"results/{c0}_main.csv", f"results/{c1}_main.csv"
    rem0 = max(0, n_target - count_valid(p0))
    rem1 = max(0, n_target - count_valid(p1))
    if rem0 == 0 and rem1 == 0:
        print(f"[DONE] {c0} and {c1} already have {n_target} valid main trials.")
        return

    print(f"=== Discarding 2 warm-up trials before {c0}/{c1} session ===")
    rexp.run_trial(c1, 0, steady_sec=10)
    rexp.run_trial(c0, 0, steady_sec=10)

    schedule = [c0]*rem0 + [c1]*rem1
    random.Random(seed).shuffle(schedule)
    idx = {c0: count_valid(p0), c1: count_valid(p1)}

    for step, cell in enumerate(schedule, 1):
        idx[cell] += 1
        print(f"[{step}/{len(schedule)}] Running {cell} valid trial {idx[cell]}/{n_target}...")
        row = run_until_valid(cell, idx[cell], steady_sec=steady_sec)
        append_row(f"results/{cell}_main.csv", row)
        print(f"  -> OK: TTI={row['tti_es_s']}s, TTR_svc={row['ttr_service_s']}s, TTR_inst={row['ttr_instance_s']}s, restarts={row['restarts']}")

def run_h1_checks():
    # 1. N=30 un-faulted 50 RPS runs on C1_F1 (60s each) for false-positive non-inferiority (Delta_FP <= 0.10/run)
    out_fp = "results/H1_unfaulted_main.csv"
    done_fp = sum(1 for _ in open(out_fp)) - 1 if os.path.exists(out_fp) else 0
    if done_fp < 30:
        print(f"=== Running H1 Un-Faulted False-Restart Check ({done_fp}/30 done) ===")
        target, err = rexp.five_gate_reset("manifests/profiles/C1_F1.yaml", 50, 10)
        if err:
            raise RuntimeError(err)
        for i in range(done_fp + 1, 31):
            before = sum(p["status"]["containerStatuses"][0]["restartCount"] for p in rexp.get_pods())
            rexp.sh("kubectl exec -n mttr-lab mttr-loadgen -- python3 /tmp/load_driver.py 50 60 /tmp/fp.csv")
            after = sum(p["status"]["containerStatuses"][0]["restartCount"] for p in rexp.get_pods())
            row = {"trial": i, "cell": "C1_F1_unfaulted", "rps": 50, "duration_s": 60, "false_restarts": after - before}
            append_row(out_fp, row, fields=["trial","cell","rps","duration_s","false_restarts"])
            print(f"  [Un-faulted {i}/30] false_restarts={after - before}")

    # 2. 10-trial external kill -STOP robustness check on C1_F1
    out_stop = "results/H1_sigstop_main.csv"
    done_stop = count_valid(out_stop)
    if done_stop < 10:
        print(f"=== Running H1 External kill -STOP Robustness Check ({done_stop}/10 done) ===")
        for i in range(done_stop + 1, 11):
            while True:
                target, err = rexp.five_gate_reset("manifests/profiles/C1_F1.yaml", 50, 15)
                if err:
                    continue
                tname = target["metadata"]["name"]
                cid = target["status"]["containerStatuses"][0]["containerID"].replace("containerd://", "")
                load_proc = subprocess.Popen("kubectl exec -n mttr-lab mttr-loadgen -- python3 /tmp/load_driver.py 50 70 /tmp/trial.csv", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(3.0)
                t0 = time.time()
                # Send SIGSTOP from outside the container PID namespace on minikube-m02
                pid = json.loads(rexp.sh(f"minikube ssh -n minikube-m02 sudo crictl inspect {cid}").stdout)["info"]["pid"]; rexp.sh(f"minikube ssh -n minikube-m02 sudo kill -STOP {pid}", check=False)
                t_iso, t_ready, restarts = None, None, 0
                while time.time() < t0 + 62:
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
                if row["valid"] == 1:
                    append_row(out_stop, row)
                    print(f"  [kill -STOP {i}/10] TTI={row['tti_es_s']}s, TTR_inst={row['ttr_instance_s']}s")
                    break

def run_h3_abab(n_blocks=3, block_size=10, steady_sec=30):
    p0, p3 = "results/C0_F3_main.csv", "results/C3_F3_main.csv"
    for b in range(1, n_blocks + 1):
        for mode, cell, path in [("default", "C0_F3", p0), ("tuned", "C3_F3", p3)]:
            target_n = b * block_size
            cur = count_valid(path)
            if cur >= target_n:
                print(f"[Block {b}] {cell} already has {cur}/{target_n} valid trials, skipping.")
                continue
            print(f"=== [H3 ABAB Block {b}/{n_blocks}] Switching timers to {mode} for {cell} ===")
            subprocess.run(f"bash scripts/switch_h3_timers.sh {mode}", shell=True, check=True)
            for t in range(cur + 1, target_n + 1):
                print(f"[H3 Block {b} | {cell}] Running valid trial {t}/{n_blocks*block_size}...")
                row = run_until_valid(cell, t, steady_sec=steady_sec)
                append_row(path, row)
                print(f"  -> OK: TTI={row['tti_es_s']}s, TTR_svc={row['ttr_service_s']}s, TTR_inst={row['ttr_instance_s']}s")
    subprocess.run("bash scripts/switch_h3_timers.sh default", shell=True, check=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, choices=["H1", "H1_checks", "H2a", "H2b", "H3"])
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()

    # Record frozen protocol metadata once
    freeze_path = "results/protocol_freeze.json"
    if not os.path.exists(freeze_path):
        meta = {
            "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "k8s_version": "v1.35.0", "minikube_version": "v1.39.0", "docker_version": "29.0.1",
            "cgroup": "cgroup2fs", "host": "Windows 11 / WSL2 (i5-1235U, 10GB WSL2 RAM)",
            "amendments": ["F3 uses docker pause/unpause on minikube-m02 to preserve Minikube CNI state; app uses asyncio server with backlog=256."]
        }
        json.dump(meta, open(freeze_path, "w"), indent=2)

    if args.exp == "H1":
        run_randomized_pair("C0_F1", "C1_F1", args.n, seed=101)
    elif args.exp == "H1_checks":
        run_h1_checks()
    elif args.exp == "H2a":
        subprocess.run("python3 scripts/run_f2a.py --trials-per-dose 10 --phase main", shell=True, check=True)
    elif args.exp == "H2b":
        run_randomized_pair("C0_F2b", "C2_F2b", args.n, seed=202)
    elif args.exp == "H3":
        run_h3_abab(n_blocks=3, block_size=10)

if __name__ == "__main__":
    main()
