/* ==========================================================================
   变声台 · 前端逻辑
   - 后端：server/realtime_web.py（本地 FastAPI + WebSocket）
   - 无后端时（GitHub Pages 静态预览）自动进入 demo 模式，界面可交互但不发声
   ========================================================================== */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const API = "";            // 与 index.html 同目录：本地由 FastAPI 托管，Pages 上进入 demo
  const HOT_KEYS = ["pitch", "formant", "index_rate", "rms_mix_rate", "threhold"];
  const PERF_KEYS = ["block_time", "crossfade_length", "extra_time"];

  const state = {
    boot: null,
    demo: false,
    params: {},
    running: false,
    busy: false,
    dirty: false,
    mode: "vc",
    monitor: { enabled: false, device: "", gain: 1 },
    lastStatus: "",
    peaks: { in: 0, out: 0 },
  };

  /* ------------------------------------------------------------------ 主题 */
  const THEME_KEY = "rvc-studio-theme";

  function readThemeParam() {
    const q = new URLSearchParams(location.search).get("theme");
    return q === "light" || q === "dark" ? q : null;
  }

  function applyTheme(theme, animate) {
    const html = document.documentElement;
    if (animate) {
      html.classList.add("theme-anim");
      window.setTimeout(() => html.classList.remove("theme-anim"), 380);
    }
    html.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* 隐私模式 */ }
    const meta = $('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#141413" : "#faf9f5");
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
    const forced = readThemeParam();
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(forced || saved || (prefersDark ? "dark" : "light"), false);
  }

  /* --------------------------------------------------------------- 小工具 */
  function toast(message, kind = "") {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = message;
    box.appendChild(el);
    window.setTimeout(() => {
      el.style.transition = "opacity 200ms ease";
      el.style.opacity = "0";
      window.setTimeout(() => el.remove(), 220);
    }, 2800);
  }

  const logRows = [];
  function log(message, kind = "") {
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    logRows.push({ time, message, kind });
    while (logRows.length > 40) logRows.shift();
    renderLog();
  }

  function renderLog() {
    const list = $("#log-list");
    if (!list) return;
    if (!logRows.length) {
      list.innerHTML = '<p class="log-empty">暂无记录。启动后这里会显示状态与错误。</p>';
      return;
    }
    list.innerHTML = logRows
      .map(
        (r) =>
          `<div class="log-line ${r.kind}"><time>${r.time}</time><span>${escapeHtml(r.message)}</span></div>`
      )
      .join("");
    list.scrollTop = list.scrollHeight;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, body) {
    const res = await fetch(API + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
    return data;
  }

  function fmtValue(key, value) {
    const num = Number(value);
    if (key === "pitch") return num > 0 ? `+${num.toFixed(0)}` : num.toFixed(0);
    if (key === "threhold") return `${num.toFixed(0)} dB`;
    if (PERF_KEYS.includes(key)) return `${num.toFixed(2)} s`;
    return num.toFixed(2);
  }

  function setSegment(selector, attr, value) {
    $$(`${selector} button`).forEach((btn) => {
      btn.classList.toggle("on", btn.dataset[attr] === value);
    });
  }

  function segmentValue(selector, attr) {
    const on = $(`${selector} button.on`);
    return on ? on.dataset[attr] : null;
  }

  /* ------------------------------------------------------------ 演示数据 */
  const DEMO = {
    ok: true,
    hostapis: ["Windows WASAPI"],
    devices: {
      "Windows WASAPI": {
        inputs: ["麦克风阵列 (Realtek(R) Audio)"],
        outputs: ["CABLE Input (VB-Audio Virtual Cable)", "耳机 (Realtek(R) Audio)"],
      },
    },
    models: [
      { name: "Tsuruji", index: "Tsuruji.index", size_mb: 52.6 },
      { name: "yujie_yalin", index: "yujie_yalin.index", size_mb: 54.9 },
      { name: "natural_shiyun", index: "natural_shiyun.index", size_mb: 54.9 },
    ],
    indexes: ["Tsuruji.index", "yujie_yalin.index", "natural_shiyun.index"],
    settings: {
      model: "Tsuruji",
      index_path: "Tsuruji.index",
      hostapi: "Windows WASAPI",
      input_device: "麦克风阵列 (Realtek(R) Audio)",
      output_device: "CABLE Input (VB-Audio Virtual Cable)",
      sr_type: "device",
      monitor_device: "耳机 (Realtek(R) Audio)",
      monitor_enabled: false,
      monitor_gain: 1,
      params: {
        pitch: 12, formant: 0.12, index_rate: 0.6, rms_mix_rate: 0.35, threhold: -55,
        block_time: 0.24, crossfade_length: 0.06, extra_time: 2.2,
        f0: "rmvpe", I_noise_reduce: true, O_noise_reduce: false,
      },
    },
    presets: {
      "御姐·日常自然": { pitch: 12, formant: 0.12, index_rate: 0.6, rms_mix_rate: 0.35, threhold: -55, block_time: 0.24, crossfade_length: 0.06, extra_time: 2.2, f0: "rmvpe", I_noise_reduce: true, O_noise_reduce: false },
      "御姐·直播稳亮": { pitch: 12, formant: 0.18, index_rate: 0.72, rms_mix_rate: 0.45, threhold: -50, block_time: 0.32, crossfade_length: 0.09, extra_time: 2.8, f0: "rmvpe", I_noise_reduce: true, O_noise_reduce: true },
      "甜妹·高音": { pitch: 14, formant: 0.45, index_rate: 0.7, rms_mix_rate: 0.4, threhold: -55, block_time: 0.24, crossfade_length: 0.06, extra_time: 2.2, f0: "rmvpe", I_noise_reduce: true, O_noise_reduce: false },
      "游戏·低延迟": { pitch: 12, formant: 0.1, index_rate: 0.5, rms_mix_rate: 0.25, threhold: -60, block_time: 0.2, crossfade_length: 0.05, extra_time: 1.5, f0: "rmvpe", I_noise_reduce: true, O_noise_reduce: false },
      "唱歌": { pitch: 12, formant: 0.1, index_rate: 0.8, rms_mix_rate: 0.6, threhold: -60, block_time: 0.5, crossfade_length: 0.12, extra_time: 4.0, f0: "rmvpe", I_noise_reduce: false, O_noise_reduce: false },
      "极低延迟": { pitch: 12, formant: 0.1, index_rate: 0.4, rms_mix_rate: 0.2, threhold: -60, block_time: 0.12, crossfade_length: 0.03, extra_time: 1.0, f0: "rmvpe", I_noise_reduce: false, O_noise_reduce: false },
    },
    engine: { device: "cuda:0", half: true, gpu: "NVIDIA GeForce RTX 4060 Laptop GPU", vram_total: 8 },
    snapshot: {
      running: false, status: "idle", status_text: "静态预览", error: "",
      infer_ms: 0, latency_ms: 0, vu_in: 0, vu_out: 0, vram_mb: 0, callbacks: 0, samplerate: 0,
      loaded: { name: "Tsuruji", version: "v2", sr: 40000, has_index: true },
      monitor: { enabled: false, device: "耳机 (Realtek(R) Audio)", gain: 1 },
    },
  };

  /* ------------------------------------------------------------ 界面渲染 */
  function fillSelect(select, values, value) {
    if (!select) return;
    select.innerHTML = "";
    values.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
    if (value && values.includes(value)) select.value = value;
    else if (values.length) select.value = values[0];
  }

  function applySettings(settings, engineInfo) {
    const boot = state.boot;
    fillSelect($("#model-select"), boot.models.map((m) => m.name), settings.model);
    onModelChange(true);

    const hostapis = boot.hostapis || [];
    fillSelect($("#hostapi-select"), hostapis, settings.hostapi);
    fillDevices($("#hostapi-select").value, settings);
    setSegment("#sr-seg", "val", settings.sr_type === "model" ? "model" : "device");

    state.monitor = {
      enabled: !!settings.monitor_enabled,
      device: settings.monitor_device || "",
      gain: Number(settings.monitor_gain || 1),
    };
    fillMonitorDevices($("#hostapi-select").value, state.monitor.device);
    $("#monitor-on").checked = state.monitor.enabled;
    $("#monitor-gain").value = state.monitor.gain;
    setRangePct($("#monitor-gain"));
    $("#monitor-gain-val").textContent = state.monitor.gain.toFixed(2);

    const params = Object.assign({}, boot.settings.params, settings.params || {});
    Object.keys(params).forEach((key) => {
      if (key === "f0") return setSegment("#f0-seg", "val", params.f0);
      if (key === "I_noise_reduce") return ($("#denoise-in").checked = !!params.I_noise_reduce);
      if (key === "O_noise_reduce") return ($("#denoise-out").checked = !!params.O_noise_reduce);
      setSlider(key, params[key]);
    });

    // 设备信息
    const chip = $("#gpu-chip");
    const gpu = engineInfo.gpu || "CPU";
    chip.textContent = `${gpu}${engineInfo.vram_total ? ` · ${engineInfo.vram_total}GB` : ""}${engineInfo.half ? " · fp16" : ""}`;
    chip.title = `推理设备 ${engineInfo.device || "cpu"}`;

    // 虚拟声卡提示
    const allOut = Object.values(boot.devices || {}).flatMap((d) => d.outputs || []);
    const hasVM = allOut.some((d) => /voicemeeter/i.test(d));
    const hasCable = allOut.some((d) => /cable input/i.test(d));
    const hint = $("#chain-hint");
    if (state.demo) {
      hint.textContent = "静态预览：本地运行时才会检测真实声卡。";
    } else if (hasVM) {
      hint.textContent = "已识别 Voicemeeter：输出选 Aux Input，语音软件麦克风选 Aux Output。";
    } else if (hasCable) {
      hint.textContent = "已识别 VB-CABLE：输出选 CABLE Input，语音软件麦克风选 CABLE Output。";
    } else {
      hint.textContent = "未检测到虚拟声卡：先装 Voicemeeter 或 VB-CABLE，然后重启电脑。";
    }
  }

  function fillDevices(hostapi, preselect) {
    const devs = (state.boot.devices || {})[hostapi] || { inputs: [], outputs: [] };
    fillSelect($("#input-select"), devs.inputs, preselect && preselect.input_device);
    fillSelect($("#output-select"), devs.outputs, preselect && preselect.output_device);
    fillMonitorDevices(hostapi, state.monitor.device);
  }

  function fillMonitorDevices(hostapi, preselect) {
    const devs = (state.boot.devices || {})[hostapi] || { inputs: [], outputs: [] };
    const main = $("#output-select").value;
    fillSelect($("#monitor-device"), devs.outputs.filter((d) => d !== main), preselect);
  }

  /* ------------------------------------------------------------- 模型信息 */
  let modelInfoToken = 0;
  async function onModelChange(silent) {
    const name = $("#model-select").value;
    const model = (state.boot.models || []).find((m) => m.name === name);
    const indexNames = (state.boot.indexes || []).map((p) => String(p).split(/[\\/]/).pop());
    let options = ["不使用索引"].concat(indexNames);
    let pick = "不使用索引";
    if (model && model.index) {
      const base = String(model.index).split(/[\\/]/).pop();
      if (!options.includes(base)) options.push(base);
      pick = base;
    }
    fillSelect($("#index-select"), options, pick);

    const badges = $("#model-badges");
    badges.innerHTML = '<span class="badge">读取中…</span>';
    const token = ++modelInfoToken;
    try {
      const res = await api("/api/model_info", { name });
      if (token !== modelInfoToken) return;
      if (state.demo) throw new Error("demo");
      renderBadges(badges, res.info, res.model);
    } catch (e) {
      if (token !== modelInfoToken) return;
      if (state.demo) {
        renderBadges(
          badges,
          { ok: true, version: "v2", f0: 1, sr: 40000, speakers: 109 },
          { size_mb: model ? model.size_mb : 52.6, index: model ? model.index : "" }
        );
      } else {
        badges.innerHTML = '<span class="badge warn">模型信息读取失败</span>';
      }
    }
    if (!silent && state.running) markDirty("模型");
  }

  function renderBadges(box, info, model) {
    if (!box) return;
    box.innerHTML = "";
    const add = (text, cls = "") => {
      const el = document.createElement("span");
      el.className = "badge " + cls;
      el.textContent = text;
      box.appendChild(el);
    };
    if (!info || info.ok === false) {
      add("模型不可读", "warn");
      return;
    }
    add(`RVC ${info.version || "v1"}`);
    add(info.sr ? `${Math.round(info.sr / 1000)} kHz` : "? kHz");
    add(`f0 ${info.f0 ? "变调" : "不支持"}`, info.f0 ? "ok" : "warn");
    add(`说话人 ${info.speakers || 0}`);
    add(model && model.index ? "索引已配对" : "无索引", model && model.index ? "ok" : "warn");
    if (model && model.size_mb) add(`${model.size_mb} MB`);
  }

  /* ---------------------------------------------------------------- 滑块 */
  function setRangePct(input) {
    if (!input) return;
    const min = Number(input.min);
    const max = Number(input.max);
    if (!(max > min)) return;
    const pct = ((Number(input.value) - min) / (max - min)) * 100;
    input.style.setProperty("--pct", `${pct.toFixed(2)}%`);
  }

  function setSlider(key, value) {
    const row = $(`.slider[data-key="${key}"]`);
    if (!row) return;
    const input = $('input[type="range"]', row);
    input.value = value;
    setRangePct(input);
    $("output", row).textContent = fmtValue(key, input.value);
    state.params[key] = Number(input.value);
    if (key === "monitor_gain") $("#monitor-gain-val").textContent = Number(input.value).toFixed(2);
  }

  function collectParams() {
    const params = {};
    $$(".slider[data-key]").forEach((row) => {
      const key = row.dataset.key;
      if (key === "monitor_gain") return;
      params[key] = Number($('input[type="range"]', row).value);
    });
    params.f0 = segmentValue("#f0-seg", "val");
    params.I_noise_reduce = $("#denoise-in").checked;
    params.O_noise_reduce = $("#denoise-out").checked;
    return params;
  }

  let hotTimer = null;
  function pushHot() {
    if (!state.running || state.demo) return;
    clearTimeout(hotTimer);
    hotTimer = window.setTimeout(async () => {
      const payload = {};
      HOT_KEYS.forEach((k) => (payload[k] = state.params[k]));
      try { await api("/api/hot", { params: payload }); } catch (e) { /* 忽略瞬时失败 */ }
    }, 160);
  }

  function markDirty(what) {
    if (!state.running) return;
    state.dirty = true;
    const hint = $("#ctl-hint");
    hint.textContent = `「${what}」需要重新开始才生效`;
    hint.classList.add("bad");
  }

  /* -------------------------------------------------------------- 预设 */
  function buildPresets() {
    const wrap = $("#preset-chips");
    wrap.innerHTML = "";
    Object.keys(state.boot.presets || {}).forEach((name) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip-btn";
      btn.textContent = name;
      btn.addEventListener("click", () => applyPreset(name, btn));
      wrap.appendChild(btn);
    });
  }

  function applyPreset(name, btn) {
    const preset = (state.boot.presets || {})[name];
    if (!preset) return;
    $$("#preset-chips .chip-btn").forEach((b) => b.classList.remove("on"));
    if (btn) btn.classList.add("on");
    Object.keys(preset).forEach((key) => {
      if (key === "f0") return setSegment("#f0-seg", "val", preset.f0);
      if (key === "I_noise_reduce") return ($("#denoise-in").checked = !!preset[key]);
      if (key === "O_noise_reduce") return ($("#denoise-out").checked = !!preset[key]);
      setSlider(key, preset[key]);
    });
    if (state.running) {
      pushHot();
      markDirty("预设");
      toast("预设已套用，重新开始后完全生效", "warn");
    } else {
      toast(`已套用 ${name}`, "ok");
    }
    log(`套用预设 ${name}`);
  }

  /* ----------------------------------------------------------- 状态/电平 */
  function setState(kind, text) {
    const pill = $("#state-pill");
    const dot = $("#state-dot");
    ['idle', 'starting', 'running', 'error'].forEach((s) => {
      pill.removeAttribute(`data-${s}`);
    });
    pill.dataset.state = kind;
    if (dot) dot.dataset.state = kind;
    const label = { idle: "未启动", starting: "加载中", running: "运行中", error: "出错" }[kind] || "未启动";
    $("#state-text").textContent = text || label;
  }

  function dbText(pct) {
    if (!pct) return "-∞";
    const db = (pct / 100) * 60 - 60;
    return `${db.toFixed(0)}`;
  }

  function renderMeters(snapshot) {
    [["in", snapshot.vu_in], ["out", snapshot.vu_out]].forEach(([side, raw]) => {
      const pct = Math.max(0, Math.min(100, Number(raw) || 0));
      const fill = $(`#vu-${side}`);
      if (fill) fill.style.width = `${pct}%`;
      const peak = $(`#vu-${side}-peak`);
      state.peaks[side] = Math.max(pct, (state.peaks[side] || 0) - 1.6);
      if (peak) peak.style.left = `${state.peaks[side].toFixed(1)}%`;
      const num = $(`#vu-${side}-num`);
      if (num) num.textContent = dbText(pct);
    });
  }

  function renderSnapshot(snapshot) {
    if (!snapshot) return;
    renderMeters(snapshot);
    if ($("#st-lat")) $("#st-lat").textContent = Math.round(snapshot.latency_ms || 0);
    if ($("#st-inf")) $("#st-inf").textContent = Math.round(snapshot.infer_ms || 0);
    if ($("#st-vram")) $("#st-vram").textContent = Math.round(snapshot.vram_mb || 0);

    if (snapshot.status && snapshot.status !== state.lastStatus) {
      state.lastStatus = snapshot.status;
      if (!state.busy) setState(snapshot.status === "starting" ? "starting" : snapshot.status);
    }

    const running = !!snapshot.running;
    if (running !== state.running && !state.busy) {
      state.running = running;
      syncTransport();
    }

    if (running && !state.busy) {
      const mon = snapshot.monitor || {};
      state.monitor.enabled = !!mon.enabled;
      const box = $("#monitor-on");
      if (box && document.activeElement !== box) box.checked = !!mon.enabled;
    }

    const sub = $("#console-sub");
    if (sub) {
      if (snapshot.running) {
        const loaded = snapshot.loaded || {};
        const mon = snapshot.monitor || {};
        sub.textContent = `${loaded.name || "模型"} · ${snapshot.samplerate || 0} Hz${mon.enabled ? " · 耳机监听开" : ""}`;
      } else {
        sub.textContent = state.demo ? "静态预览 · 无后端" : "待机 · 设备未就绪";
      }
    }
  }

  function syncTransport() {
    const btn = $("#toggle-btn");
    const label = $("#toggle-label");
    const icon = $(".btn-ico use", btn);
    if (!btn) return;
    btn.classList.toggle("running", state.running);
    label.textContent = state.running ? "停止变声" : "开始变声";
    if (icon) icon.setAttribute("href", state.running ? "#i-stop" : "#i-play");
    if (!state.running) {
      $("#ctl-hint").textContent = "选择模型与设备后开始";
      $("#ctl-hint").classList.remove("bad");
    }
  }

  function setBusy(busy, label) {
    state.busy = busy;
    const btn = $("#toggle-btn");
    if (btn) btn.disabled = busy;
    if (label && $("#toggle-label")) $("#toggle-label").textContent = label;
  }

  /* ---------------------------------------------------------- 开始 / 停止 */
  async function startEngine() {
    if (state.busy) return;
    if (state.demo) {
      toast("静态预览里不会真的变声，请本地启动服务", "warn");
      log("静态预览：后端未连接", "warn");
      return;
    }
    const model = $("#model-select").value;
    if (!model) return toast("assets/weights 里没有模型", "bad");
    const monitorOn = $("#monitor-on").checked;
    setBusy(true, "加载中…");
    setState("starting", "加载中");
    $("#ctl-hint").textContent = "正在加载模型并预热 CUDA Graph（约 8～15 秒）";
    $("#ctl-hint").classList.remove("bad");
    log(`开始加载模型 ${model}`);

    try {
      const res = await api("/api/start", {
        model,
        index_path: indexPath($("#index-select").value),
        hostapi: $("#hostapi-select").value,
        input_device: $("#input-select").value,
        output_device: $("#output-select").value,
        sr_type: segmentValue("#sr-seg", "val"),
        mode: state.mode,
        monitor_device: monitorOn ? $("#monitor-device").value : "",
        monitor_gain: Number($("#monitor-gain").value),
        params: collectParams(),
      });
      state.running = true;
      state.dirty = false;
      setBusy(false);
      syncTransport();
      setState("running", "运行中");
      const snap = res.snapshot || {};
      renderSnapshot(snap);
      log(`变声已启动：${model}${monitorOn ? "（耳机监听开）" : ""}`, "ok");
      toast("变声已启动", "ok");
      $("#ctl-hint").textContent = `运行中 · ${snap.samplerate || ""} Hz · ${snap.monitor && snap.monitor.enabled ? "耳机监听开" : "仅虚拟声卡输出"}`;
      // 同步一次真实设置（服务端可能自动回退采样率）
      try {
        const boot = await api("/api/bootstrap");
        state.boot = boot;
        setSegment("#sr-seg", "val", boot.settings.sr_type === "model" ? "model" : "device");
      } catch (e) { /* ignore */ }
    } catch (e) {
      state.running = false;
      setBusy(false);
      syncTransport();
      setState("error", "启动失败");
      $("#ctl-hint").textContent = e.message;
      $("#ctl-hint").classList.add("bad");
      log(`启动失败：${e.message}`, "bad");
      toast(`启动失败：${e.message}`, "bad");
    }
  }

  async function stopEngine() {
    if (state.busy) return;
    setBusy(true, "停止中…");
    try { await api("/api/stop", {}); } catch (e) { /* ignore */ }
    state.running = false;
    state.dirty = false;
    setBusy(false);
    syncTransport();
    setState("idle", "已停止");
    log("已停止");
  }

  function indexPath(label) {
    if (!label || label === "不使用索引") return "";
    const hit = (state.boot.indexes || []).find((p) => String(p).split(/[\\/]/).pop() === label);
    return hit || "";
  }

  /* ------------------------------------------------------------- 监听 */
  let monitorTimer = null;
  async function pushMonitor(enabled, quiet) {
    if (state.demo) {
      state.monitor.enabled = !!enabled;
      toast(enabled ? "静态预览：监听开关只在本地生效" : "耳机监听已关闭", "warn");
      return;
    }
    const payload = {
      enabled: enabled !== undefined ? enabled : $("#monitor-on").checked,
      device: $("#monitor-device").value || "",
      gain: Number($("#monitor-gain").value),
    };
    try {
      const res = await api("/api/monitor", payload);
      const snapshot = res.snapshot || {};
      const real = !!(snapshot.monitor && snapshot.monitor.enabled);
      state.monitor.enabled = real;
      state.monitor.device = payload.device;
      state.monitor.gain = payload.gain;
      if (payload.enabled && !real && res.error) {
        toast(`监听打开失败：${res.error}`, "bad");
        log(`监听失败：${res.error}`, "bad");
        $("#monitor-on").checked = false;
      } else if (payload.enabled && res.note) {
        toast("已记住：开始变声后自动监听", "ok");
      } else if (!quiet) {
        toast(real ? "耳机监听已打开" : "耳机监听已关闭", real ? "ok" : "");
        log(real ? `耳机监听：${payload.device}` : "耳机监听已关闭");
      }
    } catch (e) {
      toast(`监听设置失败：${e.message}`, "bad");
    }
  }

  /* ---------------------------------------------------------- WebSocket */
  let socket = null;
  function connectSocket() {
    if (state.demo) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try {
      socket = new WebSocket(`${proto}://${location.host}/ws`);
    } catch (e) {
      return;
    }
    socket.addEventListener("message", (event) => {
      try { renderSnapshot(JSON.parse(event.data)); } catch (e) { /* ignore */ }
    });
    socket.addEventListener("close", () => {
      if (!state.demo) window.setTimeout(connectSocket, 1500);
    });
    socket.addEventListener("error", () => socket && socket.close());
  }

  /* ------------------------------------------------------------- 事件 */
  function bindEvents() {
    $("#theme-btn").addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme");
      applyTheme(current === "dark" ? "light" : "dark", true);
    });

    $("#model-select").addEventListener("change", () => onModelChange(false));
    $("#index-select").addEventListener("change", () => { if (state.running) markDirty("索引"); });
    $("#refresh-models").addEventListener("click", async () => {
      if (state.demo) return toast("静态预览：已是最新列表", "warn");
      const boot = await api("/api/bootstrap");
      state.boot = boot;
      fillSelect($("#model-select"), boot.models.map((m) => m.name), $("#model-select").value);
      onModelChange(true);
      toast("模型列表已刷新", "ok");
    });
    $("#open-models").addEventListener("click", async () => {
      if (state.demo) return toast("静态预览：无本地目录", "warn");
      try { await api("/api/open_models", {}); } catch (e) { toast("打开目录失败", "bad"); }
    });

    $("#hostapi-select").addEventListener("change", () => {
      fillDevices($("#hostapi-select").value);
      if (state.running) toast("设备类型已改，需重新开始", "warn");
    });
    $("#output-select").addEventListener("change", () => {
      fillMonitorDevices($("#hostapi-select").value, state.monitor.device);
    });
    $("#auto-vm").addEventListener("click", async () => {
      if (state.demo) return toast("静态预览：无法检测本地声卡", "warn");
      try {
        const res = await api("/api/voicemeeter", {});
        if (!res.found) return toast("没检测到虚拟声卡，先装 Voicemeeter 或 VB-CABLE", "bad");
        fillSelect($("#hostapi-select"), state.boot.hostapis, res.hostapi);
        state.boot.devices[res.hostapi] = state.boot.devices[res.hostapi] || res.devices || state.boot.devices[res.hostapi];
        fillDevices(res.hostapi, { input_device: res.input_device, output_device: res.output_device });
        toast("已匹配虚拟声卡设备", "ok");
        log(`匹配虚拟声卡：${res.input_device} → ${res.output_device}`, "ok");
      } catch (e) {
        toast(`匹配失败：${e.message}`, "bad");
      }
    });

    $("#sr-seg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      setSegment("#sr-seg", "val", btn.dataset.val);
      if (state.running) markDirty("采样率");
    });
    $("#f0-seg").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      setSegment("#f0-seg", "val", btn.dataset.val);
      if (state.running) markDirty("音高算法");
    });
    $("#mode-seg").addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      setSegment("#mode-seg", "mode", btn.dataset.mode);
      state.mode = btn.dataset.mode;
      if (!state.demo) {
        try { await api("/api/mode", { mode: state.mode }); } catch (err) { /* ignore */ }
      }
      log(state.mode === "vc" ? "切换到变声" : "切换到监听原声");
    });

    $$(".slider[data-key]").forEach((row) => {
      const key = row.dataset.key;
      const input = $('input[type="range"]', row);
      input.addEventListener("input", () => {
        setSlider(key, Number(input.value));
        if (HOT_KEYS.includes(key)) pushHot();
      });
      if (PERF_KEYS.includes(key)) input.addEventListener("change", () => markDirty("性能参数"));
    });
    $("#denoise-in").addEventListener("change", () => markDirty("输入降噪"));
    $("#denoise-out").addEventListener("change", () => markDirty("输出降噪"));

    $("#monitor-on").addEventListener("change", () => {
      if ($("#monitor-on").checked && !$("#monitor-device").value) {
        $("#monitor-on").checked = false;
        toast("先选择一个监听设备", "warn");
        return;
      }
      pushMonitor();
    });
    $("#monitor-device").addEventListener("change", () => {
      if ($("#monitor-on").checked) pushMonitor(true, true);
      else state.monitor.device = $("#monitor-device").value;
    });
    $("#monitor-gain").addEventListener("input", () => {
      setRangePct($("#monitor-gain"));
      $("#monitor-gain-val").textContent = Number($("#monitor-gain").value).toFixed(2);
      if ($("#monitor-on").checked) {
        clearTimeout(monitorTimer);
        monitorTimer = window.setTimeout(() => pushMonitor(true, true), 180);
      }
    });

    $("#toggle-btn").addEventListener("click", () => (state.running ? stopEngine() : startEngine()));
    $("#clear-log").addEventListener("click", () => { logRows.length = 0; renderLog(); });

    const openDrawer = () => {
      $("#drawer-mask").hidden = false;
      $("#drawer").hidden = false;
      requestAnimationFrame(() => {
        $("#drawer-mask").classList.add("open");
        $("#drawer").classList.add("open");
      });
    };
    const closeDrawer = () => {
      $("#drawer-mask").classList.remove("open");
      $("#drawer").classList.remove("open");
      window.setTimeout(() => {
        $("#drawer-mask").hidden = true;
        $("#drawer").hidden = true;
      }, 240);
    };
    $("#help-btn").addEventListener("click", openDrawer);
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-mask").addEventListener("click", closeDrawer);

    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "Enter") {
        e.preventDefault();
        state.running ? stopEngine() : startEngine();
      }
      if (e.key === "Escape") closeDrawer();
    });
  }

  /* ---------------------------------------------------------------- 启动 */
  async function boot() {
    initTheme();
    let data = null;
    try {
      const res = await fetch(API + "/api/bootstrap");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (e) {
      data = null;
    }

    if (data && data.ok) {
      state.boot = data;
      state.demo = false;
      $("#demo-banner").hidden = true;
    } else {
      state.boot = DEMO;
      state.demo = true;
      $("#demo-banner").hidden = false;
      $("#demo-text").textContent = "静态预览模式 · 未连接本地服务，界面可操作但不会真的变声";
      setState("idle", "静态预览");
    }

    document.documentElement.dataset.mode = state.demo ? "demo" : "live";
    document.title = "变声台 · RVC Voice Studio" + (state.demo ? "（预览）" : "");
    buildPresets();
    applySettings(state.boot.settings || {}, state.boot.engine || {});
    bindEvents();
    renderLog();
    if (!state.demo) {
      setState("idle", "未启动");
      log(`界面就绪 · 模型 ${(state.boot.models || []).length} 个 · 索引 ${(state.boot.indexes || []).length} 个`);
      connectSocket();
      renderSnapshot(state.boot.snapshot || {});
    } else {
      log("静态预览：未连接本地服务", "warn");
      renderSnapshot(DEMO.snapshot);
    }
    syncTransport();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
