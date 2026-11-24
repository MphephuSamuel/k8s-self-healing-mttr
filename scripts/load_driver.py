import csv, signal, sys, time, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

URL = "http://mttr-app-svc/api/work"
RPS = int(sys.argv[1])
DURATION = float(sys.argv[2])
OUT_CSV = sys.argv[3]

stop_flag = False
def handle_term(signum, frame):
    global stop_flag
    stop_flag = True
signal.signal(signal.SIGTERM, handle_term)
signal.signal(signal.SIGINT, handle_term)

f = open(OUT_CSV, "w", newline="", buffering=1)
w = csv.writer(f)
w.writerow(["ts", "status", "latency_ms", "pod_name", "pod_uid"])
lock = threading.Lock()

def fire():
    t_start = time.time()
    code, pod, uid = 0, "", ""
    try:
        req = urllib.request.Request(URL, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            code = resp.status
            pod = resp.headers.get("X-Pod-Name", "")
            uid = resp.headers.get("X-Pod-UID", "")
            resp.read()
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    lat_ms = (time.time() - t_start) * 1000.0
    with lock:
        if not f.closed:
            w.writerow([f"{t_start:.4f}", code, f"{lat_ms:.2f}", pod, uid])

pool = ThreadPoolExecutor(max_workers=min(RPS * 2, 200))
interval = 1.0 / RPS
t_end = time.time() + DURATION
next_t = time.time()

while not stop_flag and time.time() < t_end:
    pool.submit(fire)
    next_t += interval
    sleep_for = next_t - time.time()
    if sleep_for > 0:
        time.sleep(sleep_for)

pool.shutdown(wait=True)
f.flush()
f.close()
