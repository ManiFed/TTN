/* ============================================================
   Boundless Skies — marketing site
   Pulls live data from the cloud API; falls back gracefully when
   the API is unreachable so the page is never broken.
   ============================================================ */
(function () {
  "use strict";

  var API = "http://" + (location.hostname || "localhost") + ":8800";

  function getJSON(path) {
    return fetch(API + path, { mode: "cors" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  var $ = function (id) { return document.getElementById(id); };
  function hhmmss(iso) {
    try { return new Date(iso).toISOString().slice(11, 19); } catch (e) { return "--:--:--"; }
  }

  /* =========================================================
     1. PLATE-SOLVING STARFIELD  (distinctive hero animation)
     A slowly drifting star field; targeting reticles acquire
     real catalogue targets one by one — mirroring what the
     pipeline actually does (plate-solve → identify → measure).
     ========================================================= */
  function skyfield(initialTargets) {
    var c = $("skyfield");
    if (!c) return { setTargets: function () {} };
    var targets = initialTargets;
    var ctx = c.getContext("2d");
    var W, H, DPR = Math.min(devicePixelRatio || 1, 2);
    var stars = [], links = [];

    function resize() {
      var r = c.getBoundingClientRect();
      W = c.width = r.width * DPR; H = c.height = r.height * DPR;
      var n = Math.min(260, Math.floor((r.width * r.height) / 5200));
      stars = [];
      for (var i = 0; i < n; i++) {
        stars.push({
          x: Math.random() * W, y: Math.random() * H,
          r: (Math.random() * 1.3 + 0.3) * DPR,
          a: 0.25 + Math.random() * 0.6,
          tw: 0.4 + Math.random() * 1.6, ph: Math.random() * 6.28,
          col: Math.random() < 0.12 ? "150,175,255" : (Math.random() < 0.12 ? "232,200,150" : "255,255,255")
        });
      }
      // faint constellation: link a handful of brighter stars
      links = [];
      var bright = stars.filter(function (s) { return s.a > 0.7; }).slice(0, 9);
      for (var k = 0; k < bright.length - 1; k++) {
        if (Math.random() < 0.6) links.push([bright[k], bright[k + 1]]);
      }
    }

    // reticle acquisition targets — anchored to bright stars, labelled with real data
    var anchors = [];
    function placeAnchors() {
      anchors = [];
      var pool = stars.slice().sort(function (a, b) { return b.a - a.a; }).slice(0, 18);
      for (var i = 0; i < targets.length && i < 6; i++) {
        var s = pool[(i * 3) % pool.length];
        anchors.push({ x: s.x, y: s.y, name: targets[i].name, mag: targets[i].mag });
      }
    }

    var idx = 0, phase = "scan", t0 = 0;
    var DUR = { scan: 700, acquire: 800, lock: 2400, release: 500 };

    // shooting stars
    var meteors = [], meteorClock = 0, nextMeteorIn = 5000 + Math.random() * 9000;
    var noMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    function spawnMeteor() {
      var spd = (0.22 + Math.random() * 0.20) * DPR;
      var slope = 0.12 + Math.random() * 0.38;
      var hyp = Math.sqrt(1 + slope * slope);
      meteors.push({
        x: W * (0.3 + Math.random() * 0.7), y: H * (0.04 + Math.random() * 0.28),
        vx: -spd / hyp, vy: spd * slope / hyp,
        life: 1, trail: (75 + Math.random() * 85) * DPR
      });
      nextMeteorIn = 9000 + Math.random() * 15000; meteorClock = 0;
    }

    function reticle(x, y, p, locked, label) {
      var R = (locked ? 17 : (46 - 29 * p)) * DPR;   // ring contracts while acquiring
      var col = locked ? "255,148,56" : "37,232,160";   // amber lock / green scan
      ctx.strokeStyle = "rgba(" + col + "," + (locked ? 0.9 : 0.5 + 0.4 * p) + ")";
      ctx.lineWidth = 1 * DPR;
      // ring
      ctx.beginPath(); ctx.arc(x, y, R * 0.72, 0, 6.2832); ctx.stroke();
      // corner brackets
      var b = R, g = R * 0.55;
      [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(function (d) {
        ctx.beginPath();
        ctx.moveTo(x + d[0] * b, y + d[1] * g);
        ctx.lineTo(x + d[0] * b, y + d[1] * b);
        ctx.lineTo(x + d[0] * g, y + d[1] * b);
        ctx.stroke();
      });
      // crosshair with central gap
      ctx.beginPath();
      ctx.moveTo(x - R, y); ctx.lineTo(x - R * 0.35, y);
      ctx.moveTo(x + R * 0.35, y); ctx.lineTo(x + R, y);
      ctx.moveTo(x, y - R); ctx.lineTo(x, y - R * 0.35);
      ctx.moveTo(x, y + R * 0.35); ctx.lineTo(x, y + R);
      ctx.stroke();
      // label (typed in while locked)
      if (locked && label) {
        var full = label.name + "  " + label.mag.toFixed(1) + " mag";
        var chars = Math.min(full.length, Math.floor(full.length * Math.min(1, p * 1.6)));
        ctx.fillStyle = "rgba(232,169,58,0.92)";
        ctx.font = (11 * DPR) + "px JetBrains Mono, monospace";
        ctx.textBaseline = "top";
        ctx.fillText(full.slice(0, chars), x + R + 6 * DPR, y - R);
      }
    }

    var last = performance.now();
    function frame(now) {
      var dt = now - last; last = now;
      ctx.clearRect(0, 0, W, H);

      // drift (sidereal-ish) + wrap
      var dx = 0.004 * DPR * dt, dy = 0.0016 * DPR * dt;
      // constellation links
      ctx.strokeStyle = "rgba(37,232,160,0.10)"; ctx.lineWidth = 0.6 * DPR;
      links.forEach(function (l) {
        ctx.beginPath(); ctx.moveTo(l[0].x, l[0].y); ctx.lineTo(l[1].x, l[1].y); ctx.stroke();
      });
      // stars
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        s.x += dx; s.y += dy;
        if (s.x > W) s.x -= W; if (s.y > H) s.y -= H;
        s.ph += dt * 0.001 * s.tw;
        var a = s.a * (0.7 + 0.3 * Math.sin(s.ph));
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.2832);
        ctx.fillStyle = "rgba(" + s.col + "," + a + ")"; ctx.fill();
      }
      // shooting stars
      if (!noMotion) {
        meteorClock += dt;
        if (meteorClock > nextMeteorIn) spawnMeteor();
        for (var mi = meteors.length - 1; mi >= 0; mi--) {
          var m = meteors[mi];
          m.x += m.vx * dt; m.y += m.vy * dt; m.life -= dt * 0.0016;
          if (m.life <= 0 || m.x < -20 || m.y > H + 20) { meteors.splice(mi, 1); continue; }
          var vl = Math.sqrt(m.vx * m.vx + m.vy * m.vy);
          var tx = m.x - (m.vx / vl) * m.trail, ty = m.y - (m.vy / vl) * m.trail;
          var mg = ctx.createLinearGradient(tx, ty, m.x, m.y);
          mg.addColorStop(0, "rgba(255,255,255,0)");
          mg.addColorStop(0.65, "rgba(210,228,255," + (m.life * 0.38) + ")");
          mg.addColorStop(1, "rgba(255,255,255," + m.life + ")");
          ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(m.x, m.y);
          ctx.strokeStyle = mg; ctx.lineWidth = 1.6 * DPR; ctx.lineCap = "round"; ctx.stroke();
          ctx.beginPath(); ctx.arc(m.x, m.y, 1.8 * DPR, 0, 6.2832);
          ctx.fillStyle = "rgba(255,255,255," + m.life + ")"; ctx.fill();
        }
      }
      // reticle state machine
      if (anchors.length) {
        t0 += dt;
        var a0 = anchors[idx % anchors.length];
        if (phase === "scan" && t0 > DUR.scan) { phase = "acquire"; t0 = 0; }
        else if (phase === "acquire" && t0 > DUR.acquire) { phase = "lock"; t0 = 0; }
        else if (phase === "lock" && t0 > DUR.lock) { phase = "release"; t0 = 0; }
        else if (phase === "release" && t0 > DUR.release) { phase = "scan"; t0 = 0; idx++; }

        if (phase === "acquire") reticle(a0.x, a0.y, t0 / DUR.acquire, false, a0);
        else if (phase === "lock") reticle(a0.x, a0.y, t0 / DUR.lock, true, a0);
        else if (phase === "release") reticle(a0.x, a0.y, 1, true, a0);
      }
      requestAnimationFrame(frame);
    }

    resize();
    placeAnchors();
    window.addEventListener("resize", function () { resize(); placeAnchors(); });
    requestAnimationFrame(frame);

    return {
      setTargets: function (t) { if (t && t.length) { targets = t; idx = 0; placeAnchors(); } }
    };
  }

  /* =========================================================
     2. LIGHT CURVES
     ========================================================= */
  function drawCurve(canvas, pts, progress, opts) {
    opts = opts || {};
    var ctx = canvas.getContext("2d"), W = canvas.width, H = canvas.height;
    var pad = H * 0.14;
    var mags = pts.map(function (p) { return p.m; });
    var lo = Math.min.apply(null, mags), hi = Math.max.apply(null, mags);
    if (hi - lo < 0.5) { hi += 0.5; lo -= 0.5; }
    // astronomical convention: brighter (smaller mag) sits higher
    var xy = function (i) {
      var x = (i / (pts.length - 1)) * W;
      var y = pad + ((pts[i].m - lo) / (hi - lo)) * (H - pad * 2);
      return [x, y];
    };
    ctx.clearRect(0, 0, W, H);
    if (opts.grid) {
      ctx.strokeStyle = "rgba(255,255,255,0.04)"; ctx.lineWidth = 1;
      for (var g = pad; g < H; g += (H - pad) / 4) { ctx.beginPath(); ctx.moveTo(0, g); ctx.lineTo(W, g); ctx.stroke(); }
    }
    var lim = Math.floor(progress * pts.length);
    ctx.beginPath(); ctx.strokeStyle = "rgba(37,232,160,0.45)"; ctx.lineWidth = opts.thin ? 1.1 : 1.6;
    for (var i = 0; i < lim; i++) { var p = xy(i); i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); }
    ctx.stroke();
    for (var j = 0; j < lim; j++) {
      var q = xy(j);
      ctx.beginPath(); ctx.arc(q[0], q[1], opts.thin ? 1.7 : 2.6, 0, 6.2832);
      ctx.fillStyle = pts[j].ok ? "#E8A93A" : "rgba(240,101,95,0.6)"; ctx.fill();
    }
    if (lim > 0) {
      var e = xy(lim - 1);
      ctx.beginPath(); ctx.arc(e[0], e[1], opts.thin ? 3 : 5, 0, 6.2832); ctx.fillStyle = "rgba(37,232,160,0.3)"; ctx.fill();
      ctx.beginPath(); ctx.arc(e[0], e[1], opts.thin ? 2 : 3, 0, 6.2832); ctx.fillStyle = "#25E8A0"; ctx.fill();
    }
  }

  function animateCurve(canvas, pts, opts, dur) {
    if (!canvas || !pts || !pts.length) return;
    var started = false, start = null;
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min((ts - start) / dur, 1);
      drawCurve(canvas, pts, p, opts);
      if (p < 1) requestAnimationFrame(step);
    }
    function run() {
      if (started) return; started = true;
      requestAnimationFrame(step);
      // if rAF hasn't produced a single frame shortly after we start (throttled
      // preview / background tab), paint the final curve outright so it's never blank
      setTimeout(function () { if (start === null) drawCurve(canvas, pts, 1, opts); }, 350);
    }
    function inView() {
      var r = canvas.getBoundingClientRect();
      return r.top < (window.innerHeight || document.documentElement.clientHeight) && r.bottom > 0;
    }
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || inView() || !("IntersectionObserver" in window)) { run(); return; }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { run(); io.disconnect(); } });
    }, { threshold: 0.2 });
    io.observe(canvas);
    // safety net: if the observer never fires, paint once it's scrolled near
    var guard = setInterval(function () {
      if (inView()) { run(); io.disconnect(); clearInterval(guard); }
    }, 500);
    setTimeout(function () { clearInterval(guard); }, 30000);
  }

  /* =========================================================
     3. RELIABILITY GAUGE
     ========================================================= */
  function drawGauge(value) {
    var c = $("gauge"); if (!c) return;
    var ctx = c.getContext("2d"), W = c.width, H = c.height, cx = W / 2, cy = H - 8, rad = 70;
    ctx.clearRect(0, 0, W, H); ctx.lineWidth = 9; ctx.lineCap = "round";
    ctx.beginPath(); ctx.arc(cx, cy, rad, Math.PI, 2 * Math.PI); ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.stroke();
    var col = value >= 0.85 ? "#54D98C" : value >= 0.65 ? "#25E8A0" : "#E8A93A";
    ctx.beginPath(); ctx.arc(cx, cy, rad, Math.PI, Math.PI + value * Math.PI); ctx.strokeStyle = col; ctx.stroke();
  }

  /* =========================================================
     4. NODE BUILDER — guided 5-step wizard
     Step 1 inventory quiz → 2 telescope → 3 computer →
     4 add-ons (collapsible) → 5 review. A persistent summary
     panel tracks cost, reliability, uptime and unlocked science.
     ========================================================= */
  function builder() {
    var cfg = $("rel-val");
    if (!cfg) return;
    var FLOOR = 0.50;

    // ---- catalog ----------------------------------------------------
    var CATALOG = {
      telescope: [
        { id: "own",  name: "I already own a Seestar", desc: "Skip the hardware — register your existing scope.", price: 0,   science: 0.85, tier: 1 },
        { id: "s50",  name: "ZWO Seestar S50",         desc: "The network's backbone. 50 mm, single CV filter.", price: 499, science: 0.85, tier: 1, tag: "recommended", def: true },
        { id: "s30",  name: "ZWO Seestar S30 Pro",     desc: "Lighter, wider field. Ideal for tight spaces.",     price: 399, science: 0.75, tier: 1, tag: "compact" }
      ],
      computer: [
        { id: "own",  name: "I already have a computer", desc: "Any always-on Windows, Mac or Linux machine.", price: 0,   def: false },
        { id: "pi5",  name: "Raspberry Pi 5 (8 GB)",     desc: "Sips power, runs for months. The standalone pick.", price: 120, tag: "recommended", def: true },
        { id: "pi4",  name: "Raspberry Pi 4B",           desc: "Budget option — perfectly capable of a node.",      price: 70,  tag: "budget" },
        { id: "mac",  name: "Mac Mini",                  desc: "Overkill, but silent and rock-solid.",              price: 599, tag: "premium" }
      ],
      addons: [
        { cat: "Power & autonomy", items: [
          { id: "power",     name: "Smart power box",      desc: "Remotely power-cycle a hung Seestar — recover with no human present.", price: 60,  rel: 0.08, target: "g-power" },
          { id: "switchbot", name: "SwitchBot controls",   desc: "Automate physical buttons and switches the Node Agent can't reach.",   price: 30,  rel: 0.03 },
          { id: "ups",       name: "UPS / battery backup", desc: "Brief power cuts don't kill the night — vital on unstable grids.",      price: 70,  rel: 0.06, target: "g-ups" },
          { id: "solar",     name: "Solar power kit",      desc: "Run a node off-grid, in a genuinely dark-sky location.",                price: 300, rel: 0.02 }
        ]},
        { cat: "Protection", items: [
          { id: "enclosure", name: "Minidome + weather sensors", desc: "Observe through light rain, wind and heavy dew. Biggest single uptime boost.", price: 900, rel: 0.14, target: "g-enclosure" },
          { id: "dew",       name: "Dew heater",                 desc: "Prevents lens fogging. Without it a humid night is silently wasted.",          price: 30,  rel: 0.10, target: "g-dew" }
        ]},
        { cat: "Connectivity", items: [
          { id: "wifi",      name: "WiFi extender / mesh node",  desc: "For a scope placed well away from the router.",                                price: 50,  rel: 0.05 }
        ]}
      ]
    };
    // flat add-on lookup
    var ADDONS = {};
    CATALOG.addons.forEach(function (g) { g.items.forEach(function (it) { ADDONS[it.id] = it; }); });

    // ---- product art -----------------------------------------------
    // Self-contained, studio-render style SVG illustrations of the real
    // hardware. Gradient ids are namespaced per call (uid) so the same
    // part can render in the hero stage AND a card thumbnail at once.
    // Drop a real product photo into the same slot later if you prefer.
    var artId = 0;

    function telescope(variant) {                       // s50 / s30 / own
      var uid = "t" + (artId++);
      var compact = variant === "s30";
      var shell = compact ? "#cfd4dd" : "#e7eaf0";      // S30 is the lighter grey body
      var shellLo = compact ? "#9aa1ad" : "#b9bfca";
      var scale = compact ? 0.92 : 1;
      return '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<defs>' +
          '<linearGradient id="bd' + uid + '" x1="0" y1="0" x2="1" y2="0.2">' +
            '<stop offset="0" stop-color="' + shell + '"/>' +
            '<stop offset="0.5" stop-color="' + shellLo + '"/>' +
            '<stop offset="1" stop-color="' + shell + '"/></linearGradient>' +
          '<radialGradient id="ln' + uid + '" cx="0.38" cy="0.32" r="0.8">' +
            '<stop offset="0" stop-color="#1b3b6b"/>' +
            '<stop offset="0.45" stop-color="#0a1426"/>' +
            '<stop offset="1" stop-color="#05080f"/></radialGradient>' +
          '<linearGradient id="mt' + uid + '" x1="0" y1="0" x2="0" y2="1">' +
            '<stop offset="0" stop-color="#2c2f38"/><stop offset="1" stop-color="#15171d"/></linearGradient>' +
        '</defs>' +
        '<g transform="translate(100 108) scale(' + scale + ') translate(-100 -108)">' +
          // soft ground shadow
          '<ellipse cx="100" cy="183" rx="46" ry="9" fill="#000" opacity="0.45"/>' +
          // tripod
          '<g stroke="#3b3f49" stroke-width="5" stroke-linecap="round">' +
            '<line x1="100" y1="150" x2="74" y2="182"/><line x1="100" y1="150" x2="126" y2="182"/><line x1="100" y1="150" x2="100" y2="184"/></g>' +
          '<g fill="#23262e"><circle cx="74" cy="183" r="3.4"/><circle cx="126" cy="183" r="3.4"/><circle cx="100" cy="185" r="3.4"/></g>' +
          // alt-az base + arm
          '<rect x="84" y="138" width="32" height="16" rx="5" fill="url(#mt' + uid + ')"/>' +
          '<rect x="118" y="74" width="14" height="68" rx="6" fill="url(#mt' + uid + ')"/>' +
          // body
          '<rect x="70" y="46" width="50" height="96" rx="22" fill="url(#bd' + uid + ')" stroke="#7c828f" stroke-width="0.8"/>' +
          // left rim light + front face panel
          '<rect x="72.5" y="48.5" width="9" height="91" rx="9" fill="#fff" opacity="0.35"/>' +
          '<rect x="80" y="58" width="30" height="76" rx="13" fill="#101319" opacity="0.92"/>' +
          // objective lens (glossy)
          '<circle cx="95" cy="74" r="19" fill="#05070c"/>' +
          '<circle cx="95" cy="74" r="16" fill="url(#ln' + uid + ')"/>' +
          '<circle cx="95" cy="74" r="16" fill="none" stroke="#3a567f" stroke-width="1.2" opacity="0.7"/>' +
          '<ellipse cx="89" cy="68" rx="6" ry="4" fill="#bcd2f5" opacity="0.55" transform="rotate(-25 89 68)"/>' +
          // status LED (alive)
          '<circle cx="95" cy="120" r="3.2" fill="#54D98C"/>' +
          '<circle class="art-led" cx="95" cy="120" r="3.2" fill="#54D98C"/>' +
        '</g></svg>';
    }

    function pi(variant) {                               // pi5 / pi4
      var uid = "p" + (artId++);
      var board = variant === "pi4" ? "#1f7a44" : "#0f8a4f";
      return '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<defs><linearGradient id="pb' + uid + '" x1="0" y1="0" x2="1" y2="1">' +
          '<stop offset="0" stop-color="' + board + '"/><stop offset="1" stop-color="#0a5e36"/></linearGradient>' +
          '<linearGradient id="ch' + uid + '" x1="0" y1="0" x2="1" y2="1">' +
            '<stop offset="0" stop-color="#41454e"/><stop offset="1" stop-color="#16181d"/></linearGradient></defs>' +
        '<g transform="rotate(-16 100 100)">' +
          '<ellipse cx="100" cy="150" rx="58" ry="11" fill="#000" opacity="0.4"/>' +
          // board
          '<rect x="44" y="58" width="112" height="78" rx="7" fill="url(#pb' + uid + ')" stroke="#0a4d2c" stroke-width="1"/>' +
          '<rect x="46" y="60" width="108" height="4" rx="2" fill="#fff" opacity="0.12"/>' +
          // GPIO header (gold pins)
          '<rect x="52" y="63" width="74" height="9" rx="2" fill="#0a3a22"/>' +
          '<g fill="#E8C24A">' +
            Array.apply(null, Array(20)).map(function (_, i) { return '<rect x="' + (54 + i * 3.6) + '" y="64.5" width="2" height="6" rx="0.6"/>'; }).join("") +
          '</g>' +
          // SoC chip
          '<rect x="86" y="88" width="30" height="30" rx="3" fill="url(#ch' + uid + ')"/>' +
          '<rect x="92" y="94" width="18" height="18" rx="1.5" fill="#23262d"/>' +
          // RAM + USB block
          '<rect x="56" y="96" width="22" height="14" rx="2" fill="url(#ch' + uid + ')"/>' +
          '<rect x="126" y="86" width="22" height="20" rx="2" fill="#aeb4be"/>' +
          '<rect x="126" y="110" width="22" height="18" rx="2" fill="#5a8fd6"/>' +
          // mounting holes
          '<circle cx="52" cy="66" r="2.4" fill="#0a3a22" stroke="#cfd6cf" stroke-width="0.8"/>' +
          '<circle cx="148" cy="128" r="2.4" fill="#0a3a22" stroke="#cfd6cf" stroke-width="0.8"/>' +
        '</g></svg>';
    }

    function macmini() {
      var uid = "m" + (artId++);
      return '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<defs><linearGradient id="al' + uid + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0" stop-color="#e9ebee"/><stop offset="1" stop-color="#b6bbc2"/></linearGradient></defs>' +
        '<ellipse cx="100" cy="146" rx="60" ry="11" fill="#000" opacity="0.4"/>' +
        '<rect x="44" y="78" width="112" height="56" rx="14" fill="url(#al' + uid + ')" stroke="#9aa0a8" stroke-width="1"/>' +
        '<rect x="44" y="118" width="112" height="16" rx="14" fill="#2a2d33"/>' +
        '<rect x="46" y="80" width="108" height="6" rx="3" fill="#fff" opacity="0.5"/>' +
        '<circle cx="100" cy="100" r="9" fill="none" stroke="#9aa0a8" stroke-width="2"/>' +
        '<circle cx="100" cy="100" r="2" fill="#9aa0a8"/>' +
        '<circle cx="138" cy="126" r="2.4" fill="#54D98C"/>' +
        "</svg>";
    }

    function laptop() {                                  // "I already have a computer"
      var uid = "c" + (artId++);
      return '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<defs><linearGradient id="lc' + uid + '" x1="0" y1="0" x2="0" y2="1">' +
          '<stop offset="0" stop-color="#3a3f49"/><stop offset="1" stop-color="#22252c"/></linearGradient></defs>' +
        '<ellipse cx="100" cy="150" rx="62" ry="10" fill="#000" opacity="0.4"/>' +
        '<rect x="56" y="56" width="88" height="58" rx="6" fill="url(#lc' + uid + ')" stroke="#565b66" stroke-width="1"/>' +
        '<rect x="62" y="62" width="76" height="46" rx="3" fill="#0c1422"/>' +
        '<rect x="66" y="66" width="40" height="3" rx="1.5" fill="#25E8A0" opacity="0.7"/>' +
        '<rect x="66" y="74" width="58" height="2" rx="1" fill="#3a567f" opacity="0.6"/>' +
        '<path d="M44 114 H156 L150 134 H50 Z" fill="#9aa0a8"/>' +
        '<path d="M44 114 H156 L154.5 119 H45.5 Z" fill="#c7ccd3"/>' +
        "</svg>";
    }

    // small filled glyphs for add-ons (badges + thumbnails)
    function glyph(id) {
      var c = { power: "#25E8A0", switchbot: "#8FEFC9", ups: "#54D98C", solar: "#E8A93A",
                enclosure: "#25E8A0", dew: "#E8A93A", wifi: "#25E8A0" }[id] || "#25E8A0";
      var inner = {
        power:     '<rect x="78" y="70" width="44" height="60" rx="8"/><line x1="88" y1="58" x2="88" y2="74" stroke="' + c + '" stroke-width="6" stroke-linecap="round"/><line x1="112" y1="58" x2="112" y2="74" stroke="' + c + '" stroke-width="6" stroke-linecap="round"/><line x1="100" y1="130" x2="100" y2="146" stroke="' + c + '" stroke-width="6" stroke-linecap="round"/>',
        switchbot: '<rect x="68" y="72" width="64" height="56" rx="10"/><circle cx="100" cy="100" r="13" fill="#0b0b0c"/>',
        ups:       '<rect x="70" y="76" width="56" height="48" rx="6"/><rect x="126" y="90" width="8" height="20" rx="2"/><path d="M104 84 L92 104 H100 L96 116 L110 96 H102 Z" fill="#0b0b0c"/>',
        solar:     '<rect x="66" y="74" width="68" height="44" rx="4"/><g stroke="#0b0b0c" stroke-width="3"><line x1="88" y1="76" x2="88" y2="116"/><line x1="112" y1="76" x2="112" y2="116"/><line x1="68" y1="92" x2="132" y2="92"/></g>',
        enclosure: '<path d="M58 122 A42 38 0 0 1 142 122 Z"/><line x1="52" y1="122" x2="148" y2="122" stroke="' + c + '" stroke-width="6" stroke-linecap="round"/>',
        dew:       '<circle cx="100" cy="100" r="24" fill="none" stroke="' + c + '" stroke-width="7"/><g stroke="' + c + '" stroke-width="6" stroke-linecap="round"><path d="M84 70 q6 -8 0 -16"/><path d="M100 66 q6 -8 0 -16"/><path d="M116 70 q6 -8 0 -16"/></g>',
        wifi:      '<g fill="none" stroke="' + c + '" stroke-width="7" stroke-linecap="round"><path d="M66 96 a48 48 0 0 1 68 0"/><path d="M78 110 a30 30 0 0 1 44 0"/></g><circle cx="100" cy="126" r="6" fill="' + c + '"/>'
      }[id] || '<circle cx="100" cy="100" r="30"/>';
      return '<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" fill="' + c + '">' + inner + "</svg>";
    }

    function compArt(id) { return id === "pi5" ? pi("pi5") : id === "pi4" ? pi("pi4") : id === "mac" ? macmini() : laptop(); }
    function scopeArt(id) { return telescope(id === "s30" ? "s30" : "s50"); }

    // ---- real product photography (with the studio SVG as graceful fallback)
    // real photography for the telescopes (the hero hardware); the computers
    // keep their crisp studio-SVG renders — consistent and instant.
    var PHOTO = {
      s50: "https://us.seestar.com/cdn/shop/files/seestar_s50.jpg?v=1767929676&width=480"
    };
    function productMedia(group, id) {
      var svg = group === "telescope" ? scopeArt(id) : compArt(id);
      var url = PHOTO[id];
      if (!url) return '<span class="pmedia"><span class="pmedia-svg">' + svg + "</span></span>";
      // photo first; if it 404s / is blocked, reveal the SVG render instead
      return '<span class="pmedia"><img class="pphoto" src="' + url + '" alt="" loading="lazy" ' +
        "onerror=\"this.style.display='none';this.nextElementSibling.style.display='flex'\" />" +
        '<span class="pmedia-svg" style="display:none">' + svg + "</span></span>";
    }

    var owned = {};            // ids the inventory quiz marks as already-owned (price → 0)
    var STEPS = 5, step = 0;

    // ---- render option lists ---------------------------------------
    function cardArt(group, it) {
      // real product photo where we have one (studio SVG render otherwise)
      var cls = PHOTO[it.id] ? "opt-thumb photo" : "opt-thumb";
      return '<span class="' + cls + '">' + productMedia(group, it.id) + "</span>";
    }
    function radioCard(group, it) {
      var tag = it.tag ? '<span class="opt-tag">' + it.tag + "</span>" : "";
      var price = it.price === 0 ? "free" : "$" + it.price;
      var sci = it.science ? '<span class="opt-sci" title="science-impact score">★ ' + it.science.toFixed(2) + "</span>" : "";
      return '<label class="opt"><input type="radio" name="' + group + '" value="' + it.id + '"' + (it.def ? " checked" : "") + ' />' +
        '<div class="opt-card">' + cardArt(group, it) + '<span class="opt-radio"></span>' +
        '<div class="opt-info"><div class="t">' + it.name + tag + '</div><div class="d">' + it.desc + "</div></div>" +
        '<div class="opt-right">' + sci + '<span class="opt-delta tier">' + price + "</span></div></div></label>";
    }
    function checkCard(it) {
      return '<label class="opt"><input type="checkbox" name="addon" value="' + it.id + '"' +
        (it.target ? ' data-target="' + it.target + '"' : "") + ' data-rel="' + it.rel + '" />' +
        '<div class="opt-card"><span class="opt-thumb glyph">' + glyph(it.id) + '</span>' +
        '<div class="opt-info"><div class="t">' + it.name + '</div>' +
        '<div class="opt-gain"><strong>+' + Math.round(it.rel * 135) + ' nights/yr</strong> · $' + it.price + "</div></div>" +
        '<span class="opt-check"></span></div></label>';
    }
    document.querySelector('[data-group="telescope"]').innerHTML = CATALOG.telescope.map(function (it) { return radioCard("telescope", it); }).join("");
    document.querySelector('[data-group="computer"]').innerHTML = CATALOG.computer.map(function (it) { return radioCard("computer", it); }).join("");
    // add-ons: a single flat grid grouped by category label — no accordion,
    // so the whole step fits the modal without scrolling
    $("accord").className = "addon-grid";
    $("accord").innerHTML = CATALOG.addons.map(function (g) {
      return '<div class="addon-cat">' + g.cat + "</div>" + g.items.map(checkCard).join("");
    }).join("");

    // ---- summary recompute -----------------------------------------
    var relVal = $("rel-val"), relFill = $("rel-fill"), relStage = $("rel-stage"),
        tierName = $("tier-name"), unlocks = $("unlocks"),
        costVal = $("cost-val"), costRange = $("cost-range"),
        nodeArt = $("node-art"), badgeWrap = $("addon-badges"),
        impHead = $("impact-head"), impNights = $("imp-nights"), impObs = $("imp-obs"),
        impAavso = $("imp-aavso"), impEvents = $("imp-events"),
        tonightEl = $("stage-tonight"), toastStack = $("toast-stack");

    // ---- projection model: turn "readiness" into science you'd actually do
    var CLEAR_NIGHTS = 135;        // typical usable clear nights/yr at a home site
    var TONIGHT = [               // real targets the scheduler would actually assign
      { n: "SS Cyg", t: "dwarf nova" },   { n: "T CrB", t: "recurrent nova" },
      { n: "R Leo", t: "Mira variable" }, { n: "Z Cam", t: "Z Cam-type CV" },
      { n: "RS Oph", t: "symbiotic nova" }, { n: "χ Cyg", t: "long-period var" }
    ];
    var CAP_MSG = {               // the reward line fired when each capability is added
      power:     ["Self-healing", "Recovers from a hung scope — nobody present."],
      ups:       ["Blackout-proof", "A power cut no longer ends the night."],
      enclosure: ["All-weather", "Keep observing through rain, wind &amp; dew."],
      dew:       ["Dew-proof optics", "Humid nights are no longer wasted."],
      wifi:      ["Rock-solid uplink", "No more dropped uploads."],
      solar:     ["Off-grid ready", "Run from a genuinely dark sky."],
      switchbot: ["Hands-free controls", "Physical switches, automated."]
    };

    function priceOf(id, base) { return owned[id] ? 0 : base; }
    function colorFor(rel) { return rel >= 0.85 ? "#54D98C" : rel >= 0.65 ? "#25E8A0" : "#E8A93A"; }

    // ---- hero stage: crossfade the render when the scope/computer changes
    var artKey = "";
    function renderStage(s) {
      var key = s.scope.id + "|" + s.comp.id;
      if (key === artKey || !nodeArt) return;
      artKey = key;
      nodeArt.innerHTML =
        '<div class="art-main">' + scopeArt(s.scope.id) + "</div>" +
        '<div class="art-side">' + compArt(s.comp.id) + "</div>";
      nodeArt.classList.remove("art-pop");
      void nodeArt.offsetWidth;          // restart the entrance animation
      nodeArt.classList.add("art-pop");
    }

    // ---- floating add-on badges, each popping in as it's equipped
    var badgeKeys = "";
    function renderBadges(s) {
      if (!badgeWrap) return;
      var key = s.adds.map(function (a) { return a.id; }).sort().join(",");
      if (key === badgeKeys) return;
      badgeKeys = key;
      badgeWrap.innerHTML = s.adds.map(function (a, i) {
        return '<span class="addon-badge" style="animation-delay:' + (i * 70) + 'ms" title="' + a.name + '">' +
          glyph(a.id) + "</span>";
      }).join("");
    }

    // ---- count the cost up/down so the number feels alive
    var costShown = 0, costTimer = null;
    function tweenCost(to) {
      var from = costShown, start = null, dur = 520;
      function step(ts) {
        if (!start) start = ts;
        var p = Math.min((ts - start) / dur, 1), e = 1 - Math.pow(1 - p, 3);
        costVal.textContent = "$" + Math.round(from + (to - from) * e).toLocaleString();
        if (p < 1) requestAnimationFrame(step); else costShown = to;
      }
      if (to !== costShown) {
        costVal.classList.remove("cost-bump"); void costVal.offsetWidth; costVal.classList.add("cost-bump");
        requestAnimationFrame(step);
        // guarantee the final value even when rAF is throttled (background/preview)
        clearTimeout(costTimer);
        costTimer = setTimeout(function () { costVal.textContent = "$" + to.toLocaleString(); costShown = to; }, dur + 150);
      }
    }

    // ---- count any number up/down so the payoff feels alive
    function tweenNum(el, to) {
      if (!el) return;
      var from = el._n || 0, start = null, dur = 600;
      if (to === from) return;
      function step(ts) {
        if (!start) start = ts;
        var p = Math.min((ts - start) / dur, 1), e = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(from + (to - from) * e).toLocaleString();
        if (p < 1) requestAnimationFrame(step); else el._n = to;
      }
      el.classList.remove("ival-bump"); void el.offsetWidth; el.classList.add("ival-bump");
      requestAnimationFrame(step);
      // guarantee the final value even when rAF is throttled (background/preview)
      clearTimeout(el._t);
      el._t = setTimeout(function () { el.textContent = to.toLocaleString(); el._n = to; }, dur + 150);
    }

    // ---- the live "tonight's run": real targets this node would chase tonight,
    //      the queue growing as the build gets stronger
    var tonightKey = "";
    function renderTonight(s, rel) {
      if (!tonightEl) return;
      var count = Math.max(2, Math.min(TONIGHT.length, Math.round(rel * TONIGHT.length)));
      var key = s.scope.id + "|" + count;
      if (key === tonightKey) return;
      tonightKey = key;
      var rows = TONIGHT.slice(0, count).map(function (o, i) {
        return '<li class="' + (i === 0 ? "tn-now" : "") + '" title="' + o.t + '">' + o.n + "</li>";
      }).join("");
      tonightEl.innerHTML =
        '<div class="tn-head"><span class="tn-dot"></span> tonight’s run · ' + count + " targets</div>" +
        '<ul class="tn-list">' + rows + "</ul>";
    }

    // ---- per-click reward toasts: name the capability the moment it's unlocked
    var toastArmed = false, prevAdds = {}, prevMilestone = 0;
    var STAR = '<svg viewBox="0 0 200 200" fill="currentColor" aria-hidden="true"><path d="M100 18 l22 56 60 4 -46 39 15 59 -51 -33 -51 33 15 -59 -46 -39 60 -4 Z"/></svg>';
    function milestoneOf(rel) { return rel >= 1 ? 2 : rel >= 0.85 ? 1 : 0; }
    function pushToast(icon, title, sub, accent) {
      if (!toastStack) return;
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      var t = document.createElement("div");
      t.className = "toast";
      if (accent) t.style.setProperty("--toast-accent", accent);
      t.innerHTML = '<span class="toast-ic">' + icon + "</span>" +
        '<div class="toast-tx"><div class="toast-t">' + title + '</div><div class="toast-s">' + sub + "</div></div>";
      toastStack.appendChild(t);
      while (toastStack.children.length > 3) toastStack.removeChild(toastStack.firstChild);
      void t.offsetWidth; t.classList.add("in");
      setTimeout(function () {
        t.classList.add("out");
        setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 380);
      }, reduce ? 2200 : 3400);
    }
    function fireToasts(s, rel) {
      var ids = {}; s.adds.forEach(function (a) { ids[a.id] = 1; });
      if (!toastArmed) { prevAdds = ids; prevMilestone = milestoneOf(rel); toastArmed = true; return; }
      var added = s.adds.filter(function (a) { return !prevAdds[a.id] && CAP_MSG[a.id]; });
      if (added.length > 2) {
        pushToast(STAR, added.length + " upgrades equipped", "Your node just leveled up.", "#8FEFC9");
      } else {
        added.forEach(function (a) { var m = CAP_MSG[a.id]; pushToast(glyph(a.id), m[0], m[1]); });
      }
      var ms = milestoneOf(rel);
      if (ms > prevMilestone) {
        if (ms === 1) pushToast(STAR, "Mission ready", "Rapid follow-up on the unexpected — unlocked.", "#54D98C");
        if (ms === 2) pushToast(STAR, "Flagship node", "Full scheduler priority — top of the queue.", "#E8A93A");
      }
      prevAdds = ids; prevMilestone = ms;
    }

    function selected() {
      var scope = CATALOG.telescope.filter(function (t) { return t.id === document.querySelector('input[name="telescope"]:checked').value; })[0];
      var comp  = CATALOG.computer.filter(function (c) { return c.id === document.querySelector('input[name="computer"]:checked').value; })[0];
      var adds = [];
      Array.prototype.forEach.call(document.querySelectorAll('input[name="addon"]:checked'), function (cb) { adds.push(ADDONS[cb.value]); });
      return { scope: scope, comp: comp, adds: adds };
    }

    function recompute() {
      var s = selected();
      // realistic render + floating add-on badges
      renderStage(s);
      renderBadges(s);

      // node readiness
      var rel = FLOOR;
      s.adds.forEach(function (a) { rel += a.rel; });
      rel = Math.min(1, rel);
      var col = colorFor(rel);
      relVal.textContent = rel.toFixed(2); relVal.style.color = col;
      relFill.style.width = (rel * 100).toFixed(0) + "%"; relFill.style.background = col;
      if (relStage) relStage.textContent =
        rel >= 1 ? "flagship" : rel >= 0.85 ? "mission ready" : rel >= 0.65 ? "hardened" : "new node";
      // the node powers up and glows once it's mission-ready
      if (nodeArt) nodeArt.classList.toggle("art-live", rel >= 0.85);

      // THE PAYOFF — projected science output, counting up so it lands
      var hasDome = owned.enclosure || s.adds.some(function (a) { return a.id === "enclosure"; });
      var nights = Math.round(CLEAR_NIGHTS * rel);
      var obs = Math.round(nights * 3.4);
      var aavso = Math.round(obs * 0.88);
      var events = Math.round(rel * 6 * (hasDome ? 1.5 : 1));
      tweenNum(impNights, nights);
      tweenNum(impObs, obs);
      tweenNum(impAavso, aavso);
      tweenNum(impEvents, events);
      if (impHead) impHead.textContent =
        rel >= 1 ? "A flagship node. Top of the queue."
        : rel >= 0.85 ? "Mission-ready — first to the unexpected."
        : rel >= 0.65 ? "Unattended science while you sleep."
        : "A real node, every clear night.";

      // the live "tonight's run"
      renderTonight(s, rel);

      // cost (count-up so the number feels alive)
      var total = priceOf(s.scope.id, s.scope.price) + priceOf(s.comp.id, s.comp.price);
      s.adds.forEach(function (a) { total += priceOf(a.id, a.price); });
      tweenCost(total);
      costRange.textContent = total === 0 ? "you own it all"
        : "$" + Math.round(total * 0.9).toLocaleString() + " – $" + Math.round(total * 1.12).toLocaleString();

      // scope caption
      tierName.textContent = s.scope.name + " · Tier " + s.scope.tier;

      // unlocked science
      var chips = ["variable stars", "novae", "CV outbursts"];
      if (hasDome) chips.push("all-weather coverage");
      if (rel >= 0.85) chips.push("rapid transient follow-up");
      unlocks.innerHTML = chips.map(function (c) { return '<span class="chip">' + c + "</span>"; }).join("");

      // per-click reward toasts
      fireToasts(s, rel);

      renderReview(s, total, { aavso: aavso, nights: nights });
    }

    function renderReview(s, total, proj) {
      var rows = [];
      function row(it, label) {
        var cost = owned[it.id] ? '<span class="rv-own">you own this</span>'
          : (it.price === 0 ? '<span class="rv-own">included free</span>' : '<span class="mono">$' + it.price.toLocaleString() + "</span>");
        rows.push('<li><div><div class="rv-name">' + it.name + '</div><div class="rv-tag">' + label + '</div></div>' +
          '<div class="rv-right">' + cost + ' <a class="rv-buy" href="#" title="opens the retailer">buy <i class="ti ti-external-link"></i></a></div></li>');
      }
      row(s.scope, "Telescope");
      row(s.comp, "Computer");
      s.adds.forEach(function (a) { row(a, "Add-on"); });
      $("review-list").innerHTML = rows.join("");
      $("review-total").textContent = "$" + total.toLocaleString();
      var payoff = $("review-payoff");
      if (payoff && proj) {
        payoff.innerHTML = "Your node could catch <strong>~" + proj.aavso.toLocaleString() +
          " observations a year</strong> — every one credited to your name, in a database professional astronomers actually pull from.";
      }
    }

    // ---- inventory quiz → drives defaults --------------------------
    function applyQuiz() {
      owned = {};
      var hint = $("quiz-hint");
      var hasSeestar = false;
      Array.prototype.forEach.call(document.querySelectorAll('input[name="have"]:checked'), function (cb) {
        var v = cb.value;
        if (v === "seestar") { hasSeestar = true; setRadio("telescope", "own"); }
        else if (v === "computer") { setRadio("computer", "own"); }
        else { owned[v] = true; setCheck("addon", v, true); }   // enclosure / ups
      });
      if (!hasSeestar) {/* leave telescope on its default */}
      if (hint) hint.style.display = hasSeestar ? "block" : "none";
      recompute();
    }
    function setRadio(group, val) {
      var el = document.querySelector('input[name="' + group + '"][value="' + val + '"]');
      if (el) el.checked = true;
    }
    function setCheck(group, val, on) {
      var el = document.querySelector('input[name="' + group + '"][value="' + val + '"]');
      if (el) el.checked = on;
    }

    // ---- wizard navigation -----------------------------------------
    var panels = document.querySelectorAll(".wiz-panel"),
        pills  = document.querySelectorAll("#wiz-steps li"),
        back = $("wiz-back"), next = $("wiz-next"), count = $("wiz-count");

    function show(n) {
      step = Math.max(0, Math.min(STEPS - 1, n));
      Array.prototype.forEach.call(panels, function (p) { p.hidden = +p.dataset.step !== step; });
      Array.prototype.forEach.call(pills, function (li, i) {
        li.classList.toggle("active", i === step);
        li.classList.toggle("done", i < step);
      });
      count.textContent = "Step " + (step + 1) + " of " + STEPS;
      back.style.visibility = step === 0 ? "hidden" : "visible";
      next.innerHTML = step === STEPS - 1 ? 'Done <i class="ti ti-check"></i>' : 'Next <i class="ti ti-arrow-right"></i>';
      if (step === STEPS - 1) { recompute(); celebrate(); }
    }

    // ---- "node assembled" moment: a one-shot shimmer of stars --------
    var burst = $("assembled-burst"), celebrated = false;
    function celebrate() {
      if (!burst) return;
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduce) return;
      var bits = [];
      for (var i = 0; i < 22; i++) {
        var ang = Math.random() * 6.2832, dist = 40 + Math.random() * 90;
        bits.push('<i style="--tx:' + (Math.cos(ang) * dist).toFixed(0) + 'px;--ty:' +
          (Math.sin(ang) * dist).toFixed(0) + 'px;--d:' + (Math.random() * 160).toFixed(0) +
          'ms;background:' + (["#E8A93A", "#25E8A0", "#54D98C", "#8FEFC9"][i % 4]) + '"></i>');
      }
      burst.innerHTML = bits.join("");
      burst.classList.remove("go"); void burst.offsetWidth; burst.classList.add("go");
      // gentle ribbon flash on the stage only the first time, so it stays tasteful
      if (!celebrated && nodeArt) { nodeArt.classList.add("art-cheer"); celebrated = true;
        setTimeout(function () { nodeArt.classList.remove("art-cheer"); }, 1200); }
    }
    back.addEventListener("click", function () { show(step - 1); });
    next.addEventListener("click", function () {
      if (step === STEPS - 1) { celebrate(); return; }   // already on review — re-fire the moment
      show(step + 1);
    });
    Array.prototype.forEach.call(pills, function (li) {
      li.addEventListener("click", function () { show(+li.dataset.go); });
    });

    // ---- modal open / close ----------------------------------------
    var modal = $("builder-modal"), lastFocus = null;
    function openModal() {
      if (!modal) return;
      lastFocus = document.activeElement;
      modal.classList.add("open");
      modal.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      recompute();
      var c = modal.querySelector(".bmodal-close"); if (c) c.focus();
    }
    function closeModal() {
      if (!modal) return;
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    if (modal) {
      Array.prototype.forEach.call(modal.querySelectorAll("[data-close]"), function (el) {
        el.addEventListener("click", closeModal);
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && modal.classList.contains("open")) closeModal();
      });
    }
    var openBtn = $("open-builder");
    if (openBtn) openBtn.addEventListener("click", function () { show(0); openModal(); });

    // ---- preset loader: open straight to a fully-assembled review ----
    var preset = $("load-preset");
    if (preset) preset.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll('input[name="have"]'), function (c) { c.checked = false; });
      owned = {};
      setRadio("telescope", "s50");
      setRadio("computer", "pi5");
      var on = { power: 1, switchbot: 1, ups: 1, enclosure: 1, wifi: 1 };
      Array.prototype.forEach.call(document.querySelectorAll('input[name="addon"]'), function (cb) { cb.checked = !!on[cb.value]; });
      recompute();
      show(STEPS - 1);                      // jump to review
      openModal();
    });

    // ---- wiring -----------------------------------------------------
    document.querySelectorAll('input[name="have"]').forEach(function (el) { el.addEventListener("change", applyQuiz); });
    document.querySelectorAll('input[name="telescope"], input[name="computer"], input[name="addon"]').forEach(function (el) {
      el.addEventListener("change", recompute);
    });

    // seed preset cost label, then render initial state
    (function () {
      var p = CATALOG.telescope[1].price + CATALOG.computer[1].price +
        ADDONS.power.price + ADDONS.switchbot.price + ADDONS.ups.price + ADDONS.enclosure.price + ADDONS.wifi.price;
      if ($("preset-cost")) $("preset-cost").textContent = "$" + p.toLocaleString();
    })();
    show(0);
    recompute();

    // ---- "Email me this build" → real mailto with parts list ----------
    var emailBtn = document.querySelector(".builder-cta .btn-plain");
    if (emailBtn) {
      emailBtn.addEventListener("click", function (e) {
        e.preventDefault();
        var s = selected();
        var lines = ["My Boundless Skies Node Build", ""];
        lines.push("Telescope : " + s.scope.name + (s.scope.price ? "  ($" + s.scope.price + ")" : "  (owned)"));
        lines.push("Computer  : " + s.comp.name  + (s.comp.price  ? "  ($" + s.comp.price  + ")" : "  (owned)"));
        if (s.adds.length) {
          lines.push(""); lines.push("Add-ons:");
          s.adds.forEach(function (a) { lines.push("  · " + a.name + "  ($" + a.price + ")"); });
        }
        var total = (owned[s.scope.id] ? 0 : s.scope.price) + (owned[s.comp.id] ? 0 : s.comp.price);
        s.adds.forEach(function (a) { total += owned[a.id] ? 0 : a.price; });
        lines.push(""); lines.push("Estimated total: $" + total.toLocaleString());
        lines.push(""); lines.push("Join the network: https://boundlessskies.org/#builder");
        window.location.href = "mailto:?subject=My%20Boundless%20Skies%20Node%20Build&body=" + encodeURIComponent(lines.join("\n"));
      });
    }
  }

  /* =========================================================
     5. SECTION ILLUSTRATIONS — step art, involve card art,
        constellation lines on the network map
     ========================================================= */
  function decorateSections() {
    var dc = 0;
    function did() { return "dc" + (dc++); }

    // Step 1: Seestar → upload beam → network node
    function stepArt0() {
      var b = did(), g = did();
      return '<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs>' +
        '<linearGradient id="' + b + '" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#25E8A0" stop-opacity="0.65"/><stop offset="1" stop-color="#25E8A0" stop-opacity="0"/></linearGradient>' +
        '<radialGradient id="' + g + '" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#25E8A0" stop-opacity="0.18"/><stop offset="1" stop-color="#25E8A0" stop-opacity="0"/></radialGradient></defs>' +
        '<circle cx="40" cy="40" r="36" fill="url(#' + g + ')"/>' +
        '<circle cx="12" cy="10" r="1" fill="#fff" opacity="0.55"/><circle cx="65" cy="8" r="1.2" fill="#fff" opacity="0.45"/><circle cx="18" cy="28" r="0.9" fill="#fff" opacity="0.4"/>' +
        '<rect x="37.5" y="12" width="5" height="24" fill="url(#' + b + ')"/>' +
        '<circle cx="40" cy="10" r="4.5" fill="none" stroke="#25E8A0" stroke-width="1.2" opacity="0.8"/><circle cx="40" cy="10" r="2" fill="#25E8A0"/>' +
        '<circle cx="40" cy="30" r="1.8" fill="#25E8A0" opacity="0.9"/><circle cx="40" cy="22" r="1.4" fill="#25E8A0" opacity="0.65"/>' +
        '<line x1="40" y1="62" x2="29" y2="73" stroke="#3b3f49" stroke-width="2" stroke-linecap="round"/>' +
        '<line x1="40" y1="62" x2="51" y2="73" stroke="#3b3f49" stroke-width="2" stroke-linecap="round"/>' +
        '<line x1="40" y1="62" x2="40" y2="74" stroke="#3b3f49" stroke-width="2" stroke-linecap="round"/>' +
        '<rect x="33" y="57" width="14" height="7" rx="2.5" fill="#23262e"/>' +
        '<rect x="27" y="38" width="20" height="22" rx="8" fill="#e7eaf0" stroke="#7c828f" stroke-width="0.6"/>' +
        '<rect x="28.5" y="39.5" width="3.5" height="19" rx="3.5" fill="#fff" opacity="0.3"/>' +
        '<rect x="47" y="42" width="4" height="14" rx="2" fill="#2c2f38"/>' +
        '<circle cx="37" cy="46" r="7" fill="#05070c"/><circle cx="37" cy="46" r="5.5" fill="#0a1426"/>' +
        '<ellipse cx="34.5" cy="43.5" rx="2" ry="1.3" fill="#bcd2f5" opacity="0.5" transform="rotate(-25 34.5 43.5)"/>' +
        '<circle cx="37" cy="56" r="1.5" fill="#54D98C"/>' +
        '<circle class="art-led" cx="37" cy="56" r="1.5" fill="#54D98C"/></svg>';
    }

    // Step 2: scheduler network hub
    function stepArt1() {
      var g = did();
      return '<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs>' +
        '<radialGradient id="' + g + '" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#25E8A0" stop-opacity="0.22"/><stop offset="1" stop-color="#25E8A0" stop-opacity="0"/></radialGradient></defs>' +
        '<circle cx="40" cy="40" r="36" fill="url(#' + g + ')"/>' +
        '<g stroke="rgba(37,232,160,0.22)" stroke-width="1"><line x1="40" y1="40" x2="14" y2="18"/><line x1="40" y1="40" x2="66" y2="18"/><line x1="40" y1="40" x2="12" y2="54"/><line x1="40" y1="40" x2="68" y2="54"/><line x1="40" y1="40" x2="40" y2="70"/></g>' +
        '<circle cx="14" cy="18" r="5" fill="#25E8A0" opacity="0.4"/><circle cx="14" cy="18" r="2.5" fill="#8FEFC9"/>' +
        '<circle cx="66" cy="18" r="5" fill="#25E8A0" opacity="0.4"/><circle cx="66" cy="18" r="2.5" fill="#8FEFC9"/>' +
        '<circle cx="12" cy="54" r="5" fill="#25E8A0" opacity="0.4"/><circle cx="12" cy="54" r="2.5" fill="#7FEAC4"/>' +
        '<circle cx="68" cy="54" r="5" fill="#25E8A0" opacity="0.4"/><circle cx="68" cy="54" r="2.5" fill="#7FEAC4"/>' +
        '<circle cx="40" cy="70" r="5" fill="#54D98C" opacity="0.4"/><circle cx="40" cy="70" r="2.5" fill="#54D98C"/>' +
        '<circle cx="40" cy="40" r="13" fill="#0C0D11" stroke="#25E8A0" stroke-width="1.5"/>' +
        '<circle cx="40" cy="40" r="10" fill="rgba(37,232,160,0.12)"/>' +
        '<g stroke="#8FEFC9" stroke-width="1.3" stroke-linecap="round" fill="none"><circle cx="40" cy="40" r="5.5"/><line x1="34.5" y1="40" x2="30" y2="40"/><line x1="45.5" y1="40" x2="50" y2="40"/><line x1="40" y1="34.5" x2="40" y2="30"/><line x1="40" y1="45.5" x2="40" y2="50"/></g></svg>';
    }

    // Step 3: AAVSO science certificate + star
    function stepArt2() {
      var g = did(), d = did();
      return '<svg viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs>' +
        '<radialGradient id="' + g + '" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#E8A93A" stop-opacity="0.18"/><stop offset="1" stop-color="#E8A93A" stop-opacity="0"/></radialGradient>' +
        '<linearGradient id="' + d + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#18192a"/><stop offset="1" stop-color="#0e0f18"/></linearGradient></defs>' +
        '<circle cx="40" cy="40" r="36" fill="url(#' + g + ')"/>' +
        '<g stroke="#E8A93A" stroke-width="1" opacity="0.4"><line x1="40" y1="8" x2="40" y2="14"/><line x1="60" y1="13" x2="56.5" y2="18.5"/><line x1="68" y1="30" x2="63" y2="32.5"/><line x1="20" y1="13" x2="23.5" y2="18.5"/><line x1="12" y1="30" x2="17" y2="32.5"/></g>' +
        '<rect x="18" y="28" width="44" height="40" rx="5" fill="url(#' + d + ')" stroke="rgba(232,169,58,0.3)" stroke-width="1"/>' +
        '<g stroke="rgba(245,244,241,0.18)" stroke-width="1.5" stroke-linecap="round"><line x1="26" y1="45" x2="54" y2="45"/><line x1="26" y1="51" x2="54" y2="51"/><line x1="26" y1="57" x2="42" y2="57"/></g>' +
        '<circle cx="40" cy="32" r="9" fill="#0a0b12" stroke="#E8A93A" stroke-width="1.4"/>' +
        '<path d="M40 25.2 L41.7 29.8 L46.6 29.8 L42.8 32.6 L44.2 37.2 L40 34.5 L35.8 37.2 L37.2 32.6 L33.4 29.8 L38.3 29.8 Z" fill="#E8A93A"/>' +
        '<text x="40" y="64" text-anchor="middle" font-family="JetBrains Mono,monospace" font-size="5.5" fill="rgba(232,169,58,0.65)" letter-spacing="0.5">AAVSO</text></svg>';
    }

    // Involve Card 1: telescope + orbiting stars
    function involveArt0() {
      var g = did();
      return '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs>' +
        '<radialGradient id="' + g + '" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#25E8A0" stop-opacity="0.18"/><stop offset="1" stop-color="#25E8A0" stop-opacity="0"/></radialGradient></defs>' +
        '<circle cx="32" cy="32" r="29" fill="url(#' + g + ')" stroke="rgba(37,232,160,0.2)" stroke-width="1"/>' +
        '<line x1="32" y1="48" x2="22" y2="57" stroke="#3b3f49" stroke-width="2" stroke-linecap="round"/>' +
        '<line x1="32" y1="48" x2="42" y2="57" stroke="#3b3f49" stroke-width="2" stroke-linecap="round"/>' +
        '<rect x="26" y="46" width="12" height="5" rx="2" fill="#23262e"/>' +
        '<rect x="22" y="30" width="20" height="20" rx="8" fill="#e7eaf0" stroke="#9aa0a8" stroke-width="0.7"/>' +
        '<rect x="42" y="34" width="4" height="14" rx="2" fill="#2c2f38"/>' +
        '<circle cx="32" cy="38" r="7" fill="#05070c"/><circle cx="32" cy="38" r="5.5" fill="#0a1426"/>' +
        '<ellipse cx="29.5" cy="35.5" rx="2" ry="1.3" fill="#bcd2f5" opacity="0.5" transform="rotate(-25 29.5 35.5)"/>' +
        '<circle cx="32" cy="47" r="1.4" fill="#54D98C"/>' +
        '<circle cx="16" cy="18" r="2.2" fill="#25E8A0" opacity="0.85"/>' +
        '<circle cx="32" cy="10" r="1.8" fill="#25E8A0" opacity="0.85"/>' +
        '<circle cx="48" cy="16" r="2" fill="#25E8A0" opacity="0.75"/></svg>';
    }

    // Involve Card 2: heart containing telescope
    function involveArt1() {
      return '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<circle cx="32" cy="32" r="29" fill="rgba(240,101,95,0.1)" stroke="rgba(240,101,95,0.2)" stroke-width="1"/>' +
        '<path d="M32 46 C20 38 11 30 15 22 A9.5 9.5 0 0 1 32 24 A9.5 9.5 0 0 1 49 22 C53 30 44 38 32 46 Z" fill="rgba(240,101,95,0.18)" stroke="#F0655F" stroke-width="1.3"/>' +
        '<line x1="32" y1="40" x2="26" y2="46" stroke="#5a3a3a" stroke-width="1.5" stroke-linecap="round"/>' +
        '<line x1="32" y1="40" x2="38" y2="46" stroke="#5a3a3a" stroke-width="1.5" stroke-linecap="round"/>' +
        '<rect x="24" y="25" width="16" height="17" rx="7" fill="#e7eaf0" stroke="#9aa0a8" stroke-width="0.6"/>' +
        '<rect x="40" y="28" width="3.5" height="11" rx="1.8" fill="#2c2f38"/>' +
        '<circle cx="32" cy="32" r="6" fill="#05070c"/><circle cx="32" cy="32" r="4.5" fill="#0a1426"/>' +
        '<ellipse cx="29.5" cy="29.5" rx="1.7" ry="1.1" fill="#bcd2f5" opacity="0.5" transform="rotate(-25 29.5 29.5)"/></svg>';
    }

    // Involve Card 3: envelope with live light curve
    function involveArt2() {
      return '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
        '<circle cx="32" cy="32" r="29" fill="rgba(37,232,160,0.1)" stroke="rgba(37,232,160,0.2)" stroke-width="1"/>' +
        '<rect x="10" y="22" width="44" height="28" rx="5" fill="#0C0D11" stroke="rgba(37,232,160,0.4)" stroke-width="1.2"/>' +
        '<polyline points="10,22 32,38 54,22" stroke="rgba(37,232,160,0.5)" stroke-width="1.2" fill="none" stroke-linejoin="round"/>' +
        '<polyline points="14,46 19,44 24,47 29,39 34,41 39,35 44,40 50,36" stroke="#25E8A0" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="0.75"/>' +
        '<circle cx="50" cy="36" r="2.5" fill="#25E8A0"/>' +
        '<circle cx="39" cy="35" r="1.8" fill="#E8A93A"/></svg>';
    }

    // Inject step illustrations
    Array.prototype.forEach.call(document.querySelectorAll('.step'), function(el, i) {
      var ic = el.querySelector('.ic'); if (!ic) return;
      var arts = [stepArt0, stepArt1, stepArt2]; if (!arts[i]) return;
      var wrap = document.createElement('div'); wrap.className = 'step-art'; wrap.innerHTML = arts[i]();
      el.replaceChild(wrap, ic);
    });

    // Inject involve card illustrations
    Array.prototype.forEach.call(document.querySelectorAll('.involve-card'), function(el, i) {
      var ic = el.querySelector('.ic'); if (!ic) return;
      var arts = [involveArt0, involveArt1, involveArt2]; if (!arts[i]) return;
      var wrap = document.createElement('div'); wrap.className = 'involve-art'; wrap.innerHTML = arts[i]();
      el.replaceChild(wrap, ic);
    });

    // Stagger reveal delay on each child so they cascade in
    Array.prototype.forEach.call(
      document.querySelectorAll('.steps > .step, .involve-grid > .involve-card'),
      function(el, i) { el.style.transitionDelay = (i * 110) + 'ms'; }
    );
  }

  /* =========================================================
     6. SCROLL REVEAL + COUNT-UP + MOBILE NAV
     ========================================================= */
  function reveals() {
    var els = document.querySelectorAll(".reveal");
    if (!("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    els.forEach(function (el) { io.observe(el); });
  }
  function countUp(el, target, dur, suffix) {
    if (!el) return;
    suffix = suffix || "";
    var final = Math.round(target).toLocaleString() + suffix, start = null;
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min((ts - start) / dur, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.floor(target * e).toLocaleString() + suffix;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
    // guarantee the final value even when rAF is throttled (background/preview)
    setTimeout(function () { el.textContent = final; }, dur + 150);
  }
  function mobileNav() {
    var btn = document.querySelector(".nav-toggle"), links = document.querySelector(".nav-links");
    if (!btn || !links) return;
    btn.addEventListener("click", function () {
      var open = links.style.display === "flex";
      if (open) { links.style.display = ""; return; }
      links.style.cssText = "display:flex;position:absolute;flex-direction:column;top:64px;right:28px;background:var(--bg-elev);padding:16px 20px;border-radius:12px;border:0.5px solid var(--line);gap:14px";
    });
  }

  function heroParallax() {
    var hero = document.querySelector(".hero");
    var pMain  = document.querySelector(".panel-main");
    var pCurve = document.querySelector(".panel-curve");
    var pGauge = document.querySelector(".panel-gauge");
    if (!hero || !pMain) return;
    var mx = 0, my = 0, cx = 0, cy = 0;
    hero.addEventListener("mousemove", function (e) {
      var r = hero.getBoundingClientRect();
      mx = (e.clientX - r.left - r.width  * 0.5) / r.width;
      my = (e.clientY - r.top  - r.height * 0.5) / r.height;
    });
    hero.addEventListener("mouseleave", function () { mx = 0; my = 0; });
    (function tick() {
      cx += (mx - cx) * 0.07; cy += (my - cy) * 0.07;
      if (Math.abs(cx) > 0.0005 || Math.abs(mx) > 0.0005) {
        pMain.style.transform  = "rotateX(14deg) rotateZ(-0.4deg) translate(" + (cx * 12).toFixed(1) + "px," + (cy *  8).toFixed(1) + "px)";
        pCurve.style.transform = "rotateX(10deg) rotateZ(3deg)    translate(" + (cx * 26).toFixed(1) + "px," + (cy * 18).toFixed(1) + "px)";
        pGauge.style.transform = "rotateX(10deg) rotateZ(-3deg)   translate(" + (cx * 20).toFixed(1) + "px," + (cy * 14).toFixed(1) + "px)";
      }
      requestAnimationFrame(tick);
    })();
  }

  /* =========================================================
     6. LIVE DATA WIRING
     ========================================================= */
  var DEFAULT_TARGETS = [
    { name: "SS Cyg", mag: 8.4 }, { name: "T CrB", mag: 9.9 },
    { name: "R Leo", mag: 6.8 }, { name: "Z UMa", mag: 7.9 }, { name: "SS Aur", mag: 12.0 }
  ];

  function fillConsole(points, target) {
    var grid = $("obs-grid"); if (!grid || !points.length) return;
    // clear any previously-rendered rows (keeps the 5 header cells)
    Array.prototype.slice.call(grid.querySelectorAll(".obs-row")).forEach(function (r) { grid.removeChild(r); });
    var recent = points.slice(-4).reverse();
    recent.forEach(function (p, i) {
      var row = document.createElement("div"); row.className = "obs-row";
      var status = p.aavso_submitted ? '<span class="c-ok">accepted</span>'
        : (i === 0 ? '<span class="c-busy">observing…</span>' : '<span class="c-busy">queued</span>');
      row.innerHTML =
        '<span class="c-node">' + p.node_id + '</span>' +
        '<span class="c-tgt">' + (target || "SS Cyg") + '</span>' +
        '<span class="c-time">' + hhmmss(p.received_at) + '</span>' +
        '<span class="c-mag">' + p.magnitude.toFixed(2) + '</span>' + status;
      grid.appendChild(row);
    });
  }

  // ---- live target ticker: a marquee of real objects on watch ----------
  var TICKER_BAKED = [
    { name: "SS Cyg", type: "dwarf nova", mag: 8.4 }, { name: "T CrB", type: "recurrent nova", mag: 9.9 },
    { name: "R Leo", type: "Mira variable", mag: 6.8 }, { name: "Z Cam", type: "Z Cam-type CV", mag: 10.0 },
    { name: "RS Oph", type: "symbiotic nova", mag: 11.2 }, { name: "χ Cyg", type: "long-period", mag: 7.1 },
    { name: "U Gem", type: "dwarf nova", mag: 9.3 }, { name: "SS Aur", type: "dwarf nova", mag: 12.0 },
    { name: "AM Her", type: "polar CV", mag: 13.0 }, { name: "RR Lyr", type: "pulsating", mag: 7.6 }
  ];
  function fillTicker(items) {
    var track = $("ticker-track"); if (!track || !items.length) return;
    function row(t) {
      return '<span class="ticker-item"><b>' + t.name + "</b> " + (t.type || "variable") +
        (typeof t.mag === "number" ? ' <span class="tk-mag">mag ' + t.mag.toFixed(1) + "</span>" : "") + "</span>";
    }
    var html = items.map(row).join("");
    track.innerHTML = html + html;       // duplicate so the -50% loop is seamless
  }

  // ---- An illustrative SS Cyg dwarf-nova run so the photometry section is
  //      NEVER blank/slow: quiescence ~12.0 punctuated by fast-rise, slow-decline
  //      outbursts to ~8.4. Live data replaces it the moment the network has any.
  function bakedLightcurve() {
    var pts = [], n = 70, base = 12.0, now = Date.now();
    for (var i = 0; i < n; i++) {
      var cyc = i % 23;                       // ~3 outbursts across 30 days
      var burst = cyc < 2 ? cyc / 2 : (cyc < 11 ? 1 - (cyc - 2) / 9 : 0);
      var m = base - burst * 3.6 + Math.sin(i * 0.9) * 0.05 + (Math.random() * 0.14 - 0.07);
      var ok = Math.random() < 0.86;
      pts.push({
        m: +m.toFixed(2), ok: ok,
        magnitude: +m.toFixed(2), aavso_submitted: ok,
        node_id: "node-" + (1000 + ((i * 7) % 60)),
        received_at: new Date(now - (n - i) * 3.6e6 * 8).toISOString()
      });
    }
    return pts;
  }

  function paintCurves(points, meta) {
    var pts = points.map(function (p) { return { m: p.magnitude, ok: !!p.aavso_submitted }; });
    var ok = points.reduce(function (a, p) { return a + (p.aavso_submitted ? 1 : 0); }, 0);
    var pct = Math.round(ok / points.length * 100);
    if ($("curve-target")) $("curve-target").textContent = (meta.target || "SS Cyg") + " — light curve";
    if ($("curve-meta")) $("curve-meta").textContent = meta.line;
    if ($("curve-badge")) $("curve-badge").textContent = pct + "% AAVSO accepted";
    // Paint immediately (never blank, never "loading"); real browsers get a
    // draw-on animation, throttled contexts still get a full instant curve.
    paintCurve($("lightcurve"), pts, { grid: true }, 1900);
    paintCurve($("minicurve"), pts, { thin: true }, 1500);
    fillConsole(points, meta.target);
  }

  // draw the final curve at once (guaranteed), then layer the rAF draw-on
  // animation when the canvas scrolls into view in a real browser
  function paintCurve(canvas, pts, opts, dur) {
    if (!canvas || !pts || !pts.length) return;
    drawCurve(canvas, pts, 1, opts);                 // instant baseline
    canvas.classList.add("curve-in");
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { animateCurve(canvas, pts, opts, dur); io.disconnect(); } });
    }, { threshold: 0.25 });
    io.observe(canvas);
  }

  // ---- "A night on the network": enhance the baked-in story with live data
  //      (real node id + the actual magnitude swing). Stays graceful: if the
  //      API is down the HTML already reads as a sensible illustrative night.
  function nightStory(lc) {
    if (!lc || !lc.points || !lc.points.length) return;
    // busiest node carries the story
    var counts = {};
    lc.points.forEach(function (p) { counts[p.node_id] = (counts[p.node_id] || 0) + 1; });
    var node = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; })[0];
    var mags = lc.points.map(function (p) { return p.magnitude; });
    var faint = Math.max.apply(null, mags), bright = Math.min.apply(null, mags);
    var nodeEl = $("night-node"), magEl = $("night-mag");
    if (node && nodeEl) nodeEl.textContent = node;
    // only show a real swing if the data genuinely brightened
    if (magEl && faint - bright >= 0.4) {
      magEl.textContent = "mag " + faint.toFixed(1) + " → " + bright.toFixed(1);
    }
  }

  function placeMap(nodes) {
    var box = $("mapbox"); if (!box) return;
    var NS = "http://www.w3.org/2000/svg";

    // constellation lines between nearby online nodes
    var online = nodes.filter(function(n) { return n.online && typeof n.longitude === "number"; });
    if (online.length > 1) {
      var svg = document.createElementNS(NS, "svg");
      svg.setAttribute("style", "position:absolute;inset:0;width:100%;height:100%;pointer-events:none");
      svg.setAttribute("viewBox", "0 0 100 100");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      var cap = Math.min(online.length, 16);
      for (var i = 0; i < cap; i++) {
        var a = online[i];
        var ax = (a.longitude + 180) / 360 * 100, ay = (90 - a.latitude) / 180 * 100;
        for (var j = i + 1; j < cap; j++) {
          var b = online[j];
          var bx = (b.longitude + 180) / 360 * 100, by = (90 - b.latitude) / 180 * 100;
          var dist = Math.sqrt(Math.pow(ax - bx, 2) + Math.pow(ay - by, 2));
          if (dist < 20) {
            var ln = document.createElementNS(NS, "line");
            ln.setAttribute("x1", ax.toFixed(1)); ln.setAttribute("y1", ay.toFixed(1));
            ln.setAttribute("x2", bx.toFixed(1)); ln.setAttribute("y2", by.toFixed(1));
            ln.setAttribute("stroke", "rgba(37,232,160,0.14)");
            ln.setAttribute("stroke-width", "0.3");
            svg.appendChild(ln);
          }
        }
      }
      box.appendChild(svg);
    }

    nodes.forEach(function (n, i) {
      if (typeof n.longitude !== "number") return;
      var x = (n.longitude + 180) / 360 * 100;
      var y = (90 - n.latitude) / 180 * 100;
      var d = document.createElement("div");
      d.className = "ndot"; d.style.left = x + "%"; d.style.top = y + "%";
      d.style.animationDelay = (i * 0.12) + "s";
      if (!n.online) { d.style.background = "#5a5e6b"; d.style.opacity = "0.7"; }
      d.title = (n.city || "node") + (n.country ? ", " + n.country : "");
      box.appendChild(d);
    });
  }

  /* =========================================================
     6b. CINEMATIC INTERACTIONS — cursor spotlight, nav scroll
         state, scroll-progress hairline, magnetic CTAs.
         All no-ops under reduced-motion / touch.
     ========================================================= */
  function cinematics() {
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var coarse = window.matchMedia && window.matchMedia("(hover: none)").matches;

    // ---- nav scroll state + scroll-progress hairline ----
    var nav = document.querySelector(".nav");
    var prog = $("scroll-progress");
    var ticking = false;
    function onScroll() {
      if (ticking) return; ticking = true;
      requestAnimationFrame(function () {
        var y = window.scrollY || 0;
        if (nav) nav.classList.toggle("scrolled", y > 24);
        if (prog) {
          var h = document.documentElement.scrollHeight - window.innerHeight;
          prog.style.setProperty("--sp", h > 0 ? Math.min(1, y / h).toFixed(4) : 0);
        }
        ticking = false;
      });
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();

    if (reduce || coarse) return;

    // ---- cursor spotlight: lerp toward the pointer ----
    var glow = $("cursor-glow");
    if (glow) {
      var tx = -9999, ty = -9999, gx = tx, gy = ty, active = false;
      window.addEventListener("mousemove", function (e) {
        tx = e.clientX; ty = e.clientY;
        if (!active) { active = true; gx = tx; gy = ty; glow.classList.add("on"); }
      }, { passive: true });
      window.addEventListener("mouseleave", function () { glow.classList.remove("on"); active = false; });
      (function trail() {
        gx += (tx - gx) * 0.12; gy += (ty - gy) * 0.12;
        glow.style.setProperty("--mx", gx.toFixed(1) + "px");
        glow.style.setProperty("--my", gy.toFixed(1) + "px");
        requestAnimationFrame(trail);
      })();
    }

    // ---- magnetic CTAs: the primary buttons lean toward the cursor ----
    Array.prototype.forEach.call(document.querySelectorAll(".hero-cta .btn, .builder-cta .btn-cream"), function (btn) {
      btn.addEventListener("mousemove", function (e) {
        var r = btn.getBoundingClientRect();
        var mx = (e.clientX - r.left - r.width / 2) / r.width;
        var my = (e.clientY - r.top - r.height / 2) / r.height;
        btn.style.transform = "translate(" + (mx * 6).toFixed(1) + "px," + (my * 6 - 2).toFixed(1) + "px)";
      });
      btn.addEventListener("mouseleave", function () { btn.style.transform = ""; });
    });
  }

  function boot() {
    builder(); reveals(); mobileNav(); heroParallax(); decorateSections(); cinematics();

    // start the hero animation immediately; re-seed with real targets once loaded
    var sky = skyfield(DEFAULT_TARGETS);
    fillTicker(TICKER_BAKED);     // marquee shows real objects at once, never empty

    // ---- network status ----
    getJSON("/api/v1/network/status").then(function (s) {
      if (!s) {
        // offline fallback: keep page sensible
        $("badge-text").textContent = "nodes observing right now";
        $("stat-targets").textContent = "—"; $("stat-subs").textContent = "—";
        $("stat-nodes").textContent = "—"; $("stat-countries").textContent = "—";
        $("gauge-num").textContent = "—";
        return;
      }
      var countries = {}; (s.nodes || []).forEach(function (n) { if (n.country) countries[n.country] = 1; });
      var nCountries = Object.keys(countries).length;
      function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

      $("badge-text").textContent = plural(s.nodes_online, "telescope") + " hunting right now";
      countUp($("stat-targets"), s.active_targets, 1400);
      countUp($("stat-subs"), s.aavso_submitted, 1600);
      countUp($("stat-nodes"), s.nodes_online, 1200);
      countUp($("stat-countries"), nCountries, 1200);
      $("network-heading").textContent = plural(s.nodes_total, "node") + ". " + plural(nCountries, "country").replace("countrys", "countries") + ". One sky.";

      // best node drives the hero gauge
      var best = (s.nodes || []).slice().sort(function (a, b) {
        return (b.reliability_score || 0) - (a.reliability_score || 0);
      })[0];
      if (best) {
        var rel = best.reliability_score || 0.5, mult = 0.5 + 0.5 * rel;
        drawGauge(rel);
        $("gauge-node").textContent = best.node_id + " · reliability";
        $("gauge-num").textContent = rel.toFixed(2);
        $("gauge-num").style.color = rel >= 0.85 ? "#54D98C" : rel >= 0.65 ? "#25E8A0" : "#E8A93A";
        $("gauge-lbl").textContent = (rel >= 0.85 ? "proven node" : "active node") + " · ×" + mult.toFixed(2);
      }
      placeMap(s.nodes || []);
    });

    // ---- light curve: paint instantly with an illustrative run, then
    //      upgrade to live photometry the moment the network has enough.
    var baked = bakedLightcurve();
    paintCurves(baked, {
      target: "SS Cyg",
      line: "RA 21ʰ42ᵐ42.8ˢ · Dec +43°35′10″ · CV filter · illustrative run"
    });

    function tryLiveCurve(name) {
      return getJSON("/api/v1/lightcurves/" + encodeURIComponent(name) + "?days=30").then(function (lc) {
        if (!lc || !lc.points || lc.points.length < 8) return false;   // keep the baked run if data is sparse
        var nodes = {};
        lc.points.forEach(function (p) { nodes[p.node_id] = 1; });
        paintCurves(lc.points, {
          target: name,
          line: Object.keys(nodes).length + " nodes · " + lc.points.length + " live points · last 30 days"
        });
        nightStory(lc);
        return true;
      });
    }
    // prefer the flagship; otherwise pick whichever target actually has the most measurements
    tryLiveCurve("SS Cyg").then(function (hit) {
      if (hit) return;
      getJSON("/api/v1/targets").then(function (t) {
        if (!t || !t.targets) return;
        var best = t.targets.slice().sort(function (a, b) {
          return (b.n_measurements || 0) - (a.n_measurements || 0);
        })[0];
        if (best && (best.n_measurements || 0) >= 8) tryLiveCurve(best.name);
      });
    });

    // ---- targets → label the hero reticles + feed the ticker with real objects ----
    getJSON("/api/v1/targets").then(function (t) {
      if (!t || !t.targets || !t.targets.length) return;
      var withMag = t.targets.filter(function (x) { return typeof x.mag === "number"; });
      var real = withMag.slice(0, 6).map(function (x) { return { name: x.name, mag: x.mag }; });
      if (real.length && sky) { sky.setTargets(real); }   // re-seed with real targets
      if (withMag.length >= 6) {
        fillTicker(withMag.slice(0, 14).map(function (x) {
          return { name: x.name, type: (x.target_type || "variable"), mag: x.mag };
        }));
      }
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
