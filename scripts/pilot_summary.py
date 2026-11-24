import glob, math, pandas as pd

stats = {}
print("=== PILOT SUMMARY ===")
for path in sorted(glob.glob("results/*pilot.csv")):
    df = pd.read_csv(path, on_bad_lines="skip")
    name = path.replace("results/", "").replace("_pilot.csv", "")
    if "valid" in df.columns:
        v = df[df["valid"] == 1]
        m = v["ttr_instance_s"].mean()
        s = v["ttr_instance_s"].std()
        med = v["ttr_instance_s"].median()
        stats[name] = (len(v), m, s, med)
        print(f"{name:10s} | total={len(df):2d} | valid={len(v):2d} | mean={m:6.2f}s | sd={s:5.2f}s | median={med:6.2f}s")
    else:
        print(f"{name:10s} | rows={len(df)}")

print("\n=== TWO-SAMPLE POWER CALIBRATION (alpha_adj=0.0167, power=0.80, Pitman ARE=0.864) ===")
z_a, z_b = 2.394, 0.842
for label, c0, c1 in [("H1 (Probes)", "C0_F1", "C1_F1"), ("H2b (QoS/OOM)", "C0_F2b", "C2_F2b"), ("H3 (Node Evict)", "C0_F3", "C3_F3")]:
    if c0 in stats and c1 in stats:
        _, m0, s0, _ = stats[c0]
        _, m1, s1, _ = stats[c1]
        delta = abs(m0 - m1)
        s0 = 0 if math.isnan(s0) else s0
        s1 = 0 if math.isnan(s1) else s1
        n_param = ((z_a + z_b)**2 * (s0**2 + s1**2)) / (delta**2) if delta > 0 else 999
        n_mwu = math.ceil(n_param / 0.864)
        print(f"{label:16s} | delta={delta:6.2f}s | s0={s0:5.2f}s, s1={s1:5.2f}s | min N (MWU) = {n_mwu}")
