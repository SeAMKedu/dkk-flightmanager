// ── External-change event stream ──────────────────────────────────────────────

function _initEventStream() {
  var es = new EventSource('/api/events');
  var _debounceTimer = null;

  es.onmessage = function(e) {
    var evt;
    try { evt = JSON.parse(e.data); } catch(ex) { return; }
    if (evt.type !== 'jobs_changed') return;

    // Debounce rapid bursts (batch runs write many files quickly)
    if (_debounceTimer) clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(function() {
      loadJobsList();
      // Show notice only when the open job was touched externally (not by our own save)
      if (_activeJob && !isRunning && evt.paths && evt.paths.indexOf(_activeJob) !== -1) {
        if (_activeJob === _ownSavedJob) {
          _ownSavedJob = null;
        } else {
          showExtModifiedNotice();
        }
      }
    }, 800);
  };

  es.onerror = function() {
    // EventSource reconnects automatically — no action needed
  };
}

function showExtModifiedNotice() {
  var el = document.getElementById('ext-modified-notice');
  el.innerHTML = '<span style="flex:1">Job modified externally.</span>'
    + '<button class="ext-mod-btn" onclick="reloadCurrentJob()">Reload</button>'
    + '<button class="ext-mod-btn" onclick="hideExtModifiedNotice()">✕</button>';
  el.classList.add('visible');
}

function hideExtModifiedNotice() {
  var el = document.getElementById('ext-modified-notice');
  el.classList.remove('visible');
  el.innerHTML = '';
}

function reloadCurrentJob() {
  hideExtModifiedNotice();
  if (_activeJob) openJob(_activeJob);
}
