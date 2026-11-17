# -*- coding: utf-8 -*-
"""RVC 实时变声引擎（无界面）

把 realtime_gui_pro.py 里验证过的音频链路抽出来，供网页版 / 命令行复用：
  - 音频设备枚举（sounddevice）
  - 模型扫描 + 索引自动配对 + 模型信息读取
  - RVC 模型加载、CUDA Graph 预热
  - 实时音频流（SOLA 拼接、降噪、音量包络混合）
  - 每块指标：推理耗时、输入/输出电平、显存占用
"""
import collections
import json
import os
import re
import threading
import time
import traceback

import numpy as np
import torch

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
RVC_ROOT = os.environ.get("RVC_ROOT", r"E:\RVC\RVC-WebUI")
if SERVER_DIR not in os.sys.path:
    os.sys.path.insert(0, SERVER_DIR)
if RVC_ROOT not in os.sys.path:
    os.sys.path.insert(0, RVC_ROOT)
NOW_DIR = RVC_ROOT

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ["OMP_NUM_THREADS"] = "4"
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

CONFIG_PATH = os.path.join(NOW_DIR, "configs", "config.json")
MODEL_DIR = os.path.join(NOW_DIR, "assets", "weights")
INDEX_DIRS = [os.path.join(NOW_DIR, "assets", "indices"), os.path.join(NOW_DIR, "logs")]

F0_METHODS = ("pm", "rmvpe", "fcpe")

DEFAULT_PARAMS = {
    "pitch": 12,
    "formant": 0.12,
    "index_rate": 0.60,
    "rms_mix_rate": 0.35,
    "threhold": -55,
    "block_time": 0.24,
    "crossfade_length": 0.06,
    "extra_time": 2.20,
    "f0": "rmvpe",
    "I_noise_reduce": True,
    "O_noise_reduce": False,
}

HOT_PARAMS = ("pitch", "formant", "index_rate", "rms_mix_rate", "threhold")
RESTART_PARAMS = (
    "block_time",
    "crossfade_length",
    "extra_time",
    "f0",
    "I_noise_reduce",
    "O_noise_reduce",
)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def find_index_for(name):
    """按同名 / 包含关系给模型找 .index"""
    candidates = []
    for d in INDEX_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".index"):
                    candidates.append(os.path.join(root, f))
    if not candidates:
        return ""
    stems = {os.path.splitext(os.path.basename(c))[0]: c for c in candidates}
    if name in stems:
        return stems[name]
    low = name.lower()
    for stem, path in stems.items():
        if low in stem.lower() or stem.lower() in low:
            return path
    return ""


def scan_models():
    out = []
    if not os.path.isdir(MODEL_DIR):
        return out
    for f in sorted(os.listdir(MODEL_DIR)):
        if not f.lower().endswith((".pth", ".pt")):
            continue
        name = os.path.splitext(f)[0]
        pth = os.path.join(MODEL_DIR, f)
        out.append(
            {
                "name": name,
                "pth": pth,
                "index": find_index_for(name),
                "size_mb": round(os.path.getsize(pth) / 1024 ** 2, 1),
            }
        )
    return out


def scan_indexes():
    out = []
    for d in INDEX_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".index"):
                    out.append(os.path.join(root, f))
    return sorted(out)


def read_model_info(pth_path):
    """只读元信息（weights_only 严格模式，安全）"""
    try:
        cpt = torch.load(pth_path, map_location="cpu", weights_only=True)
        cfg = cpt.get("config") or []
        return {
            "version": cpt.get("version", "v1"),
            "f0": cpt.get("f0", 1),
            "sr": cfg[-1] if cfg else None,
            "speakers": int(cpt["weight"]["emb_g.weight"].shape[0])
            if "weight" in cpt and "emb_g.weight" in cpt["weight"]
            else 0,
            "ok": True,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def level_pct(samples):
    arr = np.asarray(samples, dtype="float32")
    if arr.size == 0:
        return 0
    rms = float(np.sqrt(np.mean(np.square(arr)) + 1e-12))
    db = 20.0 * np.log10(rms + 1e-9)
    return int(max(0, min(100, (db + 60.0) / 60.0 * 100)))


class Engine:
    """实时变声引擎。线程安全：start/stop 由服务端串行调用"""

    def __init__(self):
        import sounddevice as sd
        from configs.config import Config

        self.sd = sd
        self.config = Config()
        self.stream = None
        self.mon_stream = None
        self.mon_device = ""
        self.mon_gain = 1.0
        self.mon_enabled = False
        self.mon_queue = collections.deque()
        self.mon_leftover = None
        self.mon_lock = threading.Lock()
        self.mon_frames = 1024
        self.mon_channels = 2
        self.mon_over = 0
        self.running = False
        self.function = "vc"  # vc | im
        self.mode = "vc"
        self.status = "idle"
        self.status_text = "未启动"
        self.last_error = ""
        self.metrics = {"infer_ms": 0.0, "vu_in": 0, "vu_out": 0, "callbacks": 0}
        self.loaded = None  # 当前模型信息
        self.hostapis = []
        self.device_map = {}
        self.settings = self._load_settings()
        self.refresh_devices()
        self._resolve_defaults()

    def _resolve_defaults(self):
        s = self.settings
        hostapi = self.pick_hostapi(s.get("hostapi", ""))
        s["hostapi"] = hostapi
        ins = self.device_map.get(hostapi, {}).get("inputs", [])
        outs = self.device_map.get(hostapi, {}).get("outputs", [])
        if s.get("input_device") not in ins or s.get("output_device") not in outs:
            i, o = self._default_devices(hostapi)
            s["input_device"], s["output_device"] = i, o
        if not s.get("model"):
            models = scan_models()
            prefer = None
            for m in models:
                if "tsuruji" in m["name"].lower():
                    prefer = m
                    break
            if prefer is None and models:
                prefer = models[0]
            if prefer:
                s["model"] = prefer["name"]
                s["index_path"] = prefer["index"]
        if not s.get("monitor_device"):
            outs = [
                d
                for d in self.device_map.get(hostapi, {}).get("outputs", [])
                if d != s.get("output_device")
            ]
            pref = ""
            for d in outs:
                low = d.lower()
                if "耳机" in d or "headphone" in low:
                    pref = d
                    break
            s["monitor_device"] = pref or (outs[0] if outs else "")

    # ------------------------------------------------------------------
    # 设备
    # ------------------------------------------------------------------
    def refresh_devices(self):
        sd = self.sd
        sd._terminate()
        sd._initialize()
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        for h in hostapis:
            for idx in h["devices"]:
                devices[idx]["hostapi_name"] = h["name"]
        self.hostapis = [h["name"] for h in hostapis]
        self.device_map = {h: {"inputs": [], "outputs": []} for h in self.hostapis}
        self.dev_indices = {h: {"inputs": [], "outputs": []} for h in self.hostapis}
        for d in devices:
            h = d["hostapi_name"]
            if h not in self.device_map:
                continue
            if d["max_input_channels"] > 0:
                self.device_map[h]["inputs"].append(d["name"])
                self.dev_indices[h]["inputs"].append(d["index"])
            if d["max_output_channels"] > 0:
                self.device_map[h]["outputs"].append(d["name"])
                self.dev_indices[h]["outputs"].append(d["index"])
        return self.device_map

    def _default_devices(self, hostapi):
        try:
            in_idx, out_idx = self.sd.default.device
            ins = self.device_map[hostapi]["inputs"]
            outs = self.device_map[hostapi]["outputs"]
            iidx = self.dev_indices[hostapi]["inputs"]
            oidx = self.dev_indices[hostapi]["outputs"]
            in_dev = ins[iidx.index(in_idx)] if in_idx in iidx else (ins[0] if ins else "")
            out_dev = outs[oidx.index(out_idx)] if out_idx in oidx else (outs[0] if outs else "")
            return in_dev, out_dev
        except Exception:
            ins = self.device_map.get(hostapi, {}).get("inputs", [])
            outs = self.device_map.get(hostapi, {}).get("outputs", [])
            return (ins[0] if ins else ""), (outs[0] if outs else "")

    def pick_hostapi(self, saved=""):
        if saved in self.hostapis:
            return saved
        if "Windows WASAPI" in self.hostapis:
            return "Windows WASAPI"
        return self.hostapis[0] if self.hostapis else ""

    def match_voicemeeter(self):
        """自动匹配虚拟声卡，返回 (hostapi, in_dev, out_dev) 或 None

        - Voicemeeter：RVC 输入取 VoiceMeeter Output，输出取 VoiceMeeter Aux Input
        - VB-CABLE：RVC 输入用真实麦克风，输出写进 CABLE Input
          （QQ/游戏那边再从 CABLE Output 这个"假麦克风"取声）
        """
        host_order = sorted(self.device_map.keys(), key=lambda h: 0 if "WASAPI" in h else 1)

        # 1) Voicemeeter
        vm_in = ("voicemeeter output", "voicemeeter aux output", "voicemeeter vaio3 output")
        vm_out = ("voicemeeter aux input", "voicemeeter input", "voicemeeter vaio3 input")
        for h in host_order:
            devs = self.device_map[h]
            for d in devs["inputs"]:
                if any(w in d.lower() for w in vm_in):
                    for od in devs["outputs"]:
                        if any(w in od.lower() for w in vm_out):
                            return h, d, od

        # 2) VB-CABLE
        for h in host_order:
            devs = self.device_map[h]
            out_dev = ""
            for od in devs["outputs"]:
                if "cable input" in od.lower():
                    out_dev = od
                    break
            if not out_dev:
                continue
            mic = ""
            for d in devs["inputs"]:
                low = d.lower()
                if "cable output" in low or "stereo mix" in low or "立体声混音" in low:
                    continue
                mic = d
                break
            if mic:
                return h, mic, out_dev
        return None

    # ------------------------------------------------------------------
    # 设置持久化
    # ------------------------------------------------------------------
    def _load_settings(self):
        data = read_json(CONFIG_PATH, {}) or {}
        params = dict(DEFAULT_PARAMS)
        for k in DEFAULT_PARAMS:
            if k in data:
                params[k] = data[k]
        out = {
            "model": data.get("model", ""),
            "index_path": data.get("index_path", ""),
            "hostapi": data.get("sg_hostapi", ""),
            "input_device": data.get("sg_input_device", ""),
            "output_device": data.get("sg_output_device", ""),
            "sr_type": data.get("sr_type", "device"),
            "monitor_device": data.get("monitor_device", ""),
            "monitor_enabled": bool(data.get("monitor_enabled", False)),
            "monitor_gain": float(data.get("monitor_gain", 1.0) or 1.0),
            "params": params,
        }
        return out

    def save_settings(self):
        data = dict(self.settings["params"])
        data.update(
            {
                "model": self.settings["model"],
                "index_path": self.settings["index_path"],
                "sg_hostapi": self.settings["hostapi"],
                "sg_input_device": self.settings["input_device"],
                "sg_output_device": self.settings["output_device"],
                "sr_type": self.settings["sr_type"],
                "monitor_device": self.settings.get("monitor_device", ""),
                "monitor_enabled": bool(self.settings.get("monitor_enabled", False)),
                "monitor_gain": float(self.settings.get("monitor_gain", 1.0) or 1.0),
            }
        )
        try:
            write_json(CONFIG_PATH, data)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def snapshot(self):
        vram = 0.0
        try:
            if torch.cuda.is_available():
                vram = torch.cuda.memory_allocated() / 1024 ** 2
        except Exception:
            pass
        return {
            "running": self.running,
            "status": self.status,
            "status_text": self.status_text,
            "error": self.last_error,
            "infer_ms": round(self.metrics.get("infer_ms", 0.0), 1),
            "latency_ms": int(round(self.delay_time * 1000)) if getattr(self, "delay_time", 0) else 0,
            "vu_in": self.metrics.get("vu_in", 0),
            "vu_out": self.metrics.get("vu_out", 0),
            "vram_mb": round(vram),
            "callbacks": self.metrics.get("callbacks", 0),
            "samplerate": getattr(self, "stream_sr", 0),
            "loaded": self.loaded,
            "monitor": {
                "enabled": self.mon_stream is not None,
                "device": self.mon_device,
                "gain": round(self.mon_gain, 2),
                "queued": len(self.mon_queue),
                "underruns": self.mon_over,
            },
        }

    def _set(self, status, text):
        self.status = status
        self.status_text = text

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start(
        self,
        model_name,
        index_path,
        hostapi,
        input_device,
        output_device,
        sr_type,
        params,
        mode="vc",
        monitor_device="",
        monitor_gain=1.0,
    ):
        if self.running:
            self.stop()
        self.last_error = ""
        self.function = mode if mode in ("vc", "im") else "vc"
        self.mode = self.function

        model = None
        for m in scan_models():
            if m["name"] == model_name:
                model = m
                break
        if model is None:
            raise RuntimeError("找不到模型：%s" % model_name)
        if re.search(r"[^\x00-\x7F]", model["pth"]):
            raise RuntimeError("模型路径里有中文，程序不支持")

        params = dict(DEFAULT_PARAMS, **(params or {}))
        self.params = params
        self.settings.update(
            {
                "model": model_name,
                "index_path": index_path or "",
                "hostapi": hostapi,
                "input_device": input_device,
                "output_device": output_device,
                "sr_type": sr_type,
                "params": params,
            }
        )

        self._set("starting", "正在加载模型…")
        try:
            self._start_model(model, index_path, hostapi, input_device, output_device, sr_type, params)
        except Exception as exc:
            msg = str(exc).lower()
            if "sample rate" in msg and sr_type == "model":
                self._set("starting", "采样率不兼容，正在改用设备采样率…")
                self._start_model(
                    model, index_path, hostapi, input_device, output_device, "device", params
                )
                self.settings["sr_type"] = "device"
            else:
                err = traceback.format_exc()
                self.last_error = err[-1200:]
                self._set("error", "启动失败")
                self.stop()
                raise
        # 耳机监听（失败不影响变声）
        try:
            if monitor_device or monitor_gain:
                self.set_monitor(
                    True if monitor_device else self.settings.get("monitor_enabled", False),
                    monitor_device or self.settings.get("monitor_device", ""),
                    monitor_gain,
                )
        except Exception:
            self.last_error = "监听设备打开失败（不影响变声）：" + traceback.format_exc()[-300:]

        self.save_settings()
        self._set("running", "运行中（%s）" % ("变声" if self.function == "vc" else "监听"))
        return self.snapshot()

    def _start_model(self, model, index_path, hostapi, input_device, output_device, sr_type, params):
        import torchaudio.transforms as tat
        from infer import rtrvc as rvc_for_realtime
        from tools.cuda_graph import run_cuda_graph
        from tools.torchgate import TorchGate

        if hostapi not in self.device_map:
            raise RuntimeError("设备类型不存在：%s" % hostapi)
        ins = self.device_map[hostapi]["inputs"]
        outs = self.device_map[hostapi]["outputs"]
        if input_device not in ins:
            input_device = ins[0] if ins else ""
        if output_device not in outs:
            output_device = outs[0] if outs else ""
        self.sd.default.device[0] = self.dev_indices[hostapi]["inputs"][ins.index(input_device)]
        self.sd.default.device[1] = self.dev_indices[hostapi]["outputs"][outs.index(output_device)]

        torch.cuda.empty_cache()
        self.rvc = rvc_for_realtime.RVC(
            int(params["pitch"]),
            float(params["formant"]),
            model["pth"],
            index_path or "",
            float(params["index_rate"]),
            self.config,
            getattr(self, "rvc", None),
        )
        self.loaded = {
            "name": model["name"],
            "version": getattr(self.rvc, "version", "v1"),
            "sr": self.rvc.tgt_sr,
            "has_index": bool(index_path),
        }
        samplerate = (
            self.rvc.tgt_sr if sr_type == "model" else self._device_samplerate()
        )
        self.stream_sr = samplerate
        channels = self._device_channels()
        self._channels = channels

        self.zc = samplerate // 100
        self.block_frame = int(round(params["block_time"] * samplerate / self.zc)) * self.zc
        self.block_frame_16k = 160 * self.block_frame // self.zc
        self.crossfade_frame = int(round(params["crossfade_length"] * samplerate / self.zc)) * self.zc
        self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
        self.sola_search_frame = self.zc
        self.extra_frame = int(round(params["extra_time"] * samplerate / self.zc)) * self.zc
        dev = self.config.device
        self.input_wav = torch.zeros(
            self.extra_frame + self.crossfade_frame + self.sola_search_frame + self.block_frame,
            device=dev,
            dtype=torch.float32,
        )
        self.input_wav_denoise = self.input_wav.clone()
        self.input_wav_res = torch.zeros(
            160 * self.input_wav.shape[0] // self.zc, device=dev, dtype=torch.float32
        )
        self.rms_buffer = np.zeros(4 * self.zc, dtype="float32")
        self.sola_buffer = torch.zeros(self.sola_buffer_frame, device=dev, dtype=torch.float32)
        self.sola_den_kernel = torch.ones(
            1, 1, self.sola_buffer_frame, device=dev, dtype=torch.float32
        )
        self.nr_buffer = self.sola_buffer.clone()
        self.output_buffer = self.input_wav.clone()
        self.skip_head = self.extra_frame // self.zc
        self.return_length = (
            self.block_frame + self.sola_buffer_frame + self.sola_search_frame
        ) // self.zc
        self.fade_in_window = (
            torch.sin(
                0.5
                * np.pi
                * torch.linspace(
                    0.0, 1.0, steps=self.sola_buffer_frame, device=dev, dtype=torch.float32
                )
            )
            ** 2
        )
        self.fade_out_window = 1 - self.fade_in_window
        self.resampler = tat.Resample(
            orig_freq=samplerate, new_freq=16000, dtype=torch.float32
        ).to(dev)
        if self.rvc.tgt_sr != samplerate:
            self.resampler2 = tat.Resample(
                orig_freq=self.rvc.tgt_sr, new_freq=samplerate, dtype=torch.float32
            ).to(dev)
        else:
            self.resampler2 = None
        self.tg = TorchGate(sr=samplerate, n_fft=4 * self.zc, prop_decrease=0.9).to(dev)
        self._run_cuda_graph = run_cuda_graph
        self._prewarm()
        self._open_stream(samplerate, channels)

    def _prewarm(self):
        from tools.cuda_graph import cuda_graph_enabled, run_cuda_graph

        if not cuda_graph_enabled(self.config.device):
            return
        try:
            dev = self.config.device
            samples = self.input_wav_res.shape[0]
            phase = torch.arange(samples, device=dev, dtype=torch.float32)
            self.input_wav_res.copy_(0.05 * torch.sin(2 * np.pi * 220.0 * phase / 16000.0))
            if self.params["I_noise_reduce"]:
                short = self.input_wav[-self.sola_buffer_frame - self.block_frame :].unsqueeze(0)
                self.tg(short, self.input_wav.unsqueeze(0))
            rcg = self._run_cuda_graph
            rcg(
                self.resampler,
                "realtime-input-resample",
                lambda a: self.resampler(a),
                self.input_wav[-self.block_frame - 2 * self.zc :],
            )
            inferred = self.rvc.infer(
                self.input_wav_res,
                self.block_frame_16k,
                self.skip_head,
                self.return_length,
                self.params["f0"],
            )
            if self.resampler2 is not None:
                inferred = rcg(
                    self.resampler2,
                    "realtime-output-resample",
                    lambda a: self.resampler2(a),
                    inferred,
                )
            if self.params["O_noise_reduce"]:
                self.tg(inferred.unsqueeze(0), self.output_buffer.unsqueeze(0))
            torch.cuda.synchronize(dev)
        except Exception:
            self.last_error = "CUDA Graph 预热失败（不影响使用）\n" + traceback.format_exc()[-600:]
        finally:
            try:
                for t in (
                    self.input_wav,
                    self.input_wav_denoise,
                    self.input_wav_res,
                    self.output_buffer,
                    self.sola_buffer,
                    self.nr_buffer,
                ):
                    t.zero_()
                self.rvc.cache_pitch.zero_()
                self.rvc.cache_pitchf.zero_()
            except Exception:
                pass

    def _open_stream(self, samplerate, channels):
        stream = None
        extra = None
        try:
            stream = self.sd.Stream(
                callback=self.audio_callback,
                blocksize=self.block_frame,
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                extra_settings=extra,
            )
            stream.start()
        except Exception:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            self.running = False
            self.stream = None
            raise
        self.stream = stream
        self.running = True
        self.metrics["callbacks"] = 0
        # 显示"算法延迟"：采样长度 + 淡入淡出（PortAudio 报的 WASAPI 缓冲值虚高，不用）
        try:
            self.delay_time = (
                self.params["block_time"] + self.params["crossfade_length"]
            )
            if self.params["I_noise_reduce"]:
                self.delay_time += min(self.params["crossfade_length"], 0.04)
        except Exception:
            self.delay_time = 0

    def stop(self):
        self.running = False
        self.close_monitor()
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.metrics["vu_in"] = 0
        self.metrics["vu_out"] = 0
        self.metrics["infer_ms"] = 0.0
        self.delay_time = 0
        self._set("idle", "已停止")

    def _device_samplerate(self):
        return int(self.sd.query_devices(device=self.sd.default.device[0])["default_samplerate"])

    def _device_channels(self):
        i = self.sd.query_devices(device=self.sd.default.device[0])["max_input_channels"]
        o = self.sd.query_devices(device=self.sd.default.device[1])["max_output_channels"]
        return min(i, o, 2)

    # ------------------------------------------------------------------
    # 热参数
    # ------------------------------------------------------------------
    def update_hot(self, params):
        if not self.running:
            self.settings["params"].update(params)
            return
        for k in HOT_PARAMS:
            if k not in params:
                continue
            v = params[k]
            self.params[k] = v
            self.settings["params"][k] = v
            if k == "pitch" and hasattr(self, "rvc"):
                self.rvc.change_key(int(v))
            elif k == "formant" and hasattr(self, "rvc"):
                self.rvc.change_formant(float(v))
            elif k == "index_rate" and hasattr(self, "rvc"):
                self.rvc.change_index_rate(float(v))
        self.save_settings()

    # ------------------------------------------------------------------
    # 音频回调
    # ------------------------------------------------------------------
    def audio_callback(self, indata, outdata, frames, times, status):
        import librosa
        import torch.nn.functional as F

        try:
            self._process(indata, outdata)
        except Exception:
            self.last_error = traceback.format_exc()[-900:]
            outdata[:] = 0
            raise

    def _process(self, indata, outdata):
        import librosa
        import torch
        import torch.nn.functional as F

        p = self.params
        zc = self.zc
        start_time = time.perf_counter()
        indata = librosa.to_mono(indata.T)
        if p["threhold"] > -60:
            indata = np.append(self.rms_buffer, indata)
            rms = librosa.feature.rms(y=indata, frame_length=4 * zc, hop_length=zc)[:, 2:]
            self.rms_buffer[:] = indata[-4 * zc :]
            indata = indata[2 * zc - zc // 2 :]
            db_th = librosa.amplitude_to_db(rms, ref=1.0)[0] < p["threhold"]
            for i in range(db_th.shape[0]):
                if db_th[i]:
                    indata[i * zc : (i + 1) * zc] = 0
            indata = indata[zc // 2 :]

        dev = self.config.device
        self.input_wav[: -self.block_frame] = self.input_wav[self.block_frame :].clone()
        self.input_wav[-indata.shape[0] :] = torch.from_numpy(indata).to(dev)
        self.input_wav_res[: -self.block_frame_16k] = self.input_wav_res[
            self.block_frame_16k :
        ].clone()
        rcg = self._run_cuda_graph

        if p["I_noise_reduce"]:
            self.input_wav_denoise[: -self.block_frame] = self.input_wav_denoise[
                self.block_frame :
            ].clone()
            input_wav = self.input_wav[-self.sola_buffer_frame - self.block_frame :]
            input_wav = self.tg(input_wav.unsqueeze(0), self.input_wav.unsqueeze(0)).squeeze(0)
            input_wav[: self.sola_buffer_frame] *= self.fade_in_window
            input_wav[: self.sola_buffer_frame] += self.nr_buffer * self.fade_out_window
            self.input_wav_denoise[-self.block_frame :] = input_wav[: self.block_frame]
            self.nr_buffer[:] = input_wav[self.block_frame :]
            self.input_wav_res[-self.block_frame_16k - 160 :] = rcg(
                self.resampler,
                "realtime-input-resample",
                lambda a: self.resampler(a),
                self.input_wav_denoise[-self.block_frame - 2 * zc :],
            )[160:]
        else:
            self.input_wav_res[-160 * (indata.shape[0] // zc + 1) :] = rcg(
                self.resampler,
                "realtime-input-resample",
                lambda a: self.resampler(a),
                self.input_wav[-indata.shape[0] - 2 * zc :],
            )[160:]

        if self.function == "vc":
            infer_wav = self.rvc.infer(
                self.input_wav_res,
                self.block_frame_16k,
                self.skip_head,
                self.return_length,
                p["f0"],
            )
            if self.resampler2 is not None:
                infer_wav = rcg(
                    self.resampler2,
                    "realtime-output-resample",
                    lambda a: self.resampler2(a),
                    infer_wav,
                )
        elif p["I_noise_reduce"]:
            infer_wav = self.input_wav_denoise[self.extra_frame :].clone()
        else:
            infer_wav = self.input_wav[self.extra_frame :].clone()

        if p["O_noise_reduce"] and self.function == "vc":
            self.output_buffer[: -self.block_frame] = self.output_buffer[
                self.block_frame :
            ].clone()
            self.output_buffer[-self.block_frame :] = infer_wav[-self.block_frame :]
            infer_wav = self.tg(infer_wav.unsqueeze(0), self.output_buffer.unsqueeze(0)).squeeze(0)

        if p["rms_mix_rate"] < 1 and self.function == "vc":
            input_wav = (
                self.input_wav_denoise[self.extra_frame :]
                if p["I_noise_reduce"]
                else self.input_wav[self.extra_frame :]
            )
            rms1 = librosa.feature.rms(
                y=input_wav[: infer_wav.shape[0]].cpu().numpy(),
                frame_length=4 * zc,
                hop_length=zc,
            )
            rms1 = torch.from_numpy(rms1).to(dev)
            rms1 = F.interpolate(
                rms1.unsqueeze(0), size=infer_wav.shape[0] + 1, mode="linear", align_corners=True
            )[0, 0, :-1]
            rms2 = librosa.feature.rms(
                y=infer_wav[:].cpu().numpy(), frame_length=4 * zc, hop_length=zc
            )
            rms2 = torch.from_numpy(rms2).to(dev)
            rms2 = F.interpolate(
                rms2.unsqueeze(0), size=infer_wav.shape[0] + 1, mode="linear", align_corners=True
            )[0, 0, :-1]
            rms2 = torch.max(rms2, torch.zeros_like(rms2) + 1e-3)
            infer_wav *= torch.pow(rms1 / rms2, 1.0 - p["rms_mix_rate"])

        conv_input = infer_wav[None, None, : self.sola_buffer_frame + self.sola_search_frame]
        cor_nom = F.conv1d(conv_input, self.sola_buffer[None, None, :])
        cor_den = torch.sqrt(F.conv1d(conv_input ** 2, self.sola_den_kernel) + 1e-8)
        sola_offset = torch.argmax(cor_nom[0, 0] / cor_den[0, 0]).item()
        infer_wav = infer_wav[sola_offset:]
        infer_wav[: self.sola_buffer_frame] *= self.fade_in_window
        infer_wav[: self.sola_buffer_frame] += self.sola_buffer * self.fade_out_window
        self.sola_buffer[:] = infer_wav[
            self.block_frame : self.block_frame + self.sola_buffer_frame
        ]
        outdata[:] = (
            infer_wav[: self.block_frame].repeat(getattr(self, '_channels', 1), 1).t().cpu().numpy()
        )

        if self.mon_stream is not None:
            self._mon_push(outdata)
        self.metrics["infer_ms"] = (time.perf_counter() - start_time) * 1000.0
        self.metrics["vu_in"] = level_pct(indata)
        self.metrics["vu_out"] = level_pct(outdata)
        self.metrics["callbacks"] = self.metrics.get("callbacks", 0) + 1

    # ------------------------------------------------------------------
    # 耳机监听：把变声后的声音同时送一份到监听设备
    # ------------------------------------------------------------------
    def _out_index(self, device_name):
        for h, devs in self.device_map.items():
            outs = devs["outputs"]
            if device_name in outs:
                return h, self.dev_indices[h]["outputs"][outs.index(device_name)]
        return None, None

    def _open_monitor(self, device_name, gain=1.0):
        self.close_monitor()
        if not device_name:
            return False
        if device_name == self.settings.get("output_device"):
            self.last_error = "监听设备不能和变声输出设备相同（会啸叫），已跳过"
            return False
        _h, idx = self._out_index(device_name)
        if idx is None:
            self.last_error = "找不到监听设备：" + str(device_name)
            return False
        try:
            max_ch = int(self.sd.query_devices(idx)["max_output_channels"])
        except Exception:
            max_ch = 2
        ch = max(1, min(max_ch, 2))
        self.mon_queue.clear()
        self.mon_leftover = None
        self.mon_gain = float(gain)
        self.mon_channels = ch
        stream = None
        try:
            stream = self.sd.OutputStream(
                device=idx,
                channels=ch,
                samplerate=self.stream_sr,
                dtype="float32",
                blocksize=self.mon_frames,
                callback=self._mon_cb,
            )
            stream.start()
        except Exception:
            self.last_error = "打开监听设备失败：" + traceback.format_exc()[-300:]
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            return False
        self.mon_stream = stream
        self.mon_device = device_name
        return True

    def close_monitor(self):
        if self.mon_stream is not None:
            try:
                self.mon_stream.abort()
                self.mon_stream.close()
            except Exception:
                pass
        self.mon_stream = None
        self.mon_device = ""
        self.mon_queue.clear()
        self.mon_leftover = None

    def set_monitor(self, enabled, device=None, gain=None):
        # 开关耳机监听。未运行变声时只记设置，下次启动生效
        if gain is not None:
            self.mon_gain = max(0.0, min(2.0, float(gain)))
        if device:
            self.settings["monitor_device"] = device
        dev = device or self.settings.get("monitor_device", "")
        self.settings["monitor_gain"] = self.mon_gain
        if enabled and self.running:
            ok = self._open_monitor(dev, self.mon_gain)
            self.settings["monitor_enabled"] = bool(ok)
            self.settings["monitor_device"] = self.mon_device or dev
            self.save_settings()
            return {"enabled": bool(ok), "device": self.mon_device, "error": self.last_error}
        if enabled:
            self.settings["monitor_enabled"] = True
            self.save_settings()
            return {"enabled": False, "device": dev, "note": "变声还没开始，已记住设置"}
        self.close_monitor()
        self.settings["monitor_enabled"] = False
        self.save_settings()
        return {"enabled": False, "device": dev}

    def _mon_push(self, block):
        if self.mon_stream is None:
            return
        try:
            arr = np.asarray(block, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr[:, None]
            if arr.shape[1] != self.mon_channels:
                arr = np.repeat(arr[:, :1], self.mon_channels, axis=1)
            with self.mon_lock:
                self.mon_queue.append(arr.copy())
                while len(self.mon_queue) > 6:
                    self.mon_queue.popleft()
                    self.mon_over += 1
        except Exception:
            pass

    def _mon_read(self, frames):
        parts = []
        need = frames
        with self.mon_lock:
            if self.mon_leftover is not None and len(self.mon_leftover):
                parts.append(self.mon_leftover)
                need -= len(self.mon_leftover)
            while need > 0 and self.mon_queue:
                blk = self.mon_queue.popleft()
                parts.append(blk)
                need -= len(blk)
            if need > 0:
                self.mon_over += 1
                parts.append(np.zeros((need, self.mon_channels), dtype=np.float32))
            data = np.concatenate(parts, axis=0)
            self.mon_leftover = data[frames:] if len(data) > frames else None
            used = data[:frames]
        return used

    def _mon_cb(self, outdata, frames, tvec, status):
        try:
            data = self._mon_read(frames)
            if data.shape[1] != outdata.shape[1]:
                data = np.repeat(data[:, :1], outdata.shape[1], axis=1)
            outdata[:] = data * self.mon_gain
        except Exception:
            outdata[:] = 0
