import asyncio, json
from typing import List, Optional, Literal, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Space Elevator MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

STATE = {
    "position_km": 0.0,
    "speed_ms": 0.0,
    "payload_kg": 0.0,
    "doors": "CLOSED",
    "running": False,
    "target_km": 0.0,
    "cabin": "IDLE",
}
SETPOINT_MS = 20.0
PARCELS: List[Dict[str, Any]] = []
CLIENTS: List[WebSocket] = []
LOCK = asyncio.Lock()

class Command(BaseModel):
    action: Literal[
        "start", "stop", "emergency_stop",
        "set_doors", "set_target", "set_speed",
        "load_from_warehouse"
    ]
    payload: Optional[Dict[str, Any]] = None

class Parcel(BaseModel):
    id: str
    weight_kg: float
    destination_km: float
    status: Literal["QUEUED", "LOADED", "SHIPPED", "DELIVERED"] = "QUEUED"

async def broadcast(msg: Dict[str, Any]):
    dead = []
    text = json.dumps(msg)
    for ws in CLIENTS:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try: CLIENTS.remove(ws)
        except: pass

async def sim_loop():
    global SETPOINT_MS
    TICK = 0.2
    while True:
        async with LOCK:
            moving = (
                STATE["running"] and STATE["doors"] != "OPEN" and
                abs(STATE["target_km"] - STATE["position_km"]) > 0.01
            )
            if moving:
                STATE["cabin"] = "MOVING"
                direction = 1.0 if STATE["target_km"] > STATE["position_km"] else -1.0
                delta_km = (SETPOINT_MS / 1000.0) * TICK
                STATE["position_km"] += direction * delta_km
                STATE["speed_ms"] = direction * SETPOINT_MS
                arrived = (
                    (STATE["target_km"] - STATE["position_km"]) * direction <= 0
                    or abs(STATE["target_km"] - STATE["position_km"]) < 0.005
                )
                if arrived:
                    STATE["position_km"] = STATE["target_km"]
                    STATE["running"] = False
                    STATE["speed_ms"] = 0.0
                    STATE["cabin"] = "IDLE"
            else:
                STATE["speed_ms"] = 0.0
                if not STATE["running"]:
                    STATE["cabin"] = "IDLE"
        await broadcast({"type": "telemetry", "data": STATE})
        await asyncio.sleep(TICK)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(sim_loop())

@app.get("/api/status")
async def get_status():
    return STATE

@app.post("/api/command")
async def send_command(cmd: Command):
    global SETPOINT_MS
    async with LOCK:
        if cmd.action == "start":
            if STATE["doors"] != "OPEN":
                STATE["running"] = True
                STATE["cabin"] = "MOVING"
        elif cmd.action == "stop":
            STATE["running"] = False
            STATE["cabin"] = "IDLE"
        elif cmd.action == "emergency_stop":
            STATE["running"] = False
            STATE["cabin"] = "ERROR"
            asyncio.create_task(_clear_error())
        elif cmd.action == "set_doors":
            want = (cmd.payload or {}).get("state")
            if abs(STATE["speed_ms"]) < 0.1:
                STATE["doors"] = "OPEN" if want == "OPEN" else "CLOSED"
        elif cmd.action == "set_target":
            km = float((cmd.payload or {}).get("km", 0))
            STATE["target_km"] = max(0.0, min(100.0, km))
        elif cmd.action == "set_speed":
            ms = float((cmd.payload or {}).get("ms", SETPOINT_MS))
            SETPOINT_MS = max(0.0, min(200.0, ms))
        elif cmd.action == "load_from_warehouse":
            for p in PARCELS:
                if p["status"] == "QUEUED":
                    p["status"] = "LOADED"
                    STATE["payload_kg"] += float(p["weight_kg"])
                    break
    return {"ok": True}

async def _clear_error():
    await asyncio.sleep(1.5)
    async with LOCK:
        if STATE["cabin"] == "ERROR":
            STATE["cabin"] = "IDLE"

@app.get("/api/warehouse/parcels")
async def list_parcels():
    return PARCELS

@app.post("/api/warehouse/parcel")
async def add_parcel(p: Parcel):
    PARCELS.insert(0, p.model_dump())
    await broadcast({"type": "parcels", "data": PARCELS[:50]})
    return {"ok": True}

@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    await ws.accept()
    CLIENTS.append(ws)
    await ws.send_text(json.dumps({"type": "telemetry", "data": STATE}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        try: CLIENTS.remove(ws)
        except: pass
