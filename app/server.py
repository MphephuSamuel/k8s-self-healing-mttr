import asyncio, json, os, threading, time

POD_NAME = os.environ.get("POD_NAME", "unknown-pod")
POD_UID = os.environ.get("POD_UID", "unknown-uid")
NODE_NAME = os.environ.get("NODE_NAME", "unknown-node")
STARTUP_DELAY_SEC = float(os.environ.get("STARTUP_DELAY_SEC", "2.0"))
WINDOW_FILE = "/fault/window.txt"

READY = False
HUNG = False
MEM_BALLAST = []

def boot_sequence():
    global READY
    time.sleep(STARTUP_DELAY_SEC)
    if os.path.exists(WINDOW_FILE):
        try:
            end_ts, alloc_mb = open(WINDOW_FILE).read().strip().split(",")
            if time.time() < float(end_ts):
                for _ in range(int(alloc_mb)):
                    MEM_BALLAST.append(bytearray(1024 * 1024))
        except Exception:
            pass
    READY = True

async def handle_client(reader, writer):
    global HUNG
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        if not raw:
            writer.close()
            return
        line = raw.split(b"\r\n", 1)[0].decode("utf-8", errors="ignore").split(" ")
        method, path = (line[0], line[1]) if len(line) >= 2 else ("GET", "/")

        if method == "POST" and path == "/hang":
            HUNG = True
            code, payload = 200, {"action": "hung", "pod": POD_NAME}
        elif method == "POST" and path == "/unhang":
            HUNG = False
            code, payload = 200, {"action": "unhung", "pod": POD_NAME}
        elif method == "POST" and path == "/oom_window":
            os.makedirs("/fault", exist_ok=True)
            with open(WINDOW_FILE, "w") as f:
                f.write(f"{time.time() + 60.0},280")
            code, payload = 200, {"action": "oom_window_started", "pod": POD_NAME}
            def spike():
                time.sleep(0.05)
                chunks = []
                while True:
                    chunks.append(bytearray(16 * 1024 * 1024))
            threading.Thread(target=spike, daemon=True).start()
        elif path == "/healthz/startup":
            code, payload = (200, {"startup": True}) if READY else (503, {"startup": False})
        elif HUNG and path in ("/healthz/ready", "/healthz/live", "/api/work", "/"):
            await asyncio.sleep(30)
            code, payload = 503, {"status": "hung"}
        elif path == "/healthz/ready":
            code, payload = (200, {"ready": True}) if READY else (503, {"ready": False})
        elif path == "/healthz/live":
            code, payload = 200, {"live": True}
        elif path in ("/", "/api/work"):
            code, payload = 200, {"status": "ok", "pod": POD_NAME, "node": NODE_NAME}
        else:
            code, payload = 404, {"error": "not_found"}

        body = json.dumps(payload).encode("utf-8")
        status_txt = "OK" if code == 200 else ("Service Unavailable" if code == 503 else "Not Found")
        resp = (
            f"HTTP/1.1 {code} {status_txt}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Pod-Name: {POD_NAME}\r\n"
            f"X-Pod-UID: {POD_UID}\r\n"
            f"X-Node-Name: {NODE_NAME}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def main():
    threading.Thread(target=boot_sequence, daemon=True).start()
    server = await asyncio.start_server(handle_client, "0.0.0.0", 8080, backlog=256)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
