/**
 * progress.js — SSE real-time progress handler untuk semua scraper
 */

let currentJobId = null;
let evtSource = null;

function startJob(endpoint, params, onDone) {
  // Reset UI
  const logEl = document.getElementById('scrapeLog');
  const barEl = document.getElementById('progressBar');
  const countEl = document.getElementById('foundCount');
  const statusEl = document.getElementById('statusBadge');
  const dlBtn = document.getElementById('downloadBtn');
  const runBtn = document.getElementById('runBtn');
  const stopBtn = document.getElementById('stopBtn');

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

  // Tutup SSE lama jika ada
  if (evtSource) evtSource.close();

  // POST untuk mulai job
  fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(params)
  })
  .then(r => r.json())
  .then(data => {
    currentJobId = data.job_id;
    // Mulai stream SSE
    evtSource = new EventSource('/stream/' + currentJobId);
    evtSource.onmessage = (e) => {
      const d = JSON.parse(e.data);

      // Update progress bar
      if (barEl && d.progress != null) {
        const pct = Math.min(d.progress, 100);
        barEl.style.width = pct + '%';
        barEl.textContent = pct + '%';
      }

      // Update jumlah ditemukan
      if (countEl && d.found != null) countEl.textContent = d.found;

      // Append log baru
      if (logEl && d.logs) {
        d.logs.forEach(line => {
          const cls = line.startsWith('✓') ? 'log-ok' : line.startsWith('⚠') ? 'log-warn' : line.startsWith('❌') ? 'log-err' : '';
          logEl.innerHTML += `<div class="${cls}">${escapeHtml(line)}</div>`;
        });
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
        if (stopBtn) stopBtn.classList.add('d-none');
        if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="bi bi-play-fill me-2"></i>Mulai Scraping'; }
        if (dlBtn && d.filename) {
          dlBtn.href = '/download/' + d.filename;
          dlBtn.textContent = '⬇ Download ' + d.filename;
          dlBtn.classList.remove('d-none');
        }
        if (typeof onDone === 'function') onDone(d);
      }

      // Error
      if (d.status === 'error') {
        evtSource.close();
        if (barEl) barEl.className = 'progress-bar bg-danger';
        if (statusEl) { statusEl.textContent = '❌ Error'; statusEl.className = 'badge bg-danger'; }
        if (stopBtn) stopBtn.classList.add('d-none');
        if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="bi bi-play-fill me-2"></i>Mulai Scraping'; }
        if (logEl) logEl.innerHTML += `<div class="log-err">❌ ${escapeHtml(d.error)}</div>`;
      }
    };

    evtSource.onerror = () => {
      if (evtSource.readyState === EventSource.CLOSED) return;
      if (logEl) logEl.innerHTML += '<div class="log-warn">⚠ Koneksi SSE terputus.</div>';
    };
  })
  .catch(err => {
    if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="bi bi-play-fill me-2"></i>Mulai Scraping'; }
    if (stopBtn) stopBtn.classList.add('d-none');
    if (logEl) logEl.innerHTML += `<div class="log-err">❌ Gagal memulai: ${err}</div>`;
  });
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
    .catch(() => {
      if (logEl) logEl.innerHTML += '<div class="log-err">❌ Gagal mengirim perintah stop.</div>';
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
