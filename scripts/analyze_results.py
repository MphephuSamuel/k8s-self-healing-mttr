import csv, json, math, os
import numpy as np
import pandas as pd
from scipy import stats

RESULTS_DIR = "results"
np.random.seed(42)
B_BOOT = 10000

def load_valid(fname):
    path = os.path.join(RESULTS_DIR, fname)
    df = pd.read_csv(path, on_bad_lines="skip")
    if "valid" in df.columns:
        df = df[df["valid"].astype(str) == "1"].copy()
    for col in ["tti_es_s", "tti_last_req_s", "ttr_service_s", "t5_ready_s", "ttr_instance_s", "restarts"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)

def p95_binomial_ci(x, alpha=0.05):
    xs = np.sort(np.asarray(x, dtype=float))
    n = len(xs)
    lo_idx = max(1, int(stats.binom.ppf(alpha / 2, n, 0.95)))
    hi_idx = min(n, int(stats.binom.ppf(1 - alpha / 2, n, 0.95)) + 1)
    cov = stats.binom.cdf(hi_idx - 1, n, 0.95) - stats.binom.cdf(lo_idx - 1, n, 0.95)
    return float(xs[lo_idx - 1]), float(xs[hi_idx - 1]), float(cov)

def p95_bca_ci(x):
    arr = np.asarray(x, dtype=float)
    try:
        res = stats.bootstrap((arr,), lambda d: np.percentile(d, 95),
                              n_resamples=B_BOOT, method="BCa", random_state=42)
        lo, hi = float(res.confidence_interval.low), float(res.confidence_interval.high)
        if np.isnan(lo) or np.isnan(hi):
            raise ValueError("NaN BCa")
        return lo, hi, "BCa"
    except Exception:
        res = stats.bootstrap((arr,), lambda d: np.percentile(d, 95),
                              n_resamples=B_BOOT, method="percentile", random_state=42)
        return float(res.confidence_interval.low), float(res.confidence_interval.high), "percentile"

def desc_stats(x):
    arr = np.asarray(x, dtype=float)
    arr = arr[~np.isnan(arr)]
    n = len(arr)
    mean = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))
    se = sd / math.sqrt(n)
    t_crit = stats.t.ppf(0.975, n - 1)
    med = float(np.median(arr))
    q1, q3 = float(np.percentile(arr, 25)), float(np.percentile(arr, 75))
    p95 = float(np.percentile(arr, 95))
    b_lo, b_hi, b_cov = p95_binomial_ci(arr)
    bca_lo, bca_hi, bca_meth = p95_bca_ci(arr)
    return {
        "n": n, "mean": mean, "sd": sd,
        "mean_ci_lo": mean - t_crit * se, "mean_ci_hi": mean + t_crit * se,
        "median": med, "q1": q1, "q3": q3, "iqr": q3 - q1,
        "min": float(np.min(arr)), "max": float(np.max(arr)),
        "p95": p95,
        "p95_binom_lo": b_lo, "p95_binom_hi": b_hi, "p95_binom_cov": b_cov,
        "p95_boot_lo": bca_lo, "p95_boot_hi": bca_hi, "p95_boot_method": bca_meth
    }

def runs_test(x):
    arr = np.asarray(x, dtype=float)
    med = np.median(arr)
    signs = [1 if v > med else 0 for v in arr if v != med]
    n1 = sum(signs)
    n2 = len(signs) - n1
    if n1 == 0 or n2 == 0:
        return np.nan, np.nan
    runs = 1 + sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    mu = (2 * n1 * n2) / (n1 + n2) + 1
    var = (2 * n1 * n2 * (2 * n1 * n2 - n1 - n2)) / (((n1 + n2) ** 2) * (n1 + n2 - 1))
    z = (runs - mu) / math.sqrt(var)
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    return float(z), float(p)

def ljung_box_q(x, lags=5):
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    xc = arr - np.mean(arr)
    denom = np.sum(xc ** 2)
    q = 0.0
    for k in range(1, lags + 1):
        rk = np.sum(xc[k:] * xc[:-k]) / denom
        q += (rk ** 2) / (n - k)
    q *= n * (n + 2)
    p = 1.0 - stats.chi2.cdf(q, df=lags)
    return float(q), float(p)

def moving_block_boot_diff(x0, x1, B=5000):
    rng = np.random.default_rng(42)
    def resample_block(a):
        n = len(a)
        b = max(2, math.ceil(n ** (1 / 3)))
        n_blocks = math.ceil(n / b)
        starts = rng.integers(0, n - b + 1, size=n_blocks)
        out = np.concatenate([a[s:s + b] for s in starts])[:n]
        return out
    diffs = np.empty(B)
    for i in range(B):
        s0, s1 = resample_block(x0), resample_block(x1)
        diffs[i] = np.median(s0[:, None] - s1[None, :])
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))

def compare_arms(x0, x1, label):
    a0 = np.asarray(x0, dtype=float)
    a1 = np.asarray(x1, dtype=float)
    u_stat, p_mwu = stats.mannwhitneyu(a0, a1, alternative="two-sided")
    # Exact Hodges-Lehmann shift (C0 - C_tuned)
    pw = a0[:, None] - a1[None, :]
    hl = float(np.median(pw))
    # Cliff's delta: P(X0 > X1) - P(X0 < X1)
    cliff = float((np.sum(pw > 0) - np.sum(pw < 0)) / (len(a0) * len(a1)))
    # Bootstrap 95% CIs for HL shift and relative MTTR reduction
    rng = np.random.default_rng(42)
    hl_boot = np.empty(B_BOOT)
    rel_boot = np.empty(B_BOOT)
    for i in range(B_BOOT):
        s0 = rng.choice(a0, size=len(a0), replace=True)
        s1 = rng.choice(a1, size=len(a1), replace=True)
        hl_boot[i] = np.median(s0[:, None] - s1[None, :])
        rel_boot[i] = (np.mean(s0) - np.mean(s1)) / np.mean(s0) * 100.0
    hl_lo, hl_hi = float(np.percentile(hl_boot, 2.5)), float(np.percentile(hl_boot, 97.5))
    rel_mean = float((np.mean(a0) - np.mean(a1)) / np.mean(a0) * 100.0)
    rel_lo, rel_hi = float(np.percentile(rel_boot, 2.5)), float(np.percentile(rel_boot, 97.5))
    # Welch's t & Hedges' g
    t_stat, p_welch = stats.ttest_ind(a0, a1, equal_var=False)
    n0, n1 = len(a0), len(a1)
    sp = math.sqrt(((n0 - 1) * np.var(a0, ddof=1) + (n1 - 1) * np.var(a1, ddof=1)) / (n0 + n1 - 2))
    d = (np.mean(a0) - np.mean(a1)) / sp
    J = 1.0 - 3.0 / (4.0 * (n0 + n1 - 2) - 1.0)
    hedges_g = float(d * J)
    return {
        "label": label, "u_stat": float(u_stat), "p_mwu": float(p_mwu),
        "hl_shift_s": hl, "hl_ci_lo": hl_lo, "hl_ci_hi": hl_hi,
        "cliffs_delta": cliff,
        "mean_diff_s": float(np.mean(a0) - np.mean(a1)),
        "rel_reduction_pct": rel_mean, "rel_ci_lo": rel_lo, "rel_ci_hi": rel_hi,
        "t_welch": float(t_stat), "p_welch": float(p_welch), "hedges_g": hedges_g
    }

def firth_logit(X, y, max_iter=100, tol=1e-8):
    n, k = X.shape
    beta = np.zeros(k)
    for _ in range(max_iter):
        eta = np.clip(X @ beta, -20, 20)
        p = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(p * (1.0 - p), 1e-12, None)
        W = np.diag(w)
        I = X.T @ W @ X
        I_inv = np.linalg.inv(I)
        H = np.sqrt(W) @ X @ I_inv @ X.T @ np.sqrt(W)
        h = np.diag(H)
        U = X.T @ (y - p + h * (0.5 - p))
        step = I_inv @ U
        # Step halving for stability
        if np.max(np.abs(step)) > 5.0:
            step = step * (5.0 / np.max(np.abs(step)))
        beta_new = beta + step
        if np.max(np.abs(step)) < tol:
            beta = beta_new
            break
        beta = beta_new
    eta = np.clip(X @ beta, -20, 20)
    p = 1.0 / (1.0 + np.exp(-eta))
    w = np.clip(p * (1.0 - p), 1e-12, None)
    I = X.T @ np.diag(w) @ X
    se = np.sqrt(np.diag(np.linalg.inv(I)))
    ll = np.sum(y * np.log(np.clip(p, 1e-15, 1.0)) + (1 - y) * np.log(np.clip(1 - p, 1e-15, 1.0)))
    sign, logdet = np.linalg.slogdet(I)
    pen_ll = float(ll + 0.5 * logdet)
    return beta, se, pen_ll

# ---- 1. Load all datasets ----
cells = {
    "C0_F1": load_valid("C0_F1_main.csv"),
    "C1_F1": load_valid("C1_F1_main.csv"),
    "H1_sigstop": load_valid("H1_sigstop_main.csv"),
    "C0_F2b": load_valid("C0_F2b_main.csv"),
    "C2_F2b": load_valid("C2_F2b_main.csv"),
    "C0_F3": load_valid("C0_F3_main.csv"),
    "C3_F3": load_valid("C3_F3_main.csv"),
}
f2a = pd.read_csv(os.path.join(RESULTS_DIR, "F2a_main.csv"))
unfaulted = pd.read_csv(os.path.join(RESULTS_DIR, "H1_unfaulted_main.csv"))
inv_path = os.path.join(RESULTS_DIR, "invalid_trials_main.csv")
invalid_df = pd.read_csv(inv_path, on_bad_lines="skip") if os.path.exists(inv_path) else pd.DataFrame()

print("=== 1. SAMPLE SIZES & INVALID TRIAL AUDIT ===")
for k, df in cells.items():
    print(f"  {k:12s}: valid N = {len(df)}")
print(f"  F2a_main    : rows    = {len(f2a)}")
print(f"  H1_unfaulted: rows    = {len(unfaulted)}")
if len(invalid_df) > 0:
    print("  Invalid trials logged by cell:")
    col_c = "cell" if "cell" in invalid_df.columns else invalid_df.columns[1]
    for cell_name, grp in invalid_df.groupby(col_c):
        print(f"    {str(cell_name):12s}: {len(grp)} invalid/rerun")

print("\n=== 2. DESCRIPTIVE STATISTICS (TTR_instance, TTI, TTR_service) ===")
summary = {"descriptives": {}, "serial_dependence": {}, "confirmatory": {}, "secondary": {}, "checks": {}}
for k, df in cells.items():
    summary["descriptives"][k] = {}
    for metric in ["ttr_instance_s", "tti_es_s", "ttr_service_s"]:
        d = desc_stats(df[metric])
        summary["descriptives"][k][metric] = d
        if metric == "ttr_instance_s":
            print(f"  {k:10s} [TTR_inst] N={d['n']:2d} | Mean={d['mean']:6.2f}s (SD={d['sd']:5.2f}, 95%CI [{d['mean_ci_lo']:6.2f},{d['mean_ci_hi']:6.2f}]) | Med={d['median']:6.2f}s (IQR {d['q1']:6.2f}-{d['q3']:6.2f}) | P95={d['p95']:6.2f}s (Binom [{d['p95_binom_lo']:6.2f},{d['p95_binom_hi']:6.2f}], {d['p95_boot_method']} [{d['p95_boot_lo']:6.2f},{d['p95_boot_hi']:6.2f}])")
    print(f"             [TTI / TTR_svc] TTI Mean={summary['descriptives'][k]['tti_es_s']['mean']:.2f}s (Med={summary['descriptives'][k]['tti_es_s']['median']:.2f}s) | TTR_svc Mean={summary['descriptives'][k]['ttr_service_s']['mean']:.2f}s (Med={summary['descriptives'][k]['ttr_service_s']['median']:.2f}s) | Restarts={df['restarts'].mean():.2f}")

print("\n=== 3. SERIAL-DEPENDENCE DIAGNOSTICS (TTR_instance) ===")
for k in ["C0_F1", "C1_F1", "C0_F2b", "C2_F2b", "C0_F3", "C3_F3"]:
    arr = cells[k]["ttr_instance_s"].values
    rz, rp = runs_test(arr)
    q5, qp = ljung_box_q(arr, lags=5)
    flag = (rp <= 0.05) or (qp <= 0.05)
    summary["serial_dependence"][k] = {"runs_z": rz, "runs_p": rp, "lb_q5": q5, "lb_p": qp, "triggered_block_boot": flag}
    print(f"  {k:8s}: Runs z={rz:+.3f} (p={rp:.4f}) | Ljung-Box Q(5)={q5:.3f} (p={qp:.4f}) | Triggered={flag}")

print("\n=== 4. CONFIRMATORY HYPOTHESIS TESTS (PRIMARY: TTR_instance, HOLM-BONFERRONI m=3) ===")
pairs = [("H1", "C0_F1", "C1_F1"), ("H2b", "C0_F2b", "C2_F2b"), ("H3", "C0_F3", "C3_F3")]
res_list = []
for h, c0, c1 in pairs:
    r = compare_arms(cells[c0]["ttr_instance_s"], cells[c1]["ttr_instance_s"], h)
    # Block-bootstrap sensitivity if either arm triggered serial dependence
    if summary["serial_dependence"][c0]["triggered_block_boot"] or summary["serial_dependence"][c1]["triggered_block_boot"]:
        bb_lo, bb_hi = moving_block_boot_diff(cells[c0]["ttr_instance_s"].values, cells[c1]["ttr_instance_s"].values)
        r["block_boot_hl_ci"] = [bb_lo, bb_hi]
    res_list.append(r)

# Holm-Bonferroni adjustment
order = sorted(range(len(res_list)), key=lambda i: res_list[i]["p_mwu"])
running_max = 0.0
for rank, idx in enumerate(order):
    adj = min(1.0, res_list[idx]["p_mwu"] * (len(res_list) - rank))
    running_max = max(running_max, adj)
    res_list[idx]["p_holm"] = running_max

for r in res_list:
    summary["confirmatory"][r["label"]] = r
    print(f"  {r['label']:4s}: U={r['u_stat']:.1f}, p_MWU={r['p_mwu']:.3e}, p_Holm={r['p_holm']:.3e} | HL={r['hl_shift_s']:.2f}s (95%CI [{r['hl_ci_lo']:.2f},{r['hl_ci_hi']:.2f}]) | Cliff's d={r['cliffs_delta']:.3f}")
    print(f"        Welch t={r['t_welch']:.2f} (p={r['p_welch']:.3e}), Hedges g={r['hedges_g']:.2f} | Mean Diff={r['mean_diff_s']:.2f}s | Rel Red={r['rel_reduction_pct']:.2f}% (95%CI [{r['rel_ci_lo']:.2f}%, {r['rel_ci_hi']:.2f}%]) | Obj4(>20% CI_lo)={r['rel_ci_lo']>20.0}")
    if "block_boot_hl_ci" in r:
        print(f"        Moving Block-Bootstrap HL 95% CI: [{r['block_boot_hl_ci'][0]:.2f}, {r['block_boot_hl_ci'][1]:.2f}]")

print("\n=== 5. SECONDARY ENDPOINTS (TTI & TTR_service) ===")
for h, c0, c1 in pairs:
    summary["secondary"][h] = {}
    for m in ["tti_es_s", "ttr_service_s"]:
        r = compare_arms(cells[c0][m], cells[c1][m], f"{h}_{m}")
        summary["secondary"][h][m] = r
        print(f"  {h:4s} {m:14s}: HL={r['hl_shift_s']:.2f}s (95%CI [{r['hl_ci_lo']:.2f},{r['hl_ci_hi']:.2f}]), p_MWU={r['p_mwu']:.3e}, RelRed={r['rel_reduction_pct']:.1f}%")

print("\n=== 6. ROBUSTNESS & MANIPULATION CHECKS ===")
# H1 Un-faulted
k_fp = int(unfaulted["false_restarts"].sum())
n_fp = len(unfaulted)
poiss_95_one_sided = float(stats.chi2.ppf(0.95, 2 * (k_fp + 1)) / 2.0 / n_fp)
poiss_95_two_sided = float(stats.chi2.ppf(0.975, 2 * (k_fp + 1)) / 2.0 / n_fp)
summary["checks"]["H1_unfaulted"] = {
    "false_restarts": k_fp, "n_runs": n_fp,
    "exact_poisson_95_upper_one_sided": poiss_95_one_sided,
    "exact_poisson_95_upper_two_sided": poiss_95_two_sided,
    "meets_non_inferiority_0.10": poiss_95_one_sided <= 0.10
}
print(f"  H1_unfaulted: {k_fp}/{n_fp} false restarts | 1-sided 95% exact Poisson upper={poiss_95_one_sided:.4f}/run (<=0.10: {poiss_95_one_sided<=0.10}) | 2-sided 95% upper={poiss_95_two_sided:.4f}/run")

# H1 kill -STOP vs C1_F1 (/hang)
sig_cmp = compare_arms(cells["C1_F1"]["ttr_instance_s"], cells["H1_sigstop"]["ttr_instance_s"], "C1_vs_sigstop")
summary["checks"]["H1_sigstop_vs_hang"] = sig_cmp
print(f"  H1_sigstop vs C1_F1 (/hang) TTR_inst: sigstop Mean={cells['H1_sigstop']['ttr_instance_s'].mean():.2f}s (Med={cells['H1_sigstop']['ttr_instance_s'].median():.2f}s) vs /hang Mean={cells['C1_F1']['ttr_instance_s'].mean():.2f}s | HL={sig_cmp['hl_shift_s']:.2f}s, p_MWU={sig_cmp['p_mwu']:.4f}")

# F2a Dose-Response (Firth penalized logistic regression + Fisher exact at 400Mi & 300Mi)
f2a_tab = f2a.groupby(["cell", "dose_mi"])["oom_killed"].agg(["sum", "count"]).reset_index()
print("  F2a Dose-Response OOM counts:")
for _, row in f2a_tab.iterrows():
    print(f"    {row['cell']} @ {int(row['dose_mi']):3d}Mi: {int(row['sum'])}/{int(row['count'])} OOMKilled")
# Fisher's exact at 400Mi
c0_400 = int(f2a[(f2a["cell"] == "C0_F2") & (f2a["dose_mi"] == 400)]["oom_killed"].sum())
c2_400 = int(f2a[(f2a["cell"] == "C2_F2") & (f2a["dose_mi"] == 400)]["oom_killed"].sum())
_, p_fish_400 = stats.fisher_exact([[c0_400, 10 - c0_400], [c2_400, 10 - c2_400]])
# Firth logit: oom ~ 1 + is_C2 + dose_100mi
y_f2a = f2a["oom_killed"].astype(float).values
arm_c2 = (f2a["cell"] == "C2_F2").astype(float).values
dose_100 = f2a["dose_mi"].astype(float).values / 100.0
X_full = np.column_stack([np.ones(len(y_f2a)), arm_c2, dose_100])
X_red = np.column_stack([np.ones(len(y_f2a)), dose_100])
b_full, se_full, pll_full = firth_logit(X_full, y_f2a)
b_red, se_red, pll_red = firth_logit(X_red, y_f2a)
lrt_stat = 2.0 * (pll_full - pll_red)
p_firth_lrt = float(1.0 - stats.chi2.cdf(lrt_stat, df=1))
summary["checks"]["F2a"] = {
    "fisher_400mi_p": float(p_fish_400),
    "firth_beta_arm_C2": float(b_full[1]), "firth_se_arm_C2": float(se_full[1]),
    "firth_or_arm_C2": float(math.exp(b_full[1])),
    "firth_beta_dose_100mi": float(b_full[2]),
    "firth_lrt_stat": float(lrt_stat), "firth_lrt_p": p_firth_lrt
}
print(f"  F2a Fisher's exact @ 400Mi: p={p_fish_400:.3e} | Firth logit (C2 vs C0): beta={b_full[1]:.3f} (SE={se_full[1]:.3f}), OR={math.exp(b_full[1]):.5f}, PLRT chi2={lrt_stat:.2f}, p={p_firth_lrt:.3e}")

# H3 ABAB block stability
print("  H3 ABAB Block Stability (TTR_instance):")
summary["checks"]["H3_blocks"] = {}
for cell in ["C0_F3", "C3_F3"]:
    df = cells[cell]
    summary["checks"]["H3_blocks"][cell] = []
    for b in range(3):
        sub = df.iloc[b * 10:(b + 1) * 10]["ttr_instance_s"]
        summary["checks"]["H3_blocks"][cell].append({"block": b + 1, "mean": float(sub.mean()), "sd": float(sub.std()), "median": float(sub.median())})
        print(f"    {cell} Block {b+1}: Mean={sub.mean():.2f}s (SD={sub.std():.2f}s), Med={sub.median():.2f}s")

with open(os.path.join(RESULTS_DIR, "analysis_summary.json"), "w") as f:
    json.dump(summary, f, indent=2)
print("\nSaved full stats to results/analysis_summary.json")
