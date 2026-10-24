// Device Cast receiver. Served by the sender and run either inside the TV app or from receiver.html,
// so it builds its own styles and elements.
(function () {
  // The TV app loader already provides the brand fonts and styles; reuse them and its animated logo.
  var logo = document.querySelector('#splash svg');
  var logoHTML = logo ? logo.outerHTML : '';
  var old = document.getElementById('loader-style');
  if (old) old.parentNode.removeChild(old);
  var st = document.createElement('style');
  st.textContent = 'html, body { margin: 0; height: 100%; background: #000; overflow: hidden; }' +
    ' video, img.mjpeg, canvas.mjpeg { position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: contain; background: #000; }' +
    ' #osd { position: absolute; left: 24px; top: 18px; color: #cdb8ff; font: 18px/1.4 "JetBrains Mono", monospace;' +
    ' background: rgba(11, 7, 22, .72); padding: 8px 12px; border-radius: 8px; display: none; white-space: pre; }' +
    ' #msg { z-index: 5; transition: opacity .35s; }' +
    ' #msg.hidden { opacity: 0; pointer-events: none; }' +
    ' .as-splash { background-color: #0b0716; }';
  document.head.appendChild(st);
  document.body.innerHTML = '<video id="v" muted playsinline></video>' +
    '<div id="msg" class="as-splash">' + logoHTML +
    '<div class="as-title">Device Cast</div><div id="msgText" class="as-status">Connecting…</div>' +
    '</div><div id="osd"></div>';
})();
(function () {
  var host = window.DEVICECAST_HOST || location.host;   // set by the TV app loader
  var video = document.getElementById('v');
  var msg = document.getElementById('msg');
  var osd = document.getElementById('osd');
  var showOsd = false;
  var CODECS = 'video/mp4; codecs="avc1.4d401f, mp4a.40.2"';
  // Adaptive buffer target: starts low and grows by 0.1 s on every stall, up to 1.2 s.
  var MIN_TARGET = 0.35;
  var target = MIN_TARGET;
  var MAX_TARGET = 1.2;
  var JUMP_SLACK = 0.5;   // jump ahead when the lag exceeds target + this
  var lastStall = Date.now();
  var stats = { bytes: 0, frags: 0, jumps: 0, stalls: 0, mode: '-', t0: Date.now(), lag: -1, fails: 0 };
  video.addEventListener('waiting', function () {
    stats.stalls++;
    lastStall = Date.now();
    target = Math.min(MAX_TARGET, target + 0.1);
  });

  // Report playback state to the sender every 5 s (written to the sender log).
  setInterval(function () {
    if (stats.mode === 'JPEG' || stats.mode === 'TILES') return;   // these modes post their own stats
    var x = new XMLHttpRequest();
    x.open('POST', 'http://' + host + '/stats', true);
    x.setRequestHeader('Content-Type', 'text/plain');
    try {
      x.send('mode ' + stats.mode + ', lag ' + (stats.lag >= 0 ? stats.lag.toFixed(2) + ' s' : '?') +
             ', ' + video.videoWidth + 'x' + video.videoHeight + ', state ' + video.readyState +
             ', ' + (stats.bytes / 1e6).toFixed(1) + ' MB, jumps ' + stats.jumps +
             ', stalls ' + stats.stalls + ', target ' + target.toFixed(2) + ' s, splash ' + (msg.className.indexOf('hidden') >= 0 ? 'hidden' : 'visible') +
             ', ranges ' + video.buffered.length + (video.error ? ', video error ' + video.error.code : '') +
             ' |ct=' + video.currentTime.toFixed(3));
    } catch (e) {}
  }, 5000);

  // If the sender stays unreachable for ~30 s (casting stopped), exit the app and return to the TV.
  function failedOnce() {
    stats.fails++;
    if (stats.fails >= 15) { try { window.tizen.application.getCurrentApplication().exit(); } catch (x) {} }
  }

  var msgText = document.getElementById('msgText');
  function setMsg(t) {
    if (t) { msgText.textContent = t; msg.className = 'as-splash'; }
    else { msg.className = 'as-splash hidden'; }
  }
  video.addEventListener('playing', function () { setMsg(''); });

  function mseSupported() {
    try { return !!(window.MediaSource && MediaSource.isTypeSupported(CODECS)); } catch (e) { return false; }
  }

  // MediaSource mode: H.264 + AAC over WebSocket.
  function startMSE() {
    stats.mode = 'MSE';
    var ms = new MediaSource();
    var sb = null, queue = [], ws = null, closedByUs = false;
    video.src = URL.createObjectURL(ms);

    function pump() {
      if (!sb || sb.updating || !queue.length) return;
      try { sb.appendBuffer(queue.shift()); }
      catch (e) { console.log('append error', e); restart(); }
    }

    ms.addEventListener('sourceopen', function () {
      try { sb = ms.addSourceBuffer(CODECS); } catch (e) { setMsg('MediaSource unavailable, using MJPEG'); return startMJPEG(); }
      sb.mode = 'sequence' in sb ? 'segments' : sb.mode;
      sb.addEventListener('updateend', pump);
      ws = new WebSocket('ws://' + host + '/ws');
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () { setMsg('Receiving screen…'); stats.fails = 0; };
      ws.onmessage = function (ev) {
        stats.bytes += ev.data.byteLength; stats.frags++;
        queue.push(ev.data);
        if (queue.length > 60) { queue.splice(0, queue.length - 10); }   // slow network: drop stale data
        pump();
      };
      ws.onclose = function () { if (!closedByUs) { failedOnce(); setMsg('Connection lost, retrying…'); setTimeout(restart, 1500); } };
      ws.onerror = function () { };
    });

    function restart() {
      closedByUs = true;
      try { ws && ws.close(); } catch (e) {}
      setTimeout(startMSE, 500);
    }

    // Latency control: stay close to the end of the buffer.
    setInterval(function () {
      if (!sb || !video.buffered.length) return;
      var end = video.buffered.end(video.buffered.length - 1);
      var lag = end - video.currentTime;
      stats.lag = lag;
      // After 15 s without stalls, lower the target by 0.05 s per step (recovers after a Wi-Fi spike).
      if (Date.now() - lastStall > 15000 && target > MIN_TARGET) {
        target = Math.max(MIN_TARGET, target - 0.05);
        lastStall = Date.now() - 10000;   // next step in 5 s
      }
      // At startup wait until the target is buffered, then only jump when far behind.
      if (video.paused) {
        if (lag >= target) { video.play().catch(function () {}); }
      } else if (lag > target + JUMP_SLACK) {
        video.currentTime = end - target; stats.jumps++;
      }
      // Trim old buffered data.
      var start = video.buffered.start(0);
      if (!sb.updating && video.currentTime - start > 20) { try { sb.remove(start, video.currentTime - 10); } catch (e) {} }
      if (showOsd) {
        osd.textContent = 'MSE  lag ' + lag.toFixed(2) + 's  target ' + target.toFixed(2) +
          '\n' + (stats.bytes / 1e6).toFixed(1) + ' MB  ' + stats.frags + ' frag  jumps ' + stats.jumps +
          '\n' + video.videoWidth + 'x' + video.videoHeight + '  ' + (navigator.userAgent.match(/Chrome\/\d+/) || [''])[0];
      }
    }, 250);
  }

  // MJPEG fallback: no audio, native JPEG decoding.
  function startMJPEG() {
    stats.mode = 'MJPEG';
    video.style.display = 'none';
    var img = document.createElement('img');
    img.className = 'mjpeg';
    img.src = 'http://' + host + '/stream.mjpg?t=' + Date.now();
    img.onload = function () { setMsg(''); };
    img.onerror = function () { setMsg('Connection lost, retrying…'); setTimeout(function () { img.src = 'http://' + host + '/stream.mjpg?t=' + Date.now(); }, 1500); };
    document.body.insertBefore(img, msg);
    if (showOsd) osd.textContent = 'MJPEG';
  }

  document.addEventListener('keydown', function (e) {
    // Remote control: Return (10009) exits; Info/Green (10073/404) toggles the stats overlay.
    // Other keys are not consumed, so receiver add-ons can use them (see receiver/addons/README.md).
    if (e.keyCode === 10009) { try { window.tizen.application.getCurrentApplication().exit(); } catch (x) { history.back(); } }
    if (e.keyCode === 404 || e.keyCode === 10073 || e.key === 'i') { showOsd = !showOsd; osd.style.display = showOsd ? 'block' : 'none'; }
  });

  video.addEventListener('error', function () { console.log('video error', video.error); });

  // Lowest-latency JPEG mode: no video player, only the latest frame is shown.
  function startJPEG() {
    stats.mode = 'JPEG';
    video.style.display = 'none';
    // Draw on a canvas via createImageBitmap (decodes off the main thread, two at a time);
    // fall back to <img> when the TV browser lacks it.
    var useBitmap = typeof window.createImageBitmap === 'function';
    var canvas = document.createElement('canvas');
    canvas.className = 'mjpeg';
    var ctx = canvas.getContext('2d');
    var img = document.createElement('img');
    img.className = 'mjpeg';
    document.body.insertBefore(useBitmap ? canvas : img, msg);
    var inflight = 0, MAX_INFLIGHT = 2, pending = null, prevUrl = null;
    var shownTs = 0, shownAt = 0, frames = 0, ws = null;
    var decSum = 0, decN = 0, lastArr = 0, arrSum = 0, arrN = 0, skipped = 0;

    function displayed(ts, t0) {
      if (ts < shownTs) return false;          // late: a newer frame is already on screen
      shownTs = ts; shownAt = Date.now(); frames++;
      decSum += shownAt - t0; decN++;
      setMsg('');
      return true;
    }

    function next() {
      if (pending && inflight < MAX_INFLIGHT) { var p = pending; pending = null; show(p); }
    }

    function show(buf) {
      var ts = new DataView(buf, 0, 8).getFloat64(0, false);
      var blob = new Blob([buf.slice(8)], { type: 'image/jpeg' });
      var t0 = Date.now();
      inflight++;
      if (useBitmap) {
        createImageBitmap(blob).then(function (bmp) {
          inflight--;
          if (displayed(ts, t0)) {
            if (canvas.width !== bmp.width) { canvas.width = bmp.width; canvas.height = bmp.height; }
            ctx.drawImage(bmp, 0, 0);
          }
          if (bmp.close) bmp.close();
          next();
        }, function () { inflight--; useBitmap = false; next(); });
      } else {
        var url = URL.createObjectURL(blob);
        img.onload = function () {
          inflight--;
          if (prevUrl) URL.revokeObjectURL(prevUrl);
          prevUrl = url;
          displayed(ts, t0);
          next();
        };
        img.onerror = function () { inflight--; URL.revokeObjectURL(url); next(); };
        img.src = url;
      }
    }

    function connect() {
      ws = new WebSocket('ws://' + host + '/wsj');
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () { setMsg('Receiving screen…'); stats.fails = 0; };
      ws.onmessage = function (ev) {
        var now = Date.now();
        if (lastArr) { arrSum += now - lastArr; arrN++; }
        lastArr = now;
        stats.bytes += ev.data.byteLength; stats.frags++;
        if (inflight < (useBitmap ? MAX_INFLIGHT : 1)) show(ev.data);
        else { if (pending) skipped++; pending = ev.data; }   // keep only the newest
      };
      ws.onclose = function () { failedOnce(); setMsg('Connection lost, retrying…'); setTimeout(connect, 1500); };
    }
    connect();

    // Every second: sender timestamp of the frame on screen, how long it has been shown, and TV timings.
    setInterval(function () {
      var x = new XMLHttpRequest();
      x.open('POST', 'http://' + host + '/stats', true);
      x.setRequestHeader('Content-Type', 'text/plain');
      try {
        x.send('mode JPEG ' + (useBitmap ? 'canvas' : 'img') + ', ' + frames + ' frames shown, ' +
               (stats.bytes / 1e6).toFixed(1) + ' MB, decode ' + (decN ? Math.round(decSum / decN) : '?') +
               ' ms, arrivals every ' + (arrN ? Math.round(arrSum / arrN) : '?') + ' ms, skipped on TV ' + skipped +
               heapInfo() + ' |eco=' + shownTs + '|fa=' + (shownAt ? Date.now() - shownAt : 0));
        decSum = decN = arrSum = arrN = 0;
      } catch (e) {}
    }, 1000);
  }

  function heapInfo() {
    // performance.memory is non-standard but available in the Chromium engine of Tizen TVs
    var m = window.performance && window.performance.memory;
    return m ? ', heap ' + Math.round(m.usedJSHeapSize / 1048576) + '/' + Math.round(m.jsHeapSizeLimit / 1048576) + ' MB' : '';
  }

  // Tiles mode: only changed regions, drawn on a native 1920x1080 canvas.
  function startTiles() {
    if (typeof window.createImageBitmap !== 'function') { startJPEG(); return; }
    stats.mode = 'TILES';
    video.style.display = 'none';
    var canvas = document.createElement('canvas');
    canvas.className = 'mjpeg';
    canvas.width = 1920; canvas.height = 1080;
    var ctx = canvas.getContext('2d');
    document.body.insertBefore(canvas, msg);
    var queue = [], busy = false, ws = null;

    // Sender mouse pointer as an overlay moved with a GPU transform, so it updates without redrawing tiles.
    var cur = document.createElement('img');
    cur.style.cssText = 'position:absolute;left:0;top:0;z-index:4;pointer-events:none;display:none;will-change:transform';
    document.body.insertBefore(cur, msg);
    var curShape = { hx: 0, hy: 0, w: 0, h: 0 }, curX = -1, curY = -1;
    function layoutCursor() {
      if (vl || curX < 0 || !curShape.w) { cur.style.display = 'none'; return; }
      var vw = window.innerWidth, vh = window.innerHeight;
      var s = Math.min(vw / canvas.width, vh / canvas.height);
      var ox = (vw - canvas.width * s) / 2, oy = (vh - canvas.height * s) / 2;
      cur.style.width = (curShape.w * s) + 'px';
      cur.style.height = (curShape.h * s) + 'px';
      cur.style.transform = 'translate(' + (ox + (curX - curShape.hx) * s) + 'px,' + (oy + (curY - curShape.hy) * s) + 'px)';
      cur.style.display = 'block';
    }
    // H.264 video layer: enabled when the sender detects video playback, disabled when it ends.
    var vl = null;
    function startVideoLayer() {
      if (vl) return;
      var me = vl = { ms: new MediaSource(), sb: null, q: [], ws: null, timer: null, stopped: false, target: 0.35 };
      video.style.display = 'block'; video.style.opacity = '0'; video.style.zIndex = '3';
      layoutCursor();
      video.src = URL.createObjectURL(me.ms);
      function pump() {
        if (me.stopped || !me.sb || me.sb.updating || !me.q.length) return;
        try { me.sb.appendBuffer(me.q.shift()); } catch (e) {}
      }
      me.ms.addEventListener('sourceopen', function () {
        if (me.stopped) return;
        try { me.sb = me.ms.addSourceBuffer(CODECS); } catch (e) { return; }
        me.sb.addEventListener('updateend', pump);
        me.ws = new WebSocket('ws://' + host + '/ws');
        me.ws.binaryType = 'arraybuffer';
        me.ws.onmessage = function (ev) {
          me.q.push(ev.data);
          if (me.q.length > 60) me.q.splice(0, me.q.length - 10);
          pump();
        };
      });
      // Only cover the tiles once the video is actually playing, to avoid a black frame in between.
      me.onplay = function () { if (!me.stopped) video.style.opacity = '1'; };
      video.addEventListener('playing', me.onplay);
      me.timer = setInterval(function () {
        if (!me.sb || !video.buffered.length) return;
        var end = video.buffered.end(video.buffered.length - 1), lag = end - video.currentTime;
        if (video.paused) { if (lag >= me.target) video.play().catch(function () {}); }
        else if (lag > me.target + 0.5) { video.currentTime = end - me.target; }
        var st = video.buffered.start(0);
        if (!me.sb.updating && video.currentTime - st > 20) { try { me.sb.remove(st, video.currentTime - 10); } catch (e) {} }
      }, 250);
    }
    function stopVideoLayer() {
      if (!vl) return;
      var me = vl; vl = null; me.stopped = true;
      clearInterval(me.timer);
      video.removeEventListener('playing', me.onplay);
      try { if (me.ws) me.ws.close(); } catch (e) {}
      try { video.pause(); } catch (e) {}
      video.removeAttribute('src');
      try { video.load(); } catch (e) {}
      video.style.display = 'none'; video.style.opacity = '1';
      layoutCursor();
    }

    function cursorMessage(t) {
      var p = t.split(' ');
      if (p[0] === 'c') { curX = +p[1]; curY = +p[2]; layoutCursor(); }
      else if (p[0] === 'C') {
        curShape = { hx: +p[2], hy: +p[3], w: +p[4], h: +p[5] };
        cur.src = 'data:image/png;base64,' + p[6];
        layoutCursor();
      }
    }
    var shownTs = 0, shownAt = 0, updates = 0, tilesDrawn = 0, drawSum = 0, drawN = 0;
    var gaps = [], halfRes = 0;   // intervals between consecutive draws (smoothness)

    function processNext() {
      if (busy || !queue.length) return;
      busy = true;
      var buf = queue.shift();
      var dv = new DataView(buf);
      var ts = dv.getFloat64(0, false), fw = dv.getUint16(8, false), fh = dv.getUint16(10, false), n = dv.getUint32(12, false);
      if (canvas.width !== fw || canvas.height !== fh) { canvas.width = fw; canvas.height = fh; layoutCursor(); }
      var off = 16, items = [], t0 = Date.now();
      for (var i = 0; i < n; i++) {
        var x = dv.getUint16(off, false), y = dv.getUint16(off + 2, false),
            tw = dv.getUint16(off + 4, false), th = dv.getUint16(off + 6, false), ln = dv.getUint32(off + 8, false);
        off += 12;
        items.push({ x: x, y: y, w: tw, h: th, blob: new Blob([new Uint8Array(buf, off, ln)], { type: 'image/jpeg' }) });
        off += ln;
      }
      function finish() {
        var now = Date.now();
        if (shownAt && now - shownAt < 1000) gaps.push(now - shownAt);
        for (var q = 0; q < items.length; q++) if (items[q].w === fw && items[q].h === fh) halfRes++;
        shownTs = ts; shownAt = now; updates++; tilesDrawn += items.length;
        drawSum += shownAt - t0; drawN++;
        setMsg('');
        try { ws.send('a' + queue.length); } catch (e) {}   // ack to the sender: drawn, plus current queue length
        busy = false;
        processNext();
      }
      Promise.all(items.map(function (it) { return createImageBitmap(it.blob); })).then(function (bmps) {
        for (var k = 0; k < bmps.length; k++) {
          // A tile may arrive at reduced resolution (large motion); scale it up to its area.
          ctx.drawImage(bmps[k], items[k].x, items[k].y, items[k].w, items[k].h);
          if (bmps[k].close) bmps[k].close();
        }
        finish();
      }, finish);
    }

    function connect() {
      ws = new WebSocket('ws://' + host + '/wst');
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () { setMsg('Receiving screen…'); stats.fails = 0; };
      ws.onmessage = function (ev) {
        if (typeof ev.data === 'string') {
          if (ev.data === 'mode h264') startVideoLayer();
          else if (ev.data === 'mode tiles') stopVideoLayer();
          else cursorMessage(ev.data);
          return;
        }
        stats.bytes += ev.data.byteLength;
        queue.push(ev.data);
        if (queue.length > 120) { queue = []; ws.close(); return; }   // reconnect; the sender restarts with a full frame
        processNext();
      };
      ws.onclose = function () { stopVideoLayer(); failedOnce(); setMsg('Connection lost, retrying…'); setTimeout(connect, 1500); };
    }
    connect();

    setInterval(function () {
      var x = new XMLHttpRequest();
      x.open('POST', 'http://' + host + '/stats', true);
      x.setRequestHeader('Content-Type', 'text/plain');
      try {
        x.send((vl ? 'mode tiles + H.264 video (video lag ' +
                (video.buffered.length ? (video.buffered.end(video.buffered.length - 1) - video.currentTime).toFixed(2) : '?') +
                ' s), ' : 'mode tiles, ') + updates + ' updates, ' + tilesDrawn + ' tiles, ' +
               (stats.bytes / 1e6).toFixed(1) + ' MB, draw ' + (drawN ? Math.round(drawSum / drawN) : '?') +
               ' ms, queued ' + queue.length + ', ' + canvas.width + 'x' + canvas.height + rhythm() +
               heapInfo() + ' |eco=' + shownTs + '|fa=' + (shownAt ? Date.now() - shownAt : 0));
        drawSum = drawN = 0;
      } catch (e) {}
    }, 1000);

    function rhythm() {
      if (gaps.length < 3) { gaps = []; return ''; }
      var g = gaps.slice().sort(function (a, b) { return a - b; });
      var p = function (f) { return g[Math.min(g.length - 1, Math.floor(g.length * f))]; };
      var out = ', rate ' + (1000 / p(0.5)).toFixed(0) + '/s, intervals p50 ' + p(0.5) + ' p95 ' + p(0.95) +
                ' max ' + g[g.length - 1] + ' ms, full-screen tiles ' + halfRes;
      gaps = []; halfRes = 0;
      return out;
    }
  }

  // The sender picks the mode via /hello; without an answer, fall back to H.264 video.
  (function choose() {
    var x = new XMLHttpRequest();
    x.open('GET', 'http://' + host + '/hello?t=' + Date.now(), true);
    x.timeout = 4000;
    var decided = false;
    function decide(codec) {
      if (decided) return; decided = true;
      if (codec === 'tiles') startTiles();
      else if (codec === 'jpeg') startJPEG();
      else if (mseSupported()) startMSE();
      else startMJPEG();
    }
    x.onreadystatechange = function () {
      if (x.readyState !== 4) return;
      var codec = 'h264';
      try { codec = JSON.parse(x.responseText).codec || 'h264'; } catch (e) {}
      decide(codec);
    };
    x.ontimeout = function () { decide('h264'); };
    x.onerror = function () { decide('h264'); };
    x.send();
  })();
})();
