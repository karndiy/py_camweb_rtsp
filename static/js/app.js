'use strict';

// ── Toast notifications ───────────────────────────────────────────────────────
function toast(msg, type = 'info', duration = 3000) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('removing');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }, duration);
}

// ── Clock ─────────────────────────────────────────────────────────────────────
(function tickClock() {
  const n = new Date();
  document.getElementById('clock').textContent =
    n.toISOString().replace('T', ' ').slice(0, 19);
  setTimeout(tickClock, 1000);
})();

// ── State ─────────────────────────────────────────────────────────────────────
let rtspActive    = false;
let snapLevel     = 'low';
let snapBusy      = false;
let _savedRelays  = [];
let currentZoom   = 1.0;
let currentAfMode = 'continuous';
let flipActive    = false;
let mirrorActive  = false;
let overlayActive = true;
const ZOOM_PRESETS = [1, 1.5, 2, 3, 4, 6, 8];

// ── Accordion ─────────────────────────────────────────────────────────────────
document.querySelectorAll('.acc-header').forEach(header => {
  header.addEventListener('click', () => {
    header.closest('.accordion').classList.toggle('open');
  });
});

// ── Fullscreen ────────────────────────────────────────────────────────────────
const previewWrap = document.getElementById('preview-wrap');

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    previewWrap.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen().catch(() => {});
  }
}

document.getElementById('btn-fullscreen').addEventListener('click', toggleFullscreen);
document.addEventListener('keydown', e => {
  if ((e.key === 'f' || e.key === 'F') && !e.ctrlKey && !e.metaKey) {
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) return;
    toggleFullscreen();
  }
});

// ── Stream status ─────────────────────────────────────────────────────────────
const streamImg     = document.getElementById('stream-img');
const streamOverlay = document.getElementById('stream-overlay');
const streamBadge   = document.getElementById('stream-badge');

streamImg.addEventListener('load', () => {
  streamOverlay.classList.add('hidden');
  streamBadge.className   = 'stream-badge live';
  streamBadge.textContent = '● LIVE';
});

streamImg.addEventListener('error', () => {
  streamOverlay.classList.remove('hidden');
  streamBadge.className   = 'stream-badge offline';
  streamBadge.textContent = '● OFFLINE';
  setTimeout(() => {
    streamImg.src = '/stream?t=' + Date.now();
  }, 2000);
});

// ── Status poll ───────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    updateUI(d);
  } catch (e) {}
  setTimeout(pollStatus, 2000);
}

function updateUI(d) {
  document.getElementById('st-fps').textContent  = d.fps;
  document.getElementById('st-jpeg').textContent = d.jpeg_kb;
  document.getElementById('st-res').textContent  = d.resolution || '—';
  document.getElementById('st-tx').textContent   = d.local_rtsp ? d.local_rtsp.tx_kbps : '—';
  document.getElementById('viewers-info').textContent =
    d.viewers + ' viewer' + (d.viewers !== 1 ? 's' : '');

  // RTSP button state
  rtspActive = !!d.local_rtsp;
  const btn = document.getElementById('btn-rtsp');
  if (rtspActive) {
    btn.textContent = '◉  STOP RTSP';
    btn.className   = 'btn btn-rtsp-on';
    document.getElementById('rtsp-url').textContent = d.local_rtsp.url;
  } else {
    btn.textContent = '◉  START RTSP';
    btn.className   = 'btn btn-rtsp-off';
    document.getElementById('rtsp-url').textContent = '';
  }

  // Stream badges
  document.getElementById('badge-tl').textContent =
    (d.local_rtsp ? 'RTSP' : 'MJPEG') + '  ' + (d.resolution || '');
  document.getElementById('badge-tr').textContent = d.fps + ' fps';
  document.getElementById('badge-br').textContent = d.jpeg_kb + ' kB / frame';

  // Snapshot status pills
  const zl = parseFloat(d.zoom_level) || 1.0;
  const zoomPill = document.getElementById('snap-zoom-pill');
  if (zl > 1.0) {
    zoomPill.textContent   = 'ZOOM ' + zl.toFixed(1) + '×';
    zoomPill.style.display = 'inline';
  } else {
    zoomPill.style.display = 'none';
  }
  document.getElementById('snap-af-pill').textContent = 'AF:' + (d.af_mode || '—');

  // Camera source buttons
  const camIdx = d.camera_index ?? 0;
  [0, 1].forEach(i =>
    document.getElementById('cam-src-' + i)?.classList.toggle('active', i === camIdx));
  const lbl = document.getElementById('cam-name-lbl');
  if (lbl) lbl.textContent = d.camera_name || '';

  // Relays
  renderRelays(d.relays || []);
  updateSavedRelays(d.config && d.config.saved_relays);

  // Snapshot busy
  snapBusy = d.snap_busy;
  document.getElementById('btn-snap').disabled = snapBusy;

  // Log
  renderLog(d.log || []);

  // Tunnel
  if (d.tunnel) renderTunnel(d.tunnel);

  // Apply saved config once on first load
  if (d.config && !window._cfgApplied) {
    window._cfgApplied = true;
    applyConfig(d.config);
  }
}

function applyConfig(c) {
  document.querySelectorAll('input[name=res]').forEach(r => { r.checked = r.value === c.resolution; });
  document.querySelectorAll('input[name=fps]').forEach(r => { r.checked = r.value === c.fps; });
  document.getElementById('inp-bitrate').value   = c.bitrate || '2000';
  document.getElementById('chk-reconnect').checked = !!c.auto_reconnect;

  const zl = parseFloat(c.zoom_level) || 1.0;
  currentZoom = zl;
  const zoomSlider = document.getElementById('zoom-slider');
  zoomSlider.value = Math.round(zl * 10);
  updateZoomDisplay(zoomSlider.value);

  if (c.af_mode) {
    currentAfMode = c.af_mode;
    setAfModeUI(c.af_mode);
    if (c.af_mode === 'manual' && c.lens_position !== undefined) {
      const sv = Math.round(parseFloat(c.lens_position) * 10);
      document.getElementById('lens-slider').value   = sv;
      document.getElementById('lens-val').textContent = parseFloat(c.lens_position).toFixed(1);
    }
  }

  if (c.camera_index !== undefined) {
    [0, 1].forEach(i =>
      document.getElementById('cam-src-' + i)?.classList.toggle('active', i === c.camera_index));
  }

  flipActive    = !!c.flip;
  mirrorActive  = !!c.mirror;
  overlayActive = c.show_overlay !== undefined ? !!c.show_overlay : true;
  document.getElementById('btn-flip').classList.toggle('active', flipActive);
  document.getElementById('btn-mirror').classList.toggle('active', mirrorActive);
  document.getElementById('btn-overlay').classList.toggle('active', overlayActive);

  if (c.last_relay_url) {
    const el = document.getElementById('relay-url');
    if (!el.value) el.value = c.last_relay_url;
  }

  if (c.hw_poll_interval !== undefined)
    document.getElementById('cfg-hw-poll').value = c.hw_poll_interval;
  if (c.hw_cpu_warn !== undefined)
    document.getElementById('cfg-hw-cpu-warn').value = c.hw_cpu_warn;
  if (c.hw_ram_warn !== undefined)
    document.getElementById('cfg-hw-ram-warn').value = c.hw_ram_warn;
  if (c.hw_temp_warn !== undefined)
    document.getElementById('cfg-hw-temp-warn').value = c.hw_temp_warn;
}

// ── Camera source ─────────────────────────────────────────────────────────────
[0, 1].forEach(i => {
  document.getElementById('cam-src-' + i).addEventListener('click', async () => {
    [0, 1].forEach(j =>
      document.getElementById('cam-src-' + j).classList.toggle('active', j === i));
    try {
      await fetch('/api/camera', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index: i })
      });
    } catch (e) {}
  });
});

// ── RTSP toggle ───────────────────────────────────────────────────────────────
document.getElementById('btn-rtsp').addEventListener('click', async () => {
  const btn = document.getElementById('btn-rtsp');
  btn.disabled = true;
  if (!rtspActive) {
    const res = document.querySelector('input[name=res]:checked')?.value || '720p';
    const fps = document.querySelector('input[name=fps]:checked')?.value || '30';
    const bit = document.getElementById('inp-bitrate').value || '2000';
    const arc = document.getElementById('chk-reconnect').checked;
    btn.textContent = '◉  STARTING…';
    try {
      const r = await fetch('/api/rtsp/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: res, fps, bitrate: bit, auto_reconnect: arc })
      });
      const d = await r.json();
      if (!d.ok) toast('RTSP start failed: ' + d.error, 'error');
    } catch (e) {
      toast('RTSP request failed', 'error');
    }
  } else {
    btn.textContent = '◉  STOPPING…';
    try { await fetch('/api/rtsp/stop', { method: 'POST' }); } catch (e) {}
  }
  btn.disabled = false;
});

// ── Relay: saved configs ───────────────────────────────────────────────────────
function updateSavedRelays(saved) {
  _savedRelays = saved || [];
  const sel  = document.getElementById('saved-relay-select');
  const prev = sel.value;
  sel.innerHTML = '<option value="">— load saved —</option>';
  _savedRelays.forEach((r, i) => {
    const opt = document.createElement('option');
    opt.value       = String(i);
    opt.textContent = r.target_url + '  [' + (r.fps || 30) + 'fps  ' + (r.bitrate || 2000) + 'kbps]';
    sel.appendChild(opt);
  });
  if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
}

document.getElementById('saved-relay-select').addEventListener('change', function () {
  const idx = this.value;
  if (!idx) return;
  const r = _savedRelays[parseInt(idx)];
  if (!r) return;
  document.getElementById('relay-url').value         = r.target_url || '';
  document.getElementById('relay-fps').value         = String(r.fps || 30);
  document.getElementById('relay-bitrate').value     = r.bitrate || 2000;
  document.getElementById('relay-reconnect').checked = !!r.auto_reconnect;
  this.value = '';
});

// ── Relay: add / remove ───────────────────────────────────────────────────────
document.getElementById('btn-add-relay').addEventListener('click', async () => {
  let url = document.getElementById('relay-url').value.trim();
  if (!url) { toast('Enter target RTSP URL', 'warn'); return; }
  if (!/^rtsp[s]?:\/\//i.test(url)) {
    url = 'rtsp://' + url;
    document.getElementById('relay-url').value = url;
  }
  const fps = parseInt(document.getElementById('relay-fps').value);
  const bit = parseInt(document.getElementById('relay-bitrate').value) || 2000;
  const arc = document.getElementById('relay-reconnect').checked;
  try {
    const r = await fetch('/api/relay/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_url: url, fps, bitrate: bit, auto_reconnect: arc })
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('relay-url').value = '';
      toast('Relay added' + (d.saved ? ' & config saved' : ''), 'success');
    } else {
      toast('Relay failed: ' + d.error, 'error');
    }
  } catch (e) {
    toast('Relay request failed', 'error');
  }
});

async function removeRelay(id) {
  try { await fetch('/api/relay/remove/' + id, { method: 'DELETE' }); } catch (e) {}
}

function renderRelays(relays) {
  const box = document.getElementById('relay-list');
  if (!relays.length) {
    box.innerHTML = '<div class="label-dim">No active relays</div>';
    return;
  }
  // preserve remove-button listeners by rebuilding only changed items
  box.innerHTML = '';
  relays.forEach(r => {
    const div = document.createElement('div');
    div.className = 'relay-item';
    const stateExtra = r.state === 'live' ? '  ' + r.tx_kbps + ' kbps' : '';
    div.innerHTML =
      '<div class="relay-url">' + escHtml(r.target_url) + '</div>' +
      '<div class="relay-state ' + r.state + '">' + r.state + stateExtra + '</div>' +
      '<button class="relay-del" title="Remove">✕</button>';
    div.querySelector('.relay-del').addEventListener('click', () => removeRelay(r.id));
    box.appendChild(div);
  });
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Snapshot ──────────────────────────────────────────────────────────────────
['low', 'mid', 'high'].forEach(level => {
  document.getElementById('snap-' + level).addEventListener('click', () => {
    snapLevel = level;
    ['low', 'mid', 'high'].forEach(k =>
      document.getElementById('snap-' + k).classList.toggle('active', k === level));
  });
});

document.getElementById('btn-snap').addEventListener('click', async () => {
  if (snapBusy) return;
  const btn = document.getElementById('btn-snap');
  const res = document.getElementById('snap-result');
  btn.disabled    = true;
  res.textContent = 'Capturing…';
  try {
    const r = await fetch('/api/snapshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: snapLevel })
    });
    const d  = await r.json();
    const sr = d.snap_result || {};
    if (sr.ok && sr.file) {
      res.innerHTML = '✓ <a href="/snaps/' + escHtml(sr.file) +
        '" style="color:var(--accent)" target="_blank">' + escHtml(sr.file) + '</a>';
      toast('Snapshot saved: ' + sr.file, 'success');
    } else if (sr.ok === false && sr.error) {
      res.textContent = '✗ ' + sr.error;
      toast('Snapshot failed', 'error');
    } else {
      res.textContent = '✓ Done';
    }
  } catch (e) {
    res.textContent = '✗ Request failed';
    toast('Snapshot request failed', 'error');
  }
});

// ── Zoom ──────────────────────────────────────────────────────────────────────
function updateZoomDisplay(sliderVal) {
  const z = (parseInt(sliderVal) / 10).toFixed(1);
  document.getElementById('zoom-slider-val').textContent = z + '×';
  document.getElementById('zoom-badge').textContent      = z + '×';
  ZOOM_PRESETS.forEach((p, i) => {
    document.querySelectorAll('#zoom-presets .preset-btn')[i]
      ?.classList.toggle('active', Math.abs(p - parseFloat(z)) < 0.05);
  });
}

const zoomSlider = document.getElementById('zoom-slider');
zoomSlider.addEventListener('input', function () { updateZoomDisplay(this.value); });
zoomSlider.addEventListener('change', async function () {
  currentZoom = parseInt(this.value) / 10;
  try {
    await fetch('/api/zoom', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: currentZoom })
    });
  } catch (e) {}
});

document.querySelectorAll('#zoom-presets .preset-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const level = parseFloat(btn.dataset.zoom);
    currentZoom = level;
    zoomSlider.value = Math.round(level * 10);
    updateZoomDisplay(zoomSlider.value);
    try {
      await fetch('/api/zoom', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level })
      });
    } catch (e) {}
  });
});

// ── Autofocus ─────────────────────────────────────────────────────────────────
function setAfModeUI(mode) {
  ['continuous', 'auto', 'manual'].forEach(m =>
    document.getElementById('af-' + m).classList.toggle('active', m === mode));
  document.getElementById('btn-trigger-af').style.display = mode === 'auto'   ? 'block' : 'none';
  document.getElementById('lens-pos-wrap').style.display  = mode === 'manual' ? 'block' : 'none';
}

['continuous', 'auto', 'manual'].forEach(mode => {
  document.getElementById('af-' + mode).addEventListener('click', async () => {
    currentAfMode = mode;
    setAfModeUI(mode);
    if (mode !== 'manual') {
      try {
        await fetch('/api/focus', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode })
        });
      } catch (e) {}
    }
  });
});

const lensSlider = document.getElementById('lens-slider');
lensSlider.addEventListener('input', function () {
  document.getElementById('lens-val').textContent = (parseInt(this.value) / 10).toFixed(1);
});
lensSlider.addEventListener('change', async function () {
  const pos = parseInt(this.value) / 10;
  document.getElementById('lens-val').textContent = pos.toFixed(1);
  try {
    await fetch('/api/focus', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'manual', lens_position: pos })
    });
  } catch (e) {}
});

document.getElementById('btn-trigger-af').addEventListener('click', async () => {
  const btn = document.getElementById('btn-trigger-af');
  btn.disabled    = true;
  btn.textContent = '⊙ FOCUSING…';
  try {
    await fetch('/api/focus', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'auto' })
    });
  } catch (e) {}
  setTimeout(() => { btn.disabled = false; btn.textContent = '⊙ TRIGGER AF'; }, 3000);
});

// ── Image transforms ──────────────────────────────────────────────────────────
document.getElementById('btn-flip').addEventListener('click', async () => {
  flipActive = !flipActive;
  document.getElementById('btn-flip').classList.toggle('active', flipActive);
  try {
    await fetch('/api/transform', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flip: flipActive })
    });
  } catch (e) {}
});

document.getElementById('btn-mirror').addEventListener('click', async () => {
  mirrorActive = !mirrorActive;
  document.getElementById('btn-mirror').classList.toggle('active', mirrorActive);
  try {
    await fetch('/api/transform', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mirror: mirrorActive })
    });
  } catch (e) {}
});

document.getElementById('btn-overlay').addEventListener('click', async () => {
  overlayActive = !overlayActive;
  document.getElementById('btn-overlay').classList.toggle('active', overlayActive);
  try {
    await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ show_overlay: overlayActive })
    });
  } catch (e) {}
});

// ── Log ───────────────────────────────────────────────────────────────────────
function renderLog(entries) {
  const box = document.getElementById('log-box');
  box.innerHTML = entries.map(e =>
    '<div class="log-' + e.tag + '">[' + e.ts + '] ' + escHtml(e.msg) + '</div>'
  ).join('');
  box.scrollTop = box.scrollHeight;
}

document.getElementById('btn-view-log').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/log/file');
    const d = await r.json();
    const box = document.getElementById('log-box');
    if (!d.ok) { box.innerHTML = '<div class="log-ERR">Error loading log</div>'; return; }
    box.innerHTML = d.lines.length
      ? d.lines.map(l => '<div class="log-INF">' + escHtml(l) + '</div>').join('')
      : '<div class="log-INF">Log file is empty</div>';
    box.scrollTop = box.scrollHeight;
  } catch (e) {}
});

document.getElementById('btn-clear-log').addEventListener('click', async () => {
  if (!confirm('Clear cam_events.log?')) return;
  try {
    await fetch('/api/log/clear', { method: 'POST' });
    document.getElementById('log-box').innerHTML = '<div class="log-INF">Log cleared</div>';
    toast('Log cleared', 'info');
  } catch (e) {}
});

// ── Tunnel ────────────────────────────────────────────────────────────────────
document.getElementById('btn-tunnel-start').addEventListener('click', async () => {
  const provider = document.getElementById('tunnel-provider').value;
  const statusEl = document.getElementById('tunnel-status');
  statusEl.textContent = 'Starting…';
  statusEl.style.color = 'var(--text-dim)';
  document.getElementById('btn-tunnel-start').disabled = true;
  try {
    const r = await fetch('/api/tunnel/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider })
    });
    const d = await r.json();
    if (!d.ok) {
      statusEl.textContent = 'Error: ' + d.error;
      statusEl.style.color = 'var(--red)';
      document.getElementById('btn-tunnel-start').disabled = false;
    }
  } catch (e) {
    statusEl.textContent = 'Request failed';
    statusEl.style.color = 'var(--red)';
    document.getElementById('btn-tunnel-start').disabled = false;
  }
});

document.getElementById('btn-tunnel-stop').addEventListener('click', async () => {
  try { await fetch('/api/tunnel/stop', { method: 'POST' }); } catch (e) {}
});

function renderTunnel(t) {
  if (!t) return;
  const statusEl = document.getElementById('tunnel-status');
  const urlWrap  = document.getElementById('tunnel-url-wrap');
  const urlEl    = document.getElementById('tunnel-url');
  const btnStart = document.getElementById('btn-tunnel-start');
  const btnStop  = document.getElementById('btn-tunnel-stop');
  const colors   = {
    stopped: 'var(--text-dim)', starting: 'var(--orange)',
    running: 'var(--accent)',   error:    'var(--red)'
  };

  statusEl.textContent =
    t.state === 'running'  ? 'Running via ' + t.provider :
    t.state === 'starting' ? 'Starting via ' + t.provider + '…' :
    t.state === 'error'    ? 'Error: ' + (t.error || 'exited') : 'Stopped';
  statusEl.style.color = colors[t.state] || 'var(--text-dim)';

  btnStart.disabled = t.state === 'running' || t.state === 'starting';
  btnStop.disabled  = t.state === 'stopped';

  if (t.url) {
    urlWrap.style.display = 'block';
    urlEl.textContent = t.url;
    urlEl.href        = t.url;
  } else {
    urlWrap.style.display = 'none';
  }
}

document.getElementById('btn-copy-tunnel').addEventListener('click', () => {
  const url = document.getElementById('tunnel-url').textContent;
  navigator.clipboard.writeText(url)
    .then(() => toast('URL copied!', 'success'))
    .catch(() => {});
});

// Populate available providers on load
fetch('/api/tunnel/providers').then(r => r.json()).then(d => {
  const sel = document.getElementById('tunnel-provider');
  Array.from(sel.options).forEach(opt => {
    if (!d.providers.includes(opt.value)) {
      opt.text    += ' (not installed)';
      opt.disabled = true;
    }
  });
  const first = sel.querySelector('option:not([disabled])');
  if (first) sel.value = first.value;
}).catch(() => {});

// ── Settings ──────────────────────────────────────────────────────────────────
document.getElementById('btn-save-config').addEventListener('click', async () => {
  const camIdx = (() => {
    for (let i = 0; i <= 1; i++) {
      if (document.getElementById('cam-src-' + i)?.classList.contains('active')) return i;
    }
    return 0;
  })();
  const cfg = {
    resolution:       document.querySelector('input[name=res]:checked')?.value  || '720p',
    fps:              document.querySelector('input[name=fps]:checked')?.value   || '30',
    bitrate:          document.getElementById('inp-bitrate').value               || '2000',
    auto_reconnect:   document.getElementById('chk-reconnect').checked,
    show_overlay:     overlayActive,
    flip:             flipActive,
    mirror:           mirrorActive,
    zoom_level:       currentZoom,
    af_mode:          currentAfMode,
    lens_position:    parseFloat(document.getElementById('lens-val')?.textContent) || 0,
    camera_index:     camIdx,
    hw_poll_interval: parseInt(document.getElementById('cfg-hw-poll').value)      || 10,
    hw_cpu_warn:      parseInt(document.getElementById('cfg-hw-cpu-warn').value)   || 80,
    hw_ram_warn:      parseInt(document.getElementById('cfg-hw-ram-warn').value)   || 85,
    hw_temp_warn:     parseInt(document.getElementById('cfg-hw-temp-warn').value)  || 75,
  };
  try {
    const r = await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg)
    });
    const d = await r.json();
    if (d.ok) toast('Settings saved', 'success');
    else      toast('Save error', 'error');
  } catch (e) {
    toast('Save failed', 'error');
  }
});

document.getElementById('btn-reset-config').addEventListener('click', async () => {
  if (!confirm('Reset ALL settings to factory defaults?\n(Saved relay URLs are kept.)')) return;
  try {
    const r = await fetch('/api/config/reset', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      window._cfgApplied = false;
      applyConfig(d.config);
      toast('Reset to defaults', 'warn');
    } else {
      toast('Reset failed', 'error');
    }
  } catch (e) {
    toast('Request failed', 'error');
  }
});

// ── Hardware poll ─────────────────────────────────────────────────────────────
async function pollHardware() {
  try {
    const r = await fetch('/api/hardware');
    const d = await r.json();
    const cpuWarn  = parseInt(document.getElementById('cfg-hw-cpu-warn')?.value)  || 80;
    const ramWarn  = parseInt(document.getElementById('cfg-hw-ram-warn')?.value)  || 85;
    const tempWarn = parseInt(document.getElementById('cfg-hw-temp-warn')?.value) || 75;

    if (d.cpu_percent !== undefined) {
      const el = document.getElementById('hw-cpu');
      el.textContent = d.cpu_percent + '%';
      el.style.color = d.cpu_percent >= cpuWarn ? 'var(--red)'
        : d.cpu_percent > cpuWarn * 0.75 ? 'var(--orange)' : 'var(--accent)';
    }
    if (d.temperature_c != null) {
      const el = document.getElementById('hw-temp');
      el.textContent = d.temperature_c + '°C';
      el.style.color = d.temperature_c >= tempWarn ? 'var(--red)'
        : d.temperature_c > tempWarn * 0.8 ? 'var(--orange)' : 'var(--accent)';
    }
    if (d.ram) {
      const el = document.getElementById('hw-ram-pct');
      el.textContent = d.ram.percent + '%';
      el.style.color = d.ram.percent >= ramWarn ? 'var(--red)'
        : d.ram.percent > ramWarn * 0.85 ? 'var(--orange)' : 'var(--accent)';
      document.getElementById('hw-ram-detail').textContent =
        d.ram.used_mb + ' / ' + d.ram.total_mb + ' MB';
    }
    if (d.tasks) {
      document.getElementById('hw-tasks').textContent = d.tasks.running + '/' + d.tasks.total;
    }
  } catch (e) {}
  setTimeout(pollHardware, 5000);
}

// ── Init ──────────────────────────────────────────────────────────────────────
pollStatus();
pollHardware();
