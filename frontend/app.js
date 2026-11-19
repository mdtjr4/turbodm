(() => {
  "use strict";

  const state = {
    items: new Map(),
    selectedId: null,
    filter: "all",
    search: "",
    theme: document.documentElement.getAttribute("data-theme") || "light",
    inspectorCollapsed: false,
    settings: {
      download_dir: "",
      default_connections: 12,
      max_concurrent_downloads: 3,
      speed_limit_kbps: 0,
      ytdlp_concurrent_fragments: 16,
      preferred_video_quality: "best",
      cookies_file: "",
      auto_categorize_folders: false,
      enable_turbo_video_segments: true,
    },
    probeData: null,
    selectedFormatIdx: 0,
    deleteTargetId: null,
    cloudMode: false,
    autoDownloadedIds: new Set(),
    ws: null,
    wsRetryTimer: null,
  };

  // DOM Elements
  const $ = (id) => document.getElementById(id);
  const els = {
    quickUrlInput: $("quickUrlInput"),
    quickPasteBtn: $("quickPasteBtn"),
    quickAnalyzeBtn: $("quickAnalyzeBtn"),
    quickInstantBtn: $("quickInstantBtn"),
    wsStatusPill: $("wsStatusPill"),
    wsStatusText: $("wsStatusText"),
    ffmpegBadge: $("ffmpegBadge"),
    themeToggleBtn: $("themeToggleBtn"),
    themeToggleIcon: $("themeToggleIcon"),
    themeToggleLabel: $("themeToggleLabel"),
    btnHelp: $("btnHelp"),

    btnAddUrl: $("btnAddUrl"),
    btnBatchUrl: $("btnBatchUrl"),
    btnResumeSel: $("btnResumeSel"),
    btnPauseSel: $("btnPauseSel"),
    btnPauseAll: $("btnPauseAll"),
    btnResumeAll: $("btnResumeAll"),
    btnDeleteSel: $("btnDeleteSel"),
    btnClearCompleted: $("btnClearCompleted"),
    btnOpenFolder: $("btnOpenFolder"),
    btnSettings: $("btnSettings"),
    searchInput: $("searchInput"),

    downloadTableBody: $("downloadTableBody"),
    emptyState: $("emptyState"),
    emptyAddBtn: $("emptyAddBtn"),
    emptyBatchBtn: $("emptyBatchBtn"),
    dropZone: $("dropZone"),

    globalSpeedValue: $("globalSpeedValue"),
    sidebarActiveBadge: $("sidebarActiveBadge"),
    sidebarDirLabel: $("sidebarDirLabel"),

    // Inspector
    inspectorPanel: $("inspectorPanel"),
    inspToggleBtn: $("inspToggleBtn"),
    inspToggleLabel: $("inspToggleLabel"),
    inspModePill: $("inspModePill"),
    inspFilename: $("inspFilename"),
    inspMetaRight: $("inspMetaRight"),
    inspThumbWrap: $("inspThumbWrap"),
    inspThumb: $("inspThumb"),
    inspThumbFallback: $("inspThumbFallback"),
    inspUrl: $("inspUrl"),
    inspPath: $("inspPath"),
    inspStatus: $("inspStatus"),
    inspResume: $("inspResume"),
    inspSegSummary: $("inspSegSummary"),
    inspSegGrid: $("inspSegGrid"),
    inspSpeedNow: $("inspSpeedNow"),
    speedCanvas: $("speedCanvas"),

    // Add Modal
    addModal: $("addModal"),
    modalUrlInput: $("modalUrlInput"),
    modalPasteBtn: $("modalPasteBtn"),
    modalAnalyzeBtn: $("modalAnalyzeBtn"),
    probeLoading: $("probeLoading"),
    probeError: $("probeError"),
    probeResultBox: $("probeResultBox"),
    previewThumbBox: $("previewThumbBox"),
    previewThumb: $("previewThumb"),
    previewThumbIcon: $("previewThumbIcon"),
    previewDuration: $("previewDuration"),
    previewExtractor: $("previewExtractor"),
    previewRangeBadge: $("previewRangeBadge"),
    previewSizeBadge: $("previewSizeBadge"),
    previewTitle: $("previewTitle"),
    previewSub: $("previewSub"),
    formatsSection: $("formatsSection"),
    formatList: $("formatList"),
    playlistSection: $("playlistSection"),
    playlistTitleLabel: $("playlistTitleLabel"),
    playlistQualitySelect: $("playlistQualitySelect"),
    plSelectAll: $("plSelectAll"),
    plSelectNone: $("plSelectNone"),
    playlistList: $("playlistList"),
    singleOptionsGrid: $("singleOptionsGrid"),
    modalFilenameInput: $("modalFilenameInput"),
    modalSaveDirInput: $("modalSaveDirInput"),
    modalBrowseBtn: $("modalBrowseBtn"),
    modalConnSlider: $("modalConnSlider"),
    modalConnLabel: $("modalConnLabel"),
    modalAddPausedBtn: $("modalAddPausedBtn"),
    modalStartBtn: $("modalStartBtn"),

    // Batch Modal
    batchModal: $("batchModal"),
    batchUrlsInput: $("batchUrlsInput"),
    batchQualitySelect: $("batchQualitySelect"),
    batchSaveDirInput: $("batchSaveDirInput"),
    batchBrowseBtn: $("batchBrowseBtn"),
    batchStartBtn: $("batchStartBtn"),

    // Settings Modal
    settingsModal: $("settingsModal"),
    setDownloadDir: $("setDownloadDir"),
    setBrowseBtn: $("setBrowseBtn"),
    setConnections: $("setConnections"),
    setMaxConcurrent: $("setMaxConcurrent"),
    setYtdlpFrags: $("setYtdlpFrags"),
    setSpeedLimit: $("setSpeedLimit"),
    setCookiesFile: $("setCookiesFile"),
    setTurboVideo: $("setTurboVideo"),
    setAutoCategorize: $("setAutoCategorize"),
    saveSettingsBtn: $("saveSettingsBtn"),

    // Delete Modal
    deleteModal: $("deleteModal"),
    deleteConfirmText: $("deleteConfirmText"),
    deleteFromDiskCheck: $("deleteFromDiskCheck"),
    confirmDeleteBtn: $("confirmDeleteBtn"),

    // Help Modal
    helpModal: $("helpModal"),

    contextMenu: $("contextMenu"),
    toastContainer: $("toastContainer"),
  };

  // =========================================================================
  // Theme (Light / Dark) & Collapsible Inspector Preferences
  // =========================================================================
  function applyTheme(theme, persist = true) {
    const t = theme === "dark" ? "dark" : "light";
    state.theme = t;
    document.documentElement.setAttribute("data-theme", t);
    if (persist) {
      try {
        localStorage.setItem("turbodm_theme", t);
      } catch {}
    }
    if (els.themeToggleIcon && els.themeToggleLabel) {
      if (t === "light") {
        els.themeToggleIcon.textContent = "🌙";
        els.themeToggleLabel.textContent = "Dark";
        els.themeToggleBtn.title = "Switch to Dark Mode";
      } else {
        els.themeToggleIcon.textContent = "☀️";
        els.themeToggleLabel.textContent = "Light";
        els.themeToggleBtn.title = "Switch to Light Mode";
      }
    }
    // Redraw speed graph with theme-appropriate colors
    const sel = state.selectedId ? state.items.get(state.selectedId) : null;
    drawSpeedChart(sel ? sel.speed_history || [] : []);
  }

  function applyInspectorState(collapsed, persist = true) {
    state.inspectorCollapsed = Boolean(collapsed);
    if (els.inspectorPanel) {
      els.inspectorPanel.classList.toggle("collapsed", state.inspectorCollapsed);
    }
    if (els.inspToggleLabel) {
      els.inspToggleLabel.textContent = state.inspectorCollapsed ? "Show Details" : "Hide Details";
    }
    if (persist) {
      try {
        localStorage.setItem("turbodm_inspector_collapsed", state.inspectorCollapsed ? "1" : "0");
      } catch {}
    }
  }

  function initUiPreferences() {
    let savedTheme = "light";
    let savedCollapsed = false;
    try {
      const t = localStorage.getItem("turbodm_theme");
      if (t === "dark" || t === "light") savedTheme = t;
      savedCollapsed = localStorage.getItem("turbodm_inspector_collapsed") === "1";
    } catch {}
    applyTheme(savedTheme, false);
    applyInspectorState(savedCollapsed, false);
  }

  // =========================================================================
  // Formatting Utilities
  // =========================================================================
  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined || bytes < 0) return "—";
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let val = Number(bytes);
    let idx = 0;
    while (val >= 1024 && idx < units.length - 1) {
      val /= 1024;
      idx++;
    }
    return idx === 0 ? `${Math.round(val)} B` : `${val.toFixed(2)} ${units[idx]}`;
  }

  function formatSpeed(bps) {
    if (!bps || bps <= 0) return "0.00 MB/s";
    if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
    return `${(bps / (1024 * 1024)).toFixed(2)} MB/s`;
  }

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return "—";
    const s = Math.round(seconds);
    if (s === 0) return "0s";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  function formatDuration(seconds) {
    if (!seconds || !isFinite(seconds)) return "";
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  }

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    els.toastContainer.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(8px)";
      el.style.transition = "all 0.2s";
      setTimeout(() => el.remove(), 220);
    }, 3200);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return data;
  }

  // =========================================================================
  // WebSocket Live Sync
  // =========================================================================
  function connectWebSocket() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    state.ws = ws;

    ws.onopen = () => {
      els.wsStatusPill.classList.remove("disconnected");
      els.wsStatusText.textContent = "Ready";
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          state.items.clear();
          for (const item of msg.items || []) {
            state.items.set(item.id, item);
          }
          if (msg.settings) applySettingsToState(msg.settings);
          if (msg.system) updateSystemBadges(msg.system);
          if (!state.selectedId && msg.items && msg.items.length > 0) {
            state.selectedId = msg.items[0].id;
          }
          renderAll();
        } else if (msg.type === "item_update" && msg.item) {
          const prev = state.items.get(msg.item.id);
          const isNew = !prev;
          state.items.set(msg.item.id, msg.item);
          if (isNew || !state.selectedId) {
            state.selectedId = msg.item.id;
          }
          renderAll();

          // In Cloud Mode, automatically send the completed file straight to the user's device!
          if (
            state.cloudMode &&
            msg.item.status === "completed" &&
            (!prev || prev.status !== "completed") &&
            !state.autoDownloadedIds.has(msg.item.id)
          ) {
            state.autoDownloadedIds.add(msg.item.id);
            triggerDeviceDownload(msg.item);
            toast(`Download ready! Saving "${msg.item.filename}" to your device...`, "success");
          }
        } else if (msg.type === "item_deleted") {
          state.items.delete(msg.id);
          if (state.selectedId === msg.id) {
            const first = getOrderedItems()[0];
            state.selectedId = first ? first.id : null;
          }
          renderAll();
        } else if (msg.type === "items_cleared") {
          for (const id of msg.ids || []) state.items.delete(id);
          if (!state.items.has(state.selectedId)) {
            const first = getOrderedItems()[0];
            state.selectedId = first ? first.id : null;
          }
          renderAll();
        } else if (msg.type === "settings_update" && msg.settings) {
          applySettingsToState(msg.settings);
        }
      } catch (e) {
        console.error("WS parse error:", e);
      }
    };

    ws.onclose = () => {
      els.wsStatusPill.classList.add("disconnected");
      els.wsStatusText.textContent = "Reconnecting...";
      clearTimeout(state.wsRetryTimer);
      state.wsRetryTimer = setTimeout(connectWebSocket, 1500);
    };

    ws.onerror = () => ws.close();
  }

  function triggerDeviceDownload(item) {
    const a = document.createElement("a");
    a.href = `/api/downloads/${encodeURIComponent(item.id)}/file`;
    a.download = item.filename || "download";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function applySettingsToState(settings) {
    state.settings = settings;
    if (state.cloudMode) {
      els.sidebarDirLabel.textContent = "Auto-saves to your device";
      els.sidebarDirLabel.title = "Files automatically download to your browser";
    } else {
      els.sidebarDirLabel.textContent = settings.download_dir || "Downloads/TurboDM";
      els.sidebarDirLabel.title = settings.download_dir || "";
    }
  }

  function updateSystemBadges(sys) {
    if (sys.ffmpeg_ready) {
      els.ffmpegBadge.textContent = "FFmpeg Ready";
      els.ffmpegBadge.style.display = "inline-flex";
    } else {
      els.ffmpegBadge.textContent = "FFmpeg Missing";
    }
    if (typeof sys.cloud_mode === "boolean" && state.cloudMode !== sys.cloud_mode) {
      state.cloudMode = sys.cloud_mode;
      const hideInCloud = [els.btnOpenFolder, els.modalBrowseBtn, els.batchBrowseBtn, els.setBrowseBtn];
      hideInCloud.forEach((el) => {
        if (el) el.style.display = state.cloudMode ? "none" : "";
      });
      // Hide server-side folder & cookies fields in Cloud Mode
      const hideGroups = [els.modalSaveDirInput, els.batchSaveDirInput, els.setDownloadDir, els.setCookiesFile];
      hideGroups.forEach((el) => {
        const group = el && el.closest(".field-group");
        if (group) group.style.display = state.cloudMode ? "none" : "";
      });
      if (state.cloudMode) {
        els.sidebarDirLabel.textContent = "Auto-saves to your device";
        els.sidebarDirLabel.title = "Files automatically download to your browser";
      }
      renderAll();
    }
  }

  // =========================================================================
  // Filtering & Rendering
  // =========================================================================
  function getOrderedItems() {
    return Array.from(state.items.values()).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  }

  function matchesFilter(item) {
    const f = state.filter;
    if (f === "active") {
      if (!["downloading", "probing", "merging", "queued"].includes(item.status)) return false;
    } else if (f === "unfinished") {
      if (item.status === "completed") return false;
    } else if (f === "completed") {
      if (item.status !== "completed") return false;
    } else if (f.startsWith("cat:")) {
      const cat = f.slice(4);
      if (item.category !== cat) return false;
    }

    if (state.search) {
      const q = state.search.toLowerCase();
      const hay = `${item.filename} ${item.title} ${item.extractor || ""} ${item.url}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function renderAll() {
    const all = getOrderedItems();
    renderCountsAndGlobalSpeed(all);
    renderTable(all.filter(matchesFilter));
    updateToolbarButtons();
    renderInspector();
  }

  function renderCountsAndGlobalSpeed(all) {
    let active = 0, unfinished = 0, completed = 0;
    const cats = { video: 0, audio: 0, compressed: 0, documents: 0, programs: 0 };
    let totalSpeed = 0;

    for (const i of all) {
      if (["downloading", "probing", "merging", "queued"].includes(i.status)) active++;
      if (i.status === "completed") completed++;
      else unfinished++;
      if (cats[i.category] !== undefined) cats[i.category]++;
      if (i.status === "downloading") totalSpeed += i.speed || 0;
    }

    $("cnt-all").textContent = all.length;
    $("cnt-active").textContent = active;
    $("cnt-unfinished").textContent = unfinished;
    $("cnt-completed").textContent = completed;
    $("cnt-video").textContent = cats.video;
    $("cnt-audio").textContent = cats.audio;
    $("cnt-compressed").textContent = cats.compressed;
    $("cnt-documents").textContent = cats.documents;
    $("cnt-programs").textContent = cats.programs;

    els.globalSpeedValue.textContent = formatSpeed(totalSpeed);
    els.sidebarActiveBadge.textContent = `${active} active`;
  }

  function buildRowHtml(item) {
    const ext = (item.filename.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
    const thumbHtml = item.thumbnail
      ? `<img src="${escHtml(item.thumbnail)}" alt="" loading="lazy" />`
      : `<span>${escHtml(ext)}</span>`;

    const sizeStr = item.total_bytes > 0
      ? (item.status === "completed"
          ? formatBytes(item.total_bytes)
          : `${formatBytes(item.downloaded_bytes)} / ${formatBytes(item.total_bytes)}`)
      : (item.downloaded_bytes > 0 ? formatBytes(item.downloaded_bytes) : "—");

    const pct = Math.min(100, Math.max(0, Number(item.progress || 0)));
    const pctText = item.status === "completed" ? "100%" : `${pct.toFixed(1)}%`;
    const isActive = ["downloading", "probing", "merging"].includes(item.status);
    const speedText = item.status === "downloading" ? formatSpeed(item.speed) : "—";
    const etaText = item.status === "downloading" ? formatEta(item.eta) : (item.status === "completed" ? "Done" : "—");
    const connText = isActive ? `${item.active_connections || item.connections}x` : `${item.connections}x`;
    const modeBadge = item.mode === "video_turbo"
      ? `<span class="src-pill turbo">Fast</span>`
      : "";

    const primaryBtn = isActive || item.status === "queued"
      ? `<button class="row-btn" data-row-act="pause" data-id="${item.id}" title="Pause">⏸</button>`
      : item.status === "completed"
        ? (state.cloudMode
            ? `<button class="row-btn save-btn" data-row-act="save-device" data-id="${item.id}" title="Download to your PC / Phone">⬇ Save</button>`
            : `<button class="row-btn" data-row-act="open" data-id="${item.id}" title="Open File">▶</button>`)
        : `<button class="row-btn" data-row-act="resume" data-id="${item.id}" title="Resume / Retry">▶</button>`;

    return `
      <td>
        <div class="file-cell">
          <div class="file-thumb" data-field="thumb">${thumbHtml}</div>
          <div class="file-meta">
            <div class="file-title" data-field="filename" title="${escHtml(item.filename)}">${escHtml(item.filename)}</div>
            <div class="file-sub" data-field="sub">
              <span class="src-pill">${escHtml(item.extractor || "Direct")}</span>
              ${modeBadge}
              ${item.format_label ? `<span class="truncate">${escHtml(item.format_label)}</span>` : ""}
            </div>
          </div>
        </div>
      </td>
      <td class="mono small" data-field="size">${escHtml(sizeStr)}</td>
      <td>
        <div class="prog-cell">
          <div class="prog-top">
            <span class="status-label status-${item.status}" data-field="statusText" title="${escHtml(item.error_message || item.status_text)}">${escHtml(item.status_text)}</span>
            <span class="mono small" data-field="pct">${pctText}</span>
          </div>
          <div class="prog-track">
            <div class="prog-fill ${item.status}" data-field="bar" style="width:${item.status === 'merging' ? 100 : pct}%"></div>
          </div>
        </div>
      </td>
      <td class="mono small ${item.status === 'downloading' ? 'accent-text' : 'muted'}" data-field="speed">${speedText}</td>
      <td class="mono small muted" data-field="eta">${etaText}</td>
      <td class="mono small muted" data-field="conn">${connText}</td>
      <td>
        <div class="row-actions" data-field="actions">
          ${primaryBtn}
          ${state.cloudMode ? "" : `<button class="row-btn" data-row-act="reveal" data-id="${item.id}" title="Show in Folder">📁</button>`}
          <button class="row-btn danger" data-row-act="delete" data-id="${item.id}" title="Remove">✕</button>
        </div>
      </td>
    `;
  }

  function patchRowInPlace(tr, item) {
    const selected = item.id === state.selectedId;
    tr.classList.toggle("selected", selected);

    // If major structural metadata changed (status, filename, thumbnail, mode), rebuild cells cleanly
    const sig = `${item.status}|${item.filename}|${item.thumbnail || ""}|${item.mode}|${item.extractor || ""}|${state.cloudMode}`;
    if (tr.dataset.sig !== sig) {
      tr.dataset.sig = sig;
      tr.innerHTML = buildRowHtml(item);
      return;
    }

    // Fast flicker-free telemetry update (never interrupts button clicks!)
    const sizeStr = item.total_bytes > 0
      ? (item.status === "completed"
          ? formatBytes(item.total_bytes)
          : `${formatBytes(item.downloaded_bytes)} / ${formatBytes(item.total_bytes)}`)
      : (item.downloaded_bytes > 0 ? formatBytes(item.downloaded_bytes) : "—");
    const pct = Math.min(100, Math.max(0, Number(item.progress || 0)));
    const pctText = item.status === "completed" ? "100%" : `${pct.toFixed(1)}%`;
    const isActive = ["downloading", "probing", "merging"].includes(item.status);
    const speedText = item.status === "downloading" ? formatSpeed(item.speed) : "—";
    const etaText = item.status === "downloading" ? formatEta(item.eta) : (item.status === "completed" ? "Done" : "—");
    const connText = isActive ? `${item.active_connections || item.connections}x` : `${item.connections}x`;

    const elSize = tr.querySelector('[data-field="size"]');
    const elSt = tr.querySelector('[data-field="statusText"]');
    const elPct = tr.querySelector('[data-field="pct"]');
    const elBar = tr.querySelector('[data-field="bar"]');
    const elSpd = tr.querySelector('[data-field="speed"]');
    const elEta = tr.querySelector('[data-field="eta"]');
    const elConn = tr.querySelector('[data-field="conn"]');

    if (elSize) elSize.textContent = sizeStr;
    if (elSt) {
      elSt.textContent = item.status_text;
      elSt.title = item.error_message || item.status_text;
    }
    if (elPct) elPct.textContent = pctText;
    if (elBar) elBar.style.width = `${item.status === "merging" ? 100 : pct}%`;
    if (elSpd) elSpd.textContent = speedText;
    if (elEta) elEta.textContent = etaText;
    if (elConn) elConn.textContent = connText;
  }

  function renderTable(items) {
    if (items.length === 0) {
      els.downloadTableBody.innerHTML = "";
      els.emptyState.classList.remove("hidden");
      return;
    }
    els.emptyState.classList.add("hidden");

    const wantedIds = new Set(items.map((i) => i.id));
    const existingRows = new Map();
    for (const tr of Array.from(els.downloadTableBody.querySelectorAll("tr[data-id]"))) {
      if (!wantedIds.has(tr.dataset.id)) {
        tr.remove();
      } else {
        existingRows.set(tr.dataset.id, tr);
      }
    }

    items.forEach((item, index) => {
      let tr = existingRows.get(item.id);
      if (!tr) {
        tr = document.createElement("tr");
        tr.dataset.id = item.id;
        tr.dataset.sig = `${item.status}|${item.filename}|${item.thumbnail || ""}|${item.mode}|${item.extractor || ""}|${state.cloudMode}`;
        tr.innerHTML = buildRowHtml(item);
        if (item.id === state.selectedId) tr.classList.add("selected");
      } else {
        patchRowInPlace(tr, item);
      }
      const curAtIdx = els.downloadTableBody.children[index];
      if (curAtIdx !== tr) {
        els.downloadTableBody.insertBefore(tr, curAtIdx || null);
      }
    });
  }

  function updateToolbarButtons() {
    const sel = state.selectedId ? state.items.get(state.selectedId) : null;
    if (!sel) {
      els.btnResumeSel.disabled = true;
      els.btnPauseSel.disabled = true;
      els.btnDeleteSel.disabled = true;
      return;
    }
    const isRunning = ["downloading", "probing", "merging", "queued"].includes(sel.status);
    els.btnPauseSel.disabled = !isRunning;
    els.btnResumeSel.disabled = isRunning || sel.status === "completed";
    els.btnDeleteSel.disabled = false;
  }

  function renderInspector() {
    const item = state.selectedId ? state.items.get(state.selectedId) : null;
    if (!item) {
      els.inspModePill.textContent = "DOWNLOAD DETAILS";
      els.inspFilename.textContent = "Click any download above to see live speed & details";
      els.inspMetaRight.textContent = "";
      els.inspUrl.textContent = "—";
      els.inspPath.textContent = "—";
      els.inspStatus.textContent = "—";
      els.inspResume.textContent = "—";
      els.inspSegSummary.textContent = "0 parts";
      els.inspSegGrid.innerHTML = `<div class="seg-placeholder">Live download parts appear here while downloading</div>`;
      els.inspSpeedNow.textContent = "0.00 MB/s";
      els.inspThumbWrap.classList.remove("has-img");
      drawSpeedChart([]);
      return;
    }

    const modeLabels = {
      direct: "MULTI-PART FAST ENGINE",
      video_turbo: "FAST VIDEO + AUDIO MERGE",
      video_ytdlp: "SMART VIDEO STREAM",
    };
    els.inspModePill.textContent = modeLabels[item.mode] || "DOWNLOAD DETAILS";
    els.inspFilename.textContent = item.filename;
    els.inspMetaRight.textContent = `${formatBytes(item.downloaded_bytes)} / ${formatBytes(item.total_bytes)} (${Number(item.progress || 0).toFixed(1)}%)`;

    if (item.thumbnail) {
      els.inspThumb.src = item.thumbnail;
      els.inspThumbWrap.classList.add("has-img");
    } else {
      els.inspThumbWrap.classList.remove("has-img");
      els.inspThumbFallback.textContent = item.category === "video" ? "▶" : "⬇";
    }

    els.inspUrl.textContent = item.url;
    els.inspUrl.title = item.url;
    els.inspPath.textContent = state.cloudMode
      ? (item.status === "completed" ? "Ready — Saved to your browser (click Save to download again)" : "Preparing on server, will auto-save to your device...")
      : (item.filepath || item.save_dir);
    els.inspPath.title = state.cloudMode ? "" : (item.filepath || item.save_dir);
    els.inspStatus.textContent = item.error_message ? `${item.status_text}` : item.status_text;
    els.inspResume.textContent = item.supports_resume ? "Yes (Pause & Resume Supported)" : "Standard Stream";
    els.inspSpeedNow.textContent = formatSpeed(item.speed);

    // Render parallel segment bars
    const segs = item.segments || [];
    if (segs.length === 0) {
      els.inspSegSummary.textContent = item.status === "completed" ? "Completed" : `${item.connections} parts ready`;
      els.inspSegGrid.innerHTML = `<div class="seg-placeholder">${
        item.status === "completed"
          ? "Download verified and saved safely to your computer."
          : "Waiting for download parts..."
      }</div>`;
    } else {
      const doneCnt = segs.filter((s) => s.status === "done").length;
      const activeCnt = segs.filter((s) => s.status === "active").length;
      els.inspSegSummary.textContent = `${segs.length} parts (${activeCnt} active, ${doneCnt} done)`;

      els.inspSegGrid.innerHTML = segs.slice(0, 24).map((s) => {
        const total = s.end >= s.start ? (s.end - s.start + 1) : 1;
        const pct = Math.min(100, Math.max(0, (s.downloaded / total) * 100));
        const sp = s.status === "active" && s.speed > 0 ? formatSpeed(s.speed) : (s.status === "done" ? "100%" : `${pct.toFixed(0)}%`);
        return `
          <div class="seg-chip">
            <div class="seg-chip-top">
              <span>#${s.index + 1}</span>
              <span>${sp}</span>
            </div>
            <div class="seg-bar">
              <div class="seg-bar-fill ${s.status}" style="width:${pct}%"></div>
            </div>
          </div>
        `;
      }).join("");
    }

    drawSpeedChart(item.speed_history || []);
  }

  function drawSpeedChart(history) {
    const canvas = els.speedCanvas;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const isLight = state.theme === "light";

    // Grid lines
    ctx.strokeStyle = isLight ? "rgba(148, 163, 184, 0.28)" : "rgba(51, 70, 102, 0.35)";
    ctx.lineWidth = 1;
    for (let y = 20; y < h; y += 22) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (!history || history.length < 2) return;

    const maxVal = Math.max(...history, 1024 * 256);
    const step = w / Math.max(history.length - 1, 1);

    // Fill area gradient
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    if (isLight) {
      grad.addColorStop(0, "rgba(2, 132, 199, 0.28)");
      grad.addColorStop(1, "rgba(16, 185, 129, 0.03)");
    } else {
      grad.addColorStop(0, "rgba(14, 165, 233, 0.42)");
      grad.addColorStop(1, "rgba(16, 185, 129, 0.02)");
    }

    ctx.beginPath();
    ctx.moveTo(0, h);
    history.forEach((val, idx) => {
      const x = idx * step;
      const y = h - 6 - ((val / maxVal) * (h - 14));
      ctx.lineTo(x, y);
    });
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line stroke
    ctx.beginPath();
    history.forEach((val, idx) => {
      const x = idx * step;
      const y = h - 6 - ((val / maxVal) * (h - 14));
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = isLight ? "#0284c7" : "#38bdf8";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // =========================================================================
  // Add URL & Smart Probe Modal
  // =========================================================================
  function openModal(id) { $(id).classList.remove("hidden"); }
  function closeModal(id) { $(id).classList.add("hidden"); }

  function openAddModal(prefillUrl = "", autoAnalyze = false) {
    state.probeData = null;
    state.selectedFormatIdx = 0;
    els.modalUrlInput.value = prefillUrl;
    els.probeLoading.classList.add("hidden");
    els.probeError.classList.add("hidden");
    els.probeResultBox.classList.add("hidden");
    els.modalAddPausedBtn.classList.add("hidden");
    els.modalStartBtn.disabled = true;
    els.modalStartBtn.textContent = "Start Download";
    els.modalSaveDirInput.value = state.settings.download_dir || "";
    els.modalConnSlider.value = state.settings.default_connections || 12;
    els.modalConnLabel.textContent = els.modalConnSlider.value;

    openModal("addModal");
    els.modalUrlInput.focus();
    if (prefillUrl && autoAnalyze) {
      runProbe();
    }
  }

  async function runProbe() {
    const url = els.modalUrlInput.value.trim();
    if (!url) {
      toast("Paste a video or file link first.", "error");
      return;
    }

    state.probeData = null;
    els.probeError.classList.add("hidden");
    els.probeResultBox.classList.add("hidden");
    els.probeLoading.classList.remove("hidden");
    els.modalStartBtn.disabled = true;
    els.modalAnalyzeBtn.disabled = true;

    try {
      const data = await api("/api/probe", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      state.probeData = data;
      state.selectedFormatIdx = 0;
      renderProbeResult(data);
    } catch (err) {
      els.probeError.textContent = err.message || "Could not check this link.";
      els.probeError.classList.remove("hidden");
    } finally {
      els.probeLoading.classList.add("hidden");
      els.modalAnalyzeBtn.disabled = false;
    }
  }

  function renderProbeResult(data) {
    els.probeResultBox.classList.remove("hidden");
    els.modalStartBtn.disabled = false;

    els.previewTitle.textContent = data.title || data.filename;
    els.previewExtractor.textContent = data.extractor || (data.kind === "direct" ? "Direct File" : "Video");
    els.previewSizeBadge.textContent = data.filesize ? `Size: ${formatBytes(data.filesize)}` : "Size: Auto";

    if (data.supports_range) {
      els.previewRangeBadge.classList.remove("hidden");
    } else {
      els.previewRangeBadge.classList.add("hidden");
    }

    if (data.thumbnail) {
      els.previewThumb.src = data.thumbnail;
      els.previewThumbBox.classList.add("has-img");
    } else {
      els.previewThumbBox.classList.remove("has-img");
      els.previewThumbIcon.textContent = data.kind === "direct" ? "⬇" : "▶";
    }

    if (data.duration) {
      els.previewDuration.textContent = formatDuration(data.duration);
      els.previewDuration.classList.remove("hidden");
    } else {
      els.previewDuration.classList.add("hidden");
    }

    const subParts = [];
    if (data.uploader) subParts.push(`By ${data.uploader}`);
    if (data.warning) subParts.push(data.warning);
    els.previewSub.textContent = subParts.join(" • ") || data.final_url;

    // Playlist vs Single Download
    if (data.kind === "playlist" && data.playlist_entries && data.playlist_entries.length > 0) {
      els.formatsSection.classList.add("hidden");
      els.singleOptionsGrid.classList.add("hidden");
      els.modalAddPausedBtn.classList.add("hidden");
      els.playlistSection.classList.remove("hidden");
      els.playlistTitleLabel.textContent = `Playlist Videos (${data.playlist_count} found)`;
      els.modalStartBtn.textContent = `Download Selected Playlist Videos`;

      els.playlistList.innerHTML = data.playlist_entries.map((entry, idx) => `
        <label class="playlist-row">
          <input type="checkbox" class="pl-check" data-url="${escHtml(entry.url)}" checked />
          <span class="mono muted">${String(idx + 1).padStart(2, "0")}.</span>
          <span class="truncate" style="flex:1">${escHtml(entry.title)}</span>
          <span class="mono muted small">${formatDuration(entry.duration)}</span>
        </label>
      `).join("");
      return;
    }

    els.playlistSection.classList.add("hidden");
    els.singleOptionsGrid.classList.remove("hidden");
    els.modalAddPausedBtn.classList.remove("hidden");
    els.modalStartBtn.textContent = "Start Download";
    els.modalFilenameInput.value = data.filename || "";

    if (data.formats && data.formats.length > 0) {
      els.formatsSection.classList.remove("hidden");
      renderFormatCards(data.formats);
    } else {
      els.formatsSection.classList.add("hidden");
    }
  }

  function renderFormatCards(formats) {
    els.formatList.innerHTML = formats.map((f, idx) => {
      const sel = idx === state.selectedFormatIdx ? "selected" : "";
      const turboTag = f.direct_video_url ? "⚡ Fast Boost" : "Standard";
      const sizeStr = f.filesize ? formatBytes(f.filesize) : "Auto";
      return `
        <button type="button" class="format-card ${sel}" data-fmt-idx="${idx}">
          <div style="min-width:0">
            <div class="format-card-title truncate">${escHtml(f.label)}</div>
            <div class="format-card-sub">${escHtml(f.ext.toUpperCase())} • ${turboTag}</div>
          </div>
          <div class="format-card-size">${sizeStr}</div>
        </button>
      `;
    }).join("");

    els.formatList.querySelectorAll(".format-card").forEach((card) => {
      card.addEventListener("click", () => {
        state.selectedFormatIdx = Number(card.dataset.fmtIdx);
        const chosen = formats[state.selectedFormatIdx];
        if (chosen) {
          // Update filename extension automatically when switching e.g. MP4 <-> MP3
          const curName = els.modalFilenameInput.value.trim();
          if (curName && chosen.ext) {
            const base = curName.replace(/\.[^.]+$/, "");
            els.modalFilenameInput.value = `${base}.${chosen.ext}`;
          }
          els.previewSizeBadge.textContent = chosen.filesize
            ? `Size: ${formatBytes(chosen.filesize)}`
            : "Size: Auto";
        }
        renderFormatCards(formats);
      });
    });
  }

  async function submitDownloadFromModal(startImmediately = true) {
    const data = state.probeData;
    if (!data) return;

    // Playlist batch submit
    if (data.kind === "playlist") {
      const checked = Array.from(els.playlistList.querySelectorAll(".pl-check:checked"))
        .map((cb) => cb.dataset.url)
        .filter(Boolean);
      if (checked.length === 0) {
        toast("Select at least one video from the playlist.", "error");
        return;
      }
      try {
        const res = await api("/api/downloads/batch", {
          method: "POST",
          body: JSON.stringify({
            urls: checked,
            save_dir: els.modalSaveDirInput.value.trim() || state.settings.download_dir,
            connections: Number(els.modalConnSlider.value),
            quality: els.playlistQualitySelect.value,
          }),
        });
        closeModal("addModal");
        toast(`Added ${res.count} playlist videos to queue!`, "ok");
      } catch (err) {
        toast(err.message, "error");
      }
      return;
    }

    // Single file or video submit
    const fmt = (data.formats && data.formats[state.selectedFormatIdx]) || null;
    const payload = {
      url: data.kind === "direct" ? data.final_url : data.url,
      save_dir: els.modalSaveDirInput.value.trim() || state.settings.download_dir,
      filename: els.modalFilenameInput.value.trim() || data.filename,
      connections: Number(els.modalConnSlider.value),
      format_id: fmt ? fmt.format_id : "bestvideo+bestaudio/best",
      format_label: fmt ? fmt.label : "",
      is_audio_only: fmt ? Boolean(fmt.is_audio_only) : false,
      audio_format: fmt && fmt.is_audio_only ? (fmt.ext || "mp3") : "mp3",
      kind: data.kind,
      title: data.title,
      thumbnail: data.thumbnail,
      duration: data.duration,
      extractor: data.extractor,
      filesize: (fmt && fmt.filesize) || data.filesize || 0,
      supports_range: data.supports_range,
      direct_video_url: fmt ? fmt.direct_video_url : null,
      direct_audio_url: fmt ? fmt.direct_audio_url : null,
      http_headers: (fmt && fmt.http_headers && Object.keys(fmt.http_headers).length > 0)
        ? fmt.http_headers
        : (data.http_headers || {}),
      start_immediately: startImmediately,
    };

    try {
      await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      closeModal("addModal");
      els.quickUrlInput.value = "";
      toast(startImmediately ? "Download started!" : "Saved to paused list.", "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function instantTurboGrab(rawUrl) {
    const url = (rawUrl || "").trim();
    if (!url) {
      toast("Paste a video or file link first.", "error");
      return;
    }
    try {
      await api("/api/downloads", {
        method: "POST",
        body: JSON.stringify({
          url,
          kind: null, // Backend auto-probes and immediately starts at Best Quality!
          start_immediately: true,
        }),
      });
      els.quickUrlInput.value = "";
      toast("Quick Save started — downloading best quality automatically!", "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // =========================================================================
  // Actions
  // =========================================================================
  async function performAction(id, action) {
    const item = state.items.get(id);
    if (!item) return;

    try {
      if (action === "pause") {
        await api(`/api/downloads/${id}/pause`, { method: "POST" });
      } else if (action === "resume") {
        const ep = item.status === "error" || item.status === "cancelled" ? "retry" : "resume";
        await api(`/api/downloads/${id}/${ep}`, { method: "POST" });
      } else if (action === "save-device" || (action === "open" && state.cloudMode)) {
        triggerDeviceDownload(item);
      } else if (action === "open") {
        await api(`/api/downloads/${id}/open`, { method: "POST" });
      } else if (action === "reveal") {
        if (state.cloudMode) {
          performAction(id, "save-device");
          return;
        }
        await api(`/api/downloads/${id}/reveal`, { method: "POST" });
      } else if (action === "copy-url") {
        await navigator.clipboard.writeText(item.url);
        toast("Link copied to clipboard.", "info");
      } else if (action === "delete") {
        state.deleteTargetId = id;
        els.deleteConfirmText.textContent = `Remove "${item.filename}" from TurboDM?`;
        els.deleteFromDiskCheck.checked = false;
        openModal("deleteModal");
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function browseFolderInto(inputEl) {
    try {
      const res = await api("/api/system/browse-folder", {
        method: "POST",
        body: JSON.stringify({ initial_dir: inputEl.value.trim() || state.settings.download_dir }),
      });
      if (res.path) inputEl.value = res.path;
    } catch (err) {
      toast("Could not open folder picker.", "error");
    }
  }

  async function pasteClipboardInto(inputEl, autoAnalyze = false) {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) {
        toast("Your clipboard is empty.", "info");
        return;
      }
      inputEl.value = text;
      inputEl.focus();
      if (autoAnalyze) runProbe();
    } catch {
      toast("Press Ctrl+V to paste your link.", "info");
      inputEl.focus();
    }
  }

  // =========================================================================
  // Event Bindings
  // =========================================================================
  function bindEvents() {
    // Theme toggle & Help & Collapsible Inspector
    if (els.themeToggleBtn) {
      els.themeToggleBtn.addEventListener("click", () => {
        applyTheme(state.theme === "light" ? "dark" : "light", true);
      });
    }
    if (els.btnHelp) {
      els.btnHelp.addEventListener("click", () => openModal("helpModal"));
    }
    if (els.inspToggleBtn) {
      els.inspToggleBtn.addEventListener("click", () => {
        applyInspectorState(!state.inspectorCollapsed, true);
      });
    }

    // Quick top bar
    els.quickPasteBtn.addEventListener("click", () => pasteClipboardInto(els.quickUrlInput));
    els.quickAnalyzeBtn.addEventListener("click", () => {
      const u = els.quickUrlInput.value.trim();
      openAddModal(u, Boolean(u));
    });
    els.quickInstantBtn.addEventListener("click", () => instantTurboGrab(els.quickUrlInput.value));
    els.quickUrlInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const u = els.quickUrlInput.value.trim();
        if (u) openAddModal(u, true);
      }
    });

    // Toolbar
    els.btnAddUrl.addEventListener("click", () => openAddModal("", false));
    els.emptyAddBtn.addEventListener("click", () => openAddModal("", false));
    els.btnBatchUrl.addEventListener("click", () => {
      els.batchSaveDirInput.value = state.settings.download_dir || "";
      els.batchUrlsInput.value = "";
      openModal("batchModal");
      els.batchUrlsInput.focus();
    });
    els.emptyBatchBtn.addEventListener("click", () => els.btnBatchUrl.click());

    els.btnResumeSel.addEventListener("click", () => state.selectedId && performAction(state.selectedId, "resume"));
    els.btnPauseSel.addEventListener("click", () => state.selectedId && performAction(state.selectedId, "pause"));
    els.btnDeleteSel.addEventListener("click", () => state.selectedId && performAction(state.selectedId, "delete"));

    els.btnPauseAll.addEventListener("click", async () => {
      await api("/api/downloads/actions/pause-all", { method: "POST" });
      toast("Paused all active downloads.", "info");
    });

    els.btnResumeAll.addEventListener("click", async () => {
      await api("/api/downloads/actions/resume-all", { method: "POST" });
      toast("Resumed queued downloads.", "ok");
    });

    els.btnClearCompleted.addEventListener("click", async () => {
      const res = await api("/api/downloads/actions/clear-completed", { method: "POST" });
      toast(res.cleared.length ? `Cleared ${res.cleared.length} finished item(s).` : "No finished items to clear.", "info");
    });

    els.btnOpenFolder.addEventListener("click", async () => {
      await api("/api/system/open-folder", { method: "POST", body: JSON.stringify({}) });
    });

    els.btnSettings.addEventListener("click", () => {
      const s = state.settings;
      els.setDownloadDir.value = s.download_dir || "";
      els.setConnections.value = s.default_connections;
      els.setMaxConcurrent.value = s.max_concurrent_downloads;
      els.setYtdlpFrags.value = s.ytdlp_concurrent_fragments;
      els.setSpeedLimit.value = s.speed_limit_kbps;
      els.setCookiesFile.value = s.cookies_file || "";
      els.setTurboVideo.checked = Boolean(s.enable_turbo_video_segments);
      els.setAutoCategorize.checked = Boolean(s.auto_categorize_folders);
      openModal("settingsModal");
    });

    // Search & Sidebar Filters
    els.searchInput.addEventListener("input", (e) => {
      state.search = e.target.value.trim();
      renderAll();
    });

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.filter = btn.dataset.filter;
        renderAll();
      });
    });

    // Table clicks & context menu
    els.downloadTableBody.addEventListener("click", (e) => {
      const actBtn = e.target.closest("[data-row-act]");
      if (actBtn) {
        e.stopPropagation();
        performAction(actBtn.dataset.id, actBtn.dataset.rowAct);
        return;
      }
      const tr = e.target.closest("tr[data-id]");
      if (tr) {
        state.selectedId = tr.dataset.id;
        renderAll();
      }
    });

    els.downloadTableBody.addEventListener("dblclick", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      const item = state.items.get(tr.dataset.id);
      if (item && item.status === "completed") {
        performAction(item.id, "open");
      }
    });

    els.downloadTableBody.addEventListener("contextmenu", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      e.preventDefault();
      state.selectedId = tr.dataset.id;
      renderAll();
      els.contextMenu.style.left = `${Math.min(e.clientX, window.innerWidth - 190)}px`;
      els.contextMenu.style.top = `${Math.min(e.clientY, window.innerHeight - 210)}px`;
      els.contextMenu.classList.remove("hidden");
    });

    els.contextMenu.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (btn && state.selectedId) {
        performAction(state.selectedId, btn.dataset.action);
      }
      els.contextMenu.classList.add("hidden");
    });

    document.addEventListener("click", () => els.contextMenu.classList.add("hidden"));

    // Add Modal Controls
    els.modalPasteBtn.addEventListener("click", () => pasteClipboardInto(els.modalUrlInput, true));
    els.modalAnalyzeBtn.addEventListener("click", runProbe);
    els.modalUrlInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") runProbe();
    });
    els.modalConnSlider.addEventListener("input", () => {
      els.modalConnLabel.textContent = els.modalConnSlider.value;
    });
    els.modalBrowseBtn.addEventListener("click", () => browseFolderInto(els.modalSaveDirInput));
    els.modalStartBtn.addEventListener("click", () => submitDownloadFromModal(true));
    els.modalAddPausedBtn.addEventListener("click", () => submitDownloadFromModal(false));

    els.plSelectAll.addEventListener("click", () => {
      els.playlistList.querySelectorAll(".pl-check").forEach((c) => (c.checked = true));
    });
    els.plSelectNone.addEventListener("click", () => {
      els.playlistList.querySelectorAll(".pl-check").forEach((c) => (c.checked = false));
    });

    // Batch Modal
    els.batchBrowseBtn.addEventListener("click", () => browseFolderInto(els.batchSaveDirInput));
    els.batchStartBtn.addEventListener("click", async () => {
      const urls = els.batchUrlsInput.value
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (urls.length === 0) {
        toast("Enter at least one link.", "error");
        return;
      }
      try {
        const res = await api("/api/downloads/batch", {
          method: "POST",
          body: JSON.stringify({
            urls,
            save_dir: els.batchSaveDirInput.value.trim() || state.settings.download_dir,
            quality: els.batchQualitySelect.value,
          }),
        });
        closeModal("batchModal");
        toast(`Added ${res.count} download(s) to queue!`, "ok");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    // Settings Modal
    els.setBrowseBtn.addEventListener("click", () => browseFolderInto(els.setDownloadDir));
    els.saveSettingsBtn.addEventListener("click", async () => {
      const payload = {
        download_dir: els.setDownloadDir.value.trim() || state.settings.download_dir,
        default_connections: Number(els.setConnections.value) || 12,
        max_concurrent_downloads: Number(els.setMaxConcurrent.value) || 3,
        ytdlp_concurrent_fragments: Number(els.setYtdlpFrags.value) || 16,
        speed_limit_kbps: Number(els.setSpeedLimit.value) || 0,
        preferred_video_quality: state.settings.preferred_video_quality || "best",
        cookies_file: els.setCookiesFile.value.trim(),
        enable_turbo_video_segments: els.setTurboVideo.checked,
        auto_categorize_folders: els.setAutoCategorize.checked,
      };
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify(payload) });
        closeModal("settingsModal");
        toast("Settings saved!", "ok");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    // Delete Confirm Modal
    els.confirmDeleteBtn.addEventListener("click", async () => {
      if (!state.deleteTargetId) return;
      const delDisk = els.deleteFromDiskCheck.checked;
      try {
        await api(`/api/downloads/${state.deleteTargetId}?delete_file=${delDisk}`, { method: "DELETE" });
        closeModal("deleteModal");
        toast(delDisk ? "Removed and deleted file from computer." : "Removed from list.", "info");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    // Close modals via X / Cancel / Backdrop / Esc
    document.querySelectorAll("[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => closeModal(btn.dataset.close));
    });
    document.querySelectorAll(".modal-backdrop").forEach((bd) => {
      bd.addEventListener("click", (e) => {
        if (e.target === bd) bd.classList.add("hidden");
      });
    });

    // Drag & Drop URLs onto window
    els.dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      els.dropZone.classList.add("drag-over");
    });
    els.dropZone.addEventListener("dragleave", () => els.dropZone.classList.remove("drag-over"));
    els.dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      els.dropZone.classList.remove("drag-over");
      const text = (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "").trim();
      if (text) {
        const firstLine = text.split(/\r?\n/)[0].trim();
        openAddModal(firstLine, true);
      }
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal-backdrop").forEach((m) => m.classList.add("hidden"));
        els.contextMenu.classList.add("hidden");
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        openAddModal("", false);
      } else if (e.key === "Delete" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
        if (state.selectedId) performAction(state.selectedId, "delete");
      }
    });
  }

  // Boot
  initUiPreferences();
  bindEvents();
  connectWebSocket();
})();
