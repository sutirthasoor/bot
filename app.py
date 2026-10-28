# app.py
import utils.session
import asyncio, datetime as dt, json, threading, queue
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot_core import Rotator

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "rotations.log"

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

state = {
    "status": "idle",
    "current": None,
    "last_run": None,
    "total_rotated": 0,
}
subscribers: list[queue.Queue] = []
loop_thread: threading.Thread | None = None
loop_stop = threading.Event()
rotator: Rotator | None = None


def broadcast(line: str):
    ts = dt.datetime.now().strftime("%H:%M:%S")
    msg = f"[{ts}] {line}"
    print(msg)
    for q in list(subscribers):
        try:
            q.put_nowait(msg)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def set_status(s: str, current=None):
    state["status"] = s
    if current is not None:
        state["current"] = current


class Target(BaseModel):
    name: str
    hours: float
    limit: int = 0
    mark_available: bool = True

class RunOnceReq(BaseModel):
    targets: list[Target]

class StartLoopReq(BaseModel):
    targets: list[Target]
    interval_min: float


@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/status")
async def get_status():
    return state


@app.get("/inventories")
def list_inventories():
    """Open the admin tab, read the inventory table, return the names."""
    r = Rotator(broadcast)

    async def _fetch():
        from playwright.async_api import async_playwright
        from bot_core import CDP_URL
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0]
            admin_page = ctx.pages[0]
            await r._ensure_on_inventory_page(admin_page)
            return await r._list_all_inventories(admin_page)

    try:
        names = asyncio.run(_fetch())
        return {"ok": True, "inventories": names}
    except Exception as e:
        return {"ok": False, "error": str(e), "inventories": []}


@app.post("/run-once")
async def run_once(req: RunOnceReq):
    def worker():
        global rotator
        set_status("running")
        rotator = Rotator(broadcast)
        try:
            payload = [t.model_dump() for t in req.targets]
            total = asyncio.run(rotator.run_once(payload))
            state["total_rotated"] += total
            state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
        finally:
            set_status("idle", current=None)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": "started"}


@app.post("/start-loop")
async def start_loop(req: StartLoopReq):
    global loop_thread, rotator
    if loop_thread and loop_thread.is_alive():
        return {"ok": False, "message": "loop already running"}

    loop_stop.clear()
    payload = [t.model_dump() for t in req.targets]

    def worker():
        global rotator
        set_status("running")
        rotator = Rotator(broadcast)

        async def _loop():
            while not loop_stop.is_set():
                try:
                    await rotator.run_once(payload)
                except Exception as e:
                    broadcast(f"[loop-error] {e}")
                if loop_stop.is_set():
                    break
                broadcast(f"[loop] sleeping {req.interval_min} min")
                slept = 0.0
                while slept < req.interval_min * 60 and not loop_stop.is_set():
                    await asyncio.sleep(1)
                    slept += 1

        try:
            asyncio.run(_loop())
        finally:
            set_status("idle", current=None)

    loop_thread = threading.Thread(target=worker, daemon=True)
    loop_thread.start()
    return {"ok": True, "message": "loop started"}


@app.post("/stop")
async def stop():
    if rotator:
        rotator.stop_flag = True
    loop_stop.set()
    set_status("stopping")
    return {"ok": True}


@app.get("/stream")
async def stream():
    q: queue.Queue = queue.Queue()
    subscribers.append(q)

    async def gen():
        try:
            yield f"data: {json.dumps('[connected]')}\n\n"
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    msg = None
                if msg is not None:
                    yield f"data: {json.dumps(msg)}\n\n"
                await asyncio.sleep(0.2)
        finally:
            try:
                subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")