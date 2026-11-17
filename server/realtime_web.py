# -*- coding: utf-8 -*-
"""RVC 实时变声器 · 网页版（现代 App 界面）

启动后浏览器打开 http://127.0.0.1:7898
后端复用 rvc_engine.Engine，前端是 E:\\RVC\\RVC-WebUI\\web 下的静态页面。
"""
import asyncio
import json
import os
import sys
import threading
import time
import webbrowser

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SERVER_DIR)
RVC_ROOT = os.environ.get("RVC_ROOT", r"E:\RVC\RVC-WebUI")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)
if RVC_ROOT not in sys.path:
    sys.path.insert(0, RVC_ROOT)
os.chdir(RVC_ROOT)
NOW_DIR = ROOT_DIR

NO_BROWSER = False
if "--no-browser" in sys.argv:
    sys.argv.remove("--no-browser")
    NO_BROWSER = True
if os.environ.get("RVC_WEB_NO_BROWSER") == "1":
    NO_BROWSER = True

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rvc_engine import (
    DEFAULT_PARAMS,
    MODEL_DIR,
    Engine,
    read_model_info,
    scan_indexes,
    scan_models,
)

WEB_DIR = os.path.join(NOW_DIR, "web")
PORT = int(os.environ.get("RVC_WEB_PORT", "7898"))

# 场景预设：键 = 显示名，值 = 参数
PRESETS = {
    "御姐·日常自然": {
        "pitch": 12, "formant": 0.12, "index_rate": 0.60, "rms_mix_rate": 0.35,
        "threhold": -55, "block_time": 0.24, "crossfade_length": 0.06,
        "extra_time": 2.20, "f0": "rmvpe", "I_noise_reduce": True, "O_noise_reduce": False,
    },
    "御姐·直播（稳亮）": {
        "pitch": 12, "formant": 0.18, "index_rate": 0.72, "rms_mix_rate": 0.45,
        "threhold": -50, "block_time": 0.32, "crossfade_length": 0.09,
        "extra_time": 2.80, "f0": "rmvpe", "I_noise_reduce": True, "O_noise_reduce": True,
    },
    "甜妹（高音）": {
        "pitch": 14, "formant": 0.45, "index_rate": 0.70, "rms_mix_rate": 0.40,
        "threhold": -55, "block_time": 0.24, "crossfade_length": 0.06,
        "extra_time": 2.20, "f0": "rmvpe", "I_noise_reduce": True, "O_noise_reduce": False,
    },
    "游戏语音（低延迟）": {
        "pitch": 12, "formant": 0.1, "index_rate": 0.50, "rms_mix_rate": 0.25,
        "threhold": -60, "block_time": 0.20, "crossfade_length": 0.05,
        "extra_time": 1.50, "f0": "rmvpe", "I_noise_reduce": True, "O_noise_reduce": False,
    },
    "唱歌": {
        "pitch": 12, "formant": 0.1, "index_rate": 0.80, "rms_mix_rate": 0.60,
        "threhold": -60, "block_time": 0.50, "crossfade_length": 0.12,
        "extra_time": 4.00, "f0": "rmvpe", "I_noise_reduce": False, "O_noise_reduce": False,
    },
    "极低延迟": {
        "pitch": 12, "formant": 0.1, "index_rate": 0.40, "rms_mix_rate": 0.20,
        "threhold": -60, "block_time": 0.12, "crossfade_length": 0.03,
        "extra_time": 1.00, "f0": "rmvpe", "I_noise_reduce": False, "O_noise_reduce": False,
    },
}

app = FastAPI(title="RVC 变声台")
engine = Engine()
_lock = threading.Lock()


class MainThreadDispatcher:
    """PortAudio / WASAPI 必须在创建引擎的线程里调用，否则报 WDM-KS 错误"""

    def __init__(self):
        import queue

        self.q = queue.Queue()
        self.ready = threading.Event()

    def call(self, fn, *args, **kwargs):
        box = {"event": threading.Event(), "result": None, "error": None}
        self.q.put((fn, args, kwargs, box))
        box["event"].wait()
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    def loop(self):
        self.ready.set()
        while True:
            fn, args, kwargs, box = self.q.get()
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            finally:
                box["event"].set()


dispatcher = MainThreadDispatcher()

WEB_DIR = ROOT_DIR


def ok(**kw):
    d = {"ok": True}
    d.update(kw)
    return JSONResponse(d)


def err(msg, code=400):
    return JSONResponse({"ok": False, "error": str(msg)}, status_code=code)


@app.get("/")
def index():
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(path):
        return JSONResponse({"error": "web/index.html 缺失"}, status_code=500)
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/bootstrap")
def bootstrap():
    gpu, vram = "", 0
    try:
        import torch

        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
            vram = round(torch.cuda.get_device_properties(0).total_memory / 1024 ** 3, 1)
    except Exception:
        pass
    return {
        "ok": True,
        "hostapis": engine.hostapis,
        "devices": engine.device_map,
        "models": scan_models(),
        "indexes": scan_indexes(),
        "settings": engine.settings,
        "presets": PRESETS,
        "default_params": DEFAULT_PARAMS,
        "engine": {
            "device": engine.config.device,
            "half": bool(engine.config.is_half),
            "gpu": gpu,
            "vram_total": vram,
        },
        "snapshot": engine.snapshot(),
    }


@app.post("/api/start")
async def api_start(payload: dict):
    def work():
        with _lock:
            return dispatcher.call(
                engine.start,
                payload.get("model", ""),
                payload.get("index_path", ""),
                payload.get("hostapi", ""),
                payload.get("input_device", ""),
                payload.get("output_device", ""),
                payload.get("sr_type", "device"),
                payload.get("params") or {},
                payload.get("mode", "vc"),
                payload.get("monitor_device", ""),
                float(payload.get("monitor_gain", 1.0) or 1.0),
            )

    try:
        snap = await asyncio.get_event_loop().run_in_executor(None, work)
        return ok(snapshot=snap)
    except Exception as e:
        return err(str(e) or "启动失败", 500)


@app.post("/api/stop")
async def api_stop():
    with _lock:
        dispatcher.call(engine.stop)
    return ok(snapshot=engine.snapshot())


@app.post("/api/hot")
async def api_hot(payload: dict):
    dispatcher.call(engine.update_hot, payload.get("params") or {})
    return ok()


@app.post("/api/mode")
async def api_mode(payload: dict):
    mode = payload.get("mode", "vc")
    engine.function = mode if mode in ("vc", "im") else "vc"
    engine.mode = engine.function
    if engine.status == "running":
        engine.status_text = "运行中（%s）" % ("变声" if engine.function == "vc" else "监听")
    return ok(mode=engine.function)


@app.post("/api/monitor")
async def api_monitor(payload: dict):
    """开关耳机监听：{enabled, device, gain}"""
    res = dispatcher.call(
        engine.set_monitor,
        bool(payload.get("enabled", False)),
        payload.get("device") or "",
        payload.get("gain"),
    )
    return ok(**res, snapshot=engine.snapshot())


@app.post("/api/reload_devices")
async def api_reload():
    dispatcher.call(engine.refresh_devices)
    return ok(hostapis=engine.hostapis, devices=engine.device_map)


@app.post("/api/voicemeeter")
async def api_voicemeeter():
    hit = engine.match_voicemeeter()
    if not hit:
        return ok(found=False)
    hostapi, in_dev, out_dev = hit
    engine.settings.update(
        {"hostapi": hostapi, "input_device": in_dev, "output_device": out_dev}
    )
    return ok(found=True, hostapi=hostapi, input_device=in_dev, output_device=out_dev)


@app.post("/api/open_models")
async def api_open_models():
    try:
        os.startfile(MODEL_DIR)
        return ok()
    except Exception as e:
        return err(e)


@app.post("/api/model_info")
async def api_model_info(payload: dict):
    name = payload.get("name", "")
    for m in scan_models():
        if m["name"] == name:
            return ok(info=read_model_info(m["pth"]), model=m)
    return err("模型不存在")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    try:
        while True:
            await sock.send_text(json.dumps(engine.snapshot(), ensure_ascii=False))
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


def should_open_browser():
    return not NO_BROWSER


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def open_browser():
    time.sleep(1.5)
    try:
        webbrowser.open("http://127.0.0.1:%d" % PORT)
    except Exception:
        pass


def port_in_use(port):
    import socket

    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


if __name__ == "__main__":
    if port_in_use(PORT):
        print("检测到变声台已经在运行，直接使用现有服务：http://127.0.0.1:%d" % PORT)
        if should_open_browser():
            webbrowser.open("http://127.0.0.1:%d" % PORT)
            time.sleep(1.0)
        sys.exit(0)
    print("=" * 56)
    print(" RVC 实时变声器 · 网页版")
    print(" 地址: http://127.0.0.1:%d" % PORT)
    print(" 关闭窗口或按 Ctrl+C 退出")
    print("=" * 56)
    if should_open_browser():
        threading.Thread(target=open_browser, daemon=True).start()
    else:
        print(" 静默模式：未自动打开浏览器")

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    try:
        dispatcher.loop()
    except KeyboardInterrupt:
        pass
