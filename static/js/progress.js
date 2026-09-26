/**
 * progress.js — SSE real-time progress handler untuk semua scraper
 */

let currentJobId = null;
let evtSource = null;

function _elemenJob() {
  return {
    logEl: document.getElementById('scrapeLog'),
    barEl: document.getElementById('progressBar'),
    countEl: document.getElementById('foundCount'),
    statusEl: document.getElementById('statusBadge'),
    dlBtn: document.getElementById('downloadBtn'),
    runBtn: document.getElementById('runBtn'),
    stopBtn: document.getElementById('stopBtn'),
  };
}

function _tombolKembali(runBtn, stopBtn) {
  if (stopBtn) stopBtn.classList.add('d-none');
  if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="bi bi-play-fill me-2"></i>Mulai Scraping'; }
}

/** POST untuk memulai job, lalu sambungkan UI ke progresnya. */
function startJob(endpoint, params, onDone) {
  const { logEl, runBtn, stopBtn } = _elemenJob();
  if (runBtn) { runBtn.disabled = true; runBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Memulai...'; }

  fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(params)
  })
  .then(r => r.json())
  .then(data => {
    if (data.job_id && data.ok !== false) { sambungJob(data.job_id, onDone); return; }
    // Ditolak — mis. scraping jenis yang sama masih berjalan. Kalau server
    // menyebut job-nya, tampilkan job itu alih-alih form yang seolah kosong.
    if (data.job_id) sambungJob(data.job_id, onDone);
    else _tombolKembali(runBtn, stopBtn);
    const el = _elemenJob().logEl;
    if (el) el.innerHTML += `<div class="log-warn">⚠ ${escapeHtml(data.error || 'Tidak bisa memulai.')}</div>`;
  })
  .catch(err => {
    _tombolKembali(runBtn, stopBtn);
    if (logEl) logEl.innerHTML += `<div class="log-err">❌ Gagal memulai: ${escapeHtml(err)}</div>`;
  });
}

/**
 * Sambungkan UI ke job yang sudah berjalan di server. Server mengirim ulang
 * SELURUH log dari awal, jadi halaman yang dibuka ulang setelah pindah menu
 * tampil persis seperti sebelum ditinggalkan.
 */
function sambungJob(jobId, onDone) {
  const { logEl, barEl, countEl, statusEl, dlBtn, runBtn, stopBtn } = _elemenJob();

  if (logEl) logEl.innerHTML = '';
  if (barEl) { barEl.style.width = '0%'; barEl.textContent = '0%'; barEl.className = 'progress-bar progress-bar-striped progress-bar-animated'; }
  if (countEl) countEl.textContent = '0';
  if (statusEl) { statusEl.textContent = 'Berjalan...'; statusEl.className = 'badge bg-warning'; }
  if (dlBtn) dlBtn.classList.add('d-none');
  if (stopBtn) {
    stopBtn.classList.remove('d-none');
    stopBtn.disabled = false;
    stopBtn.innerHTML = '<i class="bi bi-stop-fill me-2"></i>Stop Scraping';
  }
  if (runBtn) { runBtn.disabled = true; runBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Scraping...'; }
  document.getElementById('progressSection')?.classList.remove('d-none');

  if (evtSource) evtSource.close();
  currentJobId = jobId;
  evtSource = new EventSource('/stream/' + jobId);
  evtSource.onmessage = (e) => {
    const d = JSON.parse(e.data);

    if (d.error && !d.status) {
      // Job sudah dibersihkan dari memori server (mis. server di-restart).
      evtSource.close();
      _tombolKembali(runBtn, stopBtn);
      if (statusEl) { statusEl.textContent = 'Menunggu'; statusEl.className = 'badge bg-secondary'; }
      return;
    }

    if (barEl && d.progress != null) {
      const pct = Math.min(d.progress, 100);
      barEl.style.width = pct + '%';
      barEl.textContent = pct + '%';
    }
    if (countEl && d.found != null) countEl.textContent = d.found;

    if (logEl && d.logs && d.logs.length) {
      logEl.innerHTML += d.logs.map(line => {
        const cls = line.startsWith('✓') ? 'log-ok' : line.startsWith('⚠') ? 'log-warn' : line.startsWith('❌') ? 'log-err' : '';
        return `<div class="${cls}">${escapeHtml(line)}</div>`;
      }).join('');
      logEl.scrollTop = logEl.scrollHeight;
    }

    // Selesai (termasuk saat dihentikan — hasil parsial tetap tersimpan)
    if (d.status === 'done' || d.status === 'dibatalkan') {
      evtSource.close();
      const batal = d.status === 'dibatalkan';
      if (barEl) {
        if (!batal) { barEl.style.width = '100%'; barEl.textContent = '100%'; }
        barEl.className = 'progress-bar progress-bar-striped ' + (batal ? 'bg-secondary' : 'bg-success');
      }
      // Selesai tanpa file = tidak ada satu pun lead yang lolos. Ditandai
      // kuning, bukan hijau: "✅ Selesai" tanpa tombol download membuat user
      // mengira filenya gagal diunduh, padahal file itu memang tidak dibuat.
      const kosong = !batal && !d.filename;
      if (barEl && kosong) barEl.className = 'progress-bar bg-warning';
      if (statusEl) {
        statusEl.textContent = batal ? '⏹ Dihentikan' : (kosong ? '⚠ Tanpa hasil' : '✅ Selesai');
        statusEl.className = 'badge ' + (batal ? 'bg-secondary'
                                               : (kosong ? 'bg-warning text-dark' : 'bg-success'));
      }
      _tombolKembali(runBtn, stopBtn);
      if (dlBtn && d.filename) {
        dlBtn.href = '/download/' + d.filename;
        dlBtn.textContent = '⬇ Download ' + d.filename;
        dlBtn.classList.remove('d-none');
      }
      if (typeof onDone === 'function') onDone(d);
    }

    if (d.status === 'error') {
      evtSource.close();
      if (barEl) barEl.className = 'progress-bar bg-danger';
      if (statusEl) { statusEl.textContent = '❌ Error'; statusEl.className = 'badge bg-danger'; }
      _tombolKembali(runBtn, stopBtn);
      if (logEl) logEl.innerHTML += `<div class="log-err">❌ ${escapeHtml(d.error)}</div>`;
    }
  };

  evtSource.onerror = () => {
    if (evtSource.readyState === EventSource.CLOSED) return;
    if (logEl) logEl.innerHTML += '<div class="log-warn">⚠ Koneksi SSE terputus — mencoba menyambung lagi...</div>';
  };
}

/**
 * Saat halaman scraper dibuka: kalau ada job `jenis` yang masih berjalan (atau
 * baru saja selesai), sambungkan lagi. Scraping jalan di server, bukan di tab
 * browser — pindah menu atau menutup tab tidak menghentikannya.
 */
async function pulihkanJob(jenis, onDone) {
  try {
    const d = await (await fetch('/jobs/aktif?jenis=' + encodeURIComponent(jenis))).json();
    const j = (d.jobs || [])[0];
    if (j) sambungJob(j.job_id, onDone);
    return j || null;
  } catch (e) { return null; }
}

/**
 * Minta job berhenti. Scraper memeriksa sinyal ini di antara listing, jadi
 * berhentinya rapi: data yang sudah terkumpul tetap ditulis ke Excel & database.
 */
function stopJob() {
  if (!currentJobId) return;
  const stopBtn = document.getElementById('stopBtn');
  const logEl = document.getElementById('scrapeLog');
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Menghentikan...';
  }
  if (logEl) logEl.innerHTML += '<div class="log-warn">⏹ Permintaan berhenti dikirim — menyelesaikan listing yang sedang berjalan...</div>';
  fetch('/job/' + currentJobId + '/stop', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      // Scraper yang tidak mendukung Stop menolak dengan pesan — tampilkan dan
      // kembalikan tombolnya, jangan biarkan terlihat seolah sedang berhenti.
      if (d && d.ok === false) {
        if (logEl) logEl.innerHTML += `<div class="log-warn">⚠ ${escapeHtml(d.error || 'Tidak bisa dihentikan')}</div>`;
        if (stopBtn) { stopBtn.disabled = true; stopBtn.innerHTML = '<i class="bi bi-hourglass-split me-2"></i>Tunggu selesai'; }
      }
    })
    .catch(() => {
      if (logEl) logEl.innerHTML += '<div class="log-err">❌ Gagal mengirim perintah stop.</div>';
    });
}

/** Tes koneksi proxy Apify dari form (dipakai halaman Google Maps & Komponen). */
function cekProxy(btn) {
  const statusEl = document.getElementById('proxyStatus');
  const asli = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  statusEl.innerHTML = '<small class="text-muted">Menghubungi proxy...</small>';
  fetch('/check-proxy', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      apify_proxy_token: document.getElementById('apifyToken').value.trim(),
      apify_proxy_group: document.getElementById('apifyGroup').value,
      apify_proxy_country: document.getElementById('apifyCountry').value.trim()
    })
  })
  .then(r => r.json())
  .then(d => {
    btn.disabled = false; btn.innerHTML = asli;
    statusEl.innerHTML = d.ok
      ? `<small class="text-success"><i class="bi bi-check-circle-fill"></i> Proxy jalan — IP: <strong>${d.ip}</strong></small>`
      : `<small class="text-danger"><i class="bi bi-x-circle-fill"></i> ${escapeHtml(d.error || 'Gagal')}</small>`;
  })
  .catch(() => {
    btn.disabled = false; btn.innerHTML = asli;
    statusEl.innerHTML = '<small class="text-danger">Tidak bisa menghubungi server</small>';
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ─── Gemini helpers ────────────────────────────────────────────────────────────

function checkGeminiKey(inputId, statusId, btn) {
  const key = document.getElementById(inputId).value.trim();
  const statusEl = document.getElementById(statusId);
  if (!key) {
    statusEl.innerHTML = '<small class="text-danger">Isi API key dulu</small>';
    return;
  }
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  statusEl.innerHTML = '<small class="text-muted">Menghubungi Gemini...</small>';
  fetch('/check-gemini', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  })
  .then(r => r.json())
  .then(d => {
    btn.disabled = false;
    btn.innerHTML = origHtml;
    statusEl.innerHTML = d.ok
      ? '<small class="text-success"><i class="bi bi-check-circle-fill"></i> Terhubung ke Gemini ✓</small>'
      : `<small class="text-danger"><i class="bi bi-x-circle-fill"></i> Gagal: ${escapeHtml(d.error)}</small>`;
  })
  .catch(() => {
    btn.disabled = false;
    btn.innerHTML = origHtml;
    statusEl.innerHTML = '<small class="text-danger">Tidak bisa menghubungi server</small>';
  });
}

function toggleGeminiSection(checkbox, sectionId) {
  const el = document.getElementById(sectionId);
  if (el) el.style.display = checkbox.checked ? '' : 'none';
}
