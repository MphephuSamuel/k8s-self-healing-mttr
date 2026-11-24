#!/usr/bin/env bash
docker unpause minikube-m02 2>/dev/null || true
set -euo pipefail
MODE="${1:-}"
if [[ "$MODE" != "default" && "$MODE" != "tuned" ]]; then
  echo "Usage: $0 [default|tuned]"; exit 1
fi

T_START=$(date +%s)
if [[ "$MODE" == "tuned" ]]; then
  GRACE="15s"; PERIOD="2s"; LEASE="16"
else
  GRACE="40s"; PERIOD="5s"; LEASE="40"
fi

echo "=== Switching H3 node timers to $MODE (grace=$GRACE, period=$PERIOD, lease=$LEASE) ==="

# 1. Patch kube-controller-manager static manifest on control-plane node
minikube ssh -n minikube "sudo python3 -c '
p = \"/etc/kubernetes/manifests/kube-controller-manager.yaml\"
lines = [l for l in open(p) if \"--node-monitor-grace-period=\" not in l and \"--node-monitor-period=\" not in l]
out = []
for l in lines:
    out.append(l)
    if \"- kube-controller-manager\" in l:
        out.append(\"    - --node-monitor-grace-period=${GRACE}\\n\")
        out.append(\"    - --node-monitor-period=${PERIOD}\\n\")
open(p, \"w\").writelines(out)
'"

# 2. Patch KubeletConfiguration nodeLeaseDurationSeconds on workers m02 & m03
for NODE in minikube-m02 minikube-m03; do
  minikube ssh -n "$NODE" "sudo python3 -c '
p = \"/var/lib/kubelet/config.yaml\"
lines = [l for l in open(p) if not l.startswith(\"nodeLeaseDurationSeconds:\")]
lines.append(\"nodeLeaseDurationSeconds: ${LEASE}\\n\")
open(p, \"w\").writelines(lines)
' && sudo systemctl restart kubelet"
done

# 3. Wait until controller-manager and all nodes are Ready
sleep 10
kubectl wait --for=condition=Ready pod -l component=kube-controller-manager -n kube-system --timeout=120s
kubectl wait --for=condition=Ready nodes --all --timeout=120s

T_END=$(date +%s)
echo "Switch to $MODE completed in $((T_END - T_START)) seconds."
kubectl get lease -n kube-node-lease minikube-m02 -o custom-columns="NODE:.metadata.name,LEASE_SEC:.spec.leaseDurationSeconds,RENEW_TIME:.spec.renewTime"
