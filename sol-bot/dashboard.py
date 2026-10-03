"""
Live dashboard for the price collector.

Serves a self-contained web page showing what collect_history.py has
gathered: collector liveness, tokens being tracked, and how close each
one is to triggering the bot's breakout rule. Uses only Python's
standard library (no Flask) so it drops in without extra dependencies.

Design note: the hero is collector liveness, not price. A collector
that dies silently — or worse, runs while collecting useless data — is
the failure mode that actually matters here, so the pulse strip at the
top shows one bar per poll and makes gaps immediately visible.

This shows PUBLIC market data only — the same prices anyone can see on
DexScreener. No wallet, key, or trading info touches this page. Still,
it's an open HTTP port with no login, so treat the URL as something you
wouldn't post publicly.

Run with:
    python3 dashboard.py

Serves on http://0.0.0.0:8080 — visit http://<droplet-ip>:8080 once the
firewall allows port 8080.
"""
import csv
import glob
import gzip
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict

from config import config
from chatbot import answer_question

PORT = 8080
ONE_HOUR = 3600
LOAD_WINDOW_SECONDS = ONE_HOUR * 6
PULSE_TICKS = 40


def _open_maybe_gzip(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", newline="")
    return open(path, newline="")


def load_recent_rows(max_age_seconds: int = LOAD_WINDOW_SECONDS) -> list[dict]:
    """
    Loads rows from recent price_history files, filtered to the last
    max_age_seconds so the page stays fast after weeks of collection.
    """
    rows = []
    cutoff = time.time() - max_age_seconds
    paths = sorted(glob.glob("price_history_*.csv")) + sorted(glob.glob("price_history_*.csv.gz"))
    for path in sorted(paths)[-3:]:
        try:
            with _open_maybe_gzip(path) as f:
                for row in csv.DictReader(f):
                    try:
                        ts = float(row["timestamp"])
                        if ts < cutoff:
                            continue
                        rows.append({
                            "timestamp": ts,
                            "pair_address": row["pair_address"],
                            "symbol": row.get("symbol", "?"),
                            "price_usd": float(row["price_usd"]),
                            "volume_24h_usd": float(row["volume_24h_usd"]),
                        })
                    except (KeyError, ValueError):
                        continue
        except OSError:
            continue
    return rows


def _pct_change(series: list[dict], window_seconds: float, now: float) -> float | None:
    """Percent change across a time window, or None without enough samples."""
    recent = [r for r in series if now - r["timestamp"] <= window_seconds]
    if len(recent) < 2:
        return None
    first, last = recent[0]["price_usd"], recent[-1]["price_usd"]
    if first <= 0:
        return None
    return ((last - first) / first) * 100


def compute_summary(rows: list[dict]) -> dict:
    now = time.time()
    lookback_seconds = config.breakout_lookback_minutes * 60

    cfg = {
        "breakout_pct": config.breakout_pct,
        "lookback_minutes": config.breakout_lookback_minutes,
        "min_volume_usd": config.min_volume_usd,
        "confirmation_polls": config.confirmation_polls,
        "dry_run": config.dry_run,
        "mode": config.mode,
    }

    if not rows:
        return {
            "total_snapshots": 0,
            "pairs_tracked": 0,
            "last_update": None,
            "seconds_since_update": None,
            "poll_times": [],
            "movers": [],
            "near_trigger_count": 0,
            "config": cfg,
        }

    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair_address"]].append(r)

    movers = []
    for pair_addr, series in by_pair.items():
        series.sort(key=lambda r: r["timestamp"])
        latest = series[-1]

        change_lookback = _pct_change(series, lookback_seconds, now)
        change_1h = _pct_change(series, ONE_HOUR, now)

        window_rows = [r for r in series if now - r["timestamp"] <= ONE_HOUR]
        prices = [r["price_usd"] for r in window_rows] or [latest["price_usd"]]
        step = max(1, len(prices) // 30)
        sparkline = prices[::step][-30:]

        meets_volume = latest["volume_24h_usd"] >= config.min_volume_usd
        # Percentage points short of firing the breakout rule.
        # Zero or below means it's over the threshold right now.
        gap_to_trigger = (config.breakout_pct - change_lookback
                          if change_lookback is not None else None)

        movers.append({
            "symbol": latest["symbol"],
            "pair_address": pair_addr,
            "price_usd": latest["price_usd"],
            "change_lookback_pct": change_lookback,
            "change_1h_pct": change_1h,
            "volume_24h_usd": latest["volume_24h_usd"],
            "meets_volume": meets_volume,
            "gap_to_trigger_pct": gap_to_trigger,
            "armed": (gap_to_trigger is not None and gap_to_trigger <= 0 and meets_volume),
            "samples": len(series),
            "sparkline": sparkline,
        })

    # Sort by proximity to firing rather than raw price movement: what
    # the bot is about to act on is more useful than what moved most.
    movers.sort(key=lambda m: m["gap_to_trigger_pct"]
                if m["gap_to_trigger_pct"] is not None else 9e9)

    poll_times = sorted({round(r["timestamp"], 1) for r in rows})[-PULSE_TICKS:]
    last_ts = max(r["timestamp"] for r in rows)

    return {
        "total_snapshots": len(rows),
        "pairs_tracked": len(by_pair),
        "last_update": last_ts,
        "seconds_since_update": now - last_ts,
        "poll_times": poll_times,
        "movers": movers[:25],
        "near_trigger_count": sum(1 for m in movers if m["armed"]),
        "config": cfg,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sol-bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --paper:  #FFFFFF;
    --sunk:   #F3F4F7;
    --ink:    #000000;
    --soft:   #71757E;
    --line:   #E4E6EB;
    --signal: #5B3DF5;
    --up:     #05875F;
    --down:   #CE2C31;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper:  #0B0C0F;
      --sunk:   #14161B;
      --ink:    #FFFFFF;
      --soft:   #8B909B;
      --line:   #23262E;
      --signal: #8B72FF;
      --up:     #2FBF87;
      --down:   #F0666B;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--paper);
    color: var(--ink);
    font-family: "Instrument Sans", -apple-system, system-ui, sans-serif;
    font-size: 15px;
    line-height: 1.5;
    padding: 30px 22px 90px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1000px; margin: 0 auto; }

  /* ---- masthead ---- */
  .top { display: flex; justify-content: space-between; align-items: center;
    gap: 16px; flex-wrap: wrap; margin-bottom: 40px; }
  .mark { font-family: "Bricolage Grotesque", sans-serif; font-size: 19px;
    font-weight: 700; letter-spacing: -0.02em; }
  .status { display: inline-flex; align-items: center; gap: 9px;
    font-size: 14px; color: var(--soft); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--up); }
  .dot.stale { background: var(--down); }
  .dot.idle  { background: var(--soft); }

  /* ---- hero ---- */
  .hero { display: grid; grid-template-columns: 1.35fr 1fr; gap: 26px;
    align-items: stretch; margin-bottom: 52px; }
  .headline { display: flex; flex-direction: column; justify-content: center; }
  .figure { font-family: "Bricolage Grotesque", sans-serif;
    font-size: clamp(72px, 13vw, 128px); font-weight: 600; line-height: 0.86;
    letter-spacing: -0.045em; margin: 0 0 14px; }
  .figure.hot { color: var(--signal); }
  .caption { font-size: 17px; color: var(--soft); max-width: 30ch; margin: 0; }

  .aside { background: var(--sunk); border-radius: 12px; padding: 20px 22px;
    display: flex; flex-direction: column; justify-content: space-between; gap: 20px; }
  .beat { display: flex; align-items: flex-end; gap: 2px; height: 46px; }
  .beat i { flex: 1 1 0; max-width: 4px; min-width: 2px; border-radius: 2px;
    background: var(--ink); opacity: 0.34; }
  .beat i.miss { background: var(--down); opacity: 0.5; }
  .beat-note { font-size: 13px; color: var(--soft); margin-top: 9px; }
  .pair { display: flex; gap: 26px; }
  .pair div { flex: 1; }
  .pair b { display: block; font-family: "Bricolage Grotesque", sans-serif;
    font-size: 25px; font-weight: 600; letter-spacing: -0.02em; }
  .pair span { font-size: 13px; color: var(--soft); }

  /* ---- list ---- */
  .lede { font-family: "Bricolage Grotesque", sans-serif; font-size: 21px;
    font-weight: 600; letter-spacing: -0.02em; margin: 0 0 6px; }
  .rule { font-size: 14px; color: var(--soft); margin: 0 0 26px; max-width: 66ch; }

  .row { display: grid; grid-template-columns: 132px 1fr 92px 104px;
    gap: 20px; align-items: center; padding: 17px 0; border-top: 1px solid var(--line); }
  .row:last-child { border-bottom: 1px solid var(--line); }
  .name { font-weight: 600; font-size: 16px; letter-spacing: -0.01em; }
  .meta { font-size: 12px; color: var(--soft); font-weight: 400; }

  .track { position: relative; height: 26px; display: flex; align-items: center; }
  .bar { position: relative; height: 6px; width: 100%; background: var(--sunk);
    border-radius: 99px; overflow: hidden; }
  .fill { position: absolute; inset: 0 auto 0 0; background: var(--ink);
    border-radius: 99px; width: 0; }
  .fill.hot { background: var(--signal); }
  .fill.neg { background: var(--down); }
  /* Below the volume floor the bot will not buy, however far the price
     ran — so the bar must not read as "ready". */
  .fill.inert { background: repeating-linear-gradient(135deg,
    var(--line) 0 5px, transparent 5px 10px); }
  .track em { position: absolute; right: 0; top: -3px; font-style: normal;
    font-size: 12px; color: var(--soft); background: var(--paper); padding-left: 8px; }
  .track em.hot { color: var(--signal); font-weight: 600; }
  .anim .fill { transition: width .7s cubic-bezier(.22,.61,.36,1); }

  .val { text-align: right; font-size: 15px; font-variant-numeric: tabular-nums; }
  .val small { display: block; font-size: 12px; color: var(--soft); }
  .up { color: var(--up); } .down { color: var(--down); } .soft { color: var(--soft); }

  .blank { padding: 44px 0; color: var(--soft); border-top: 1px solid var(--line); }

  foot, footer { display: block; margin-top: 44px; padding-top: 18px;
    border-top: 1px solid var(--line); }
  footer p { margin: 0; font-size: 13px; color: var(--soft); max-width: 72ch; }
  footer b { color: var(--ink); font-weight: 500; }

  /* ---- assistant ---- */
  #ask { position: fixed; right: 20px; bottom: 20px; z-index: 30;
    background: var(--ink); color: var(--paper); border: 0; cursor: pointer;
    font-family: inherit; font-size: 14px; font-weight: 600;
    padding: 12px 20px; border-radius: 99px; }
  #panel { position: fixed; right: 20px; bottom: 74px; z-index: 30;
    width: 340px; max-width: calc(100vw - 40px); height: 410px;
    background: var(--paper); border: 1px solid var(--line); border-radius: 14px;
    display: none; flex-direction: column; overflow: hidden;
    box-shadow: 0 18px 44px rgba(0,0,0,.14); }
  #panel.open { display: flex; }
  #panel header { padding: 13px 16px; font-size: 13px; color: var(--soft);
    border-bottom: 1px solid var(--line); }
  #log { flex: 1; overflow-y: auto; padding: 14px; display: flex;
    flex-direction: column; gap: 10px; }
  .msg { padding: 9px 13px; border-radius: 13px; font-size: 14px; max-width: 86%; }
  .msg.you { align-self: flex-end; background: var(--ink); color: var(--paper); }
  .msg.bot { align-self: flex-start; background: var(--sunk); }
  #entry { display: flex; border-top: 1px solid var(--line); }
  #q { flex: 1; border: 0; background: transparent; color: var(--ink);
    font-family: inherit; font-size: 14px; padding: 13px 15px; }
  #send { border: 0; background: transparent; color: var(--signal);
    font-family: inherit; font-weight: 600; font-size: 14px; padding: 0 16px; cursor: pointer; }

  button:focus-visible, input:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { .anim .fill { transition: none; } }
  @media (max-width: 720px) {
    .hero { grid-template-columns: 1fr; gap: 22px; }
    .row { grid-template-columns: 1fr 92px; gap: 10px 14px; }
    .track { grid-column: 1 / -1; order: 3; }
    .val.vol { display: none; }
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <div class="mark">sol&middot;bot</div>
    <div class="status"><span class="dot idle" id="dot"></span><span id="stat">connecting</span></div>
  </div>

  <div class="hero">
    <div class="headline">
      <p class="figure" id="fig">&ndash;</p>
      <p class="caption" id="cap">Reading collected prices&hellip;</p>
    </div>
    <div class="aside">
      <div>
        <div class="beat" id="beat"></div>
        <div class="beat-note" id="beatnote">Collector heartbeat</div>
      </div>
      <div class="pair">
        <div><b id="n-tokens">&ndash;</b><span>tokens watched</span></div>
        <div><b id="n-samples">&ndash;</b><span>price samples</span></div>
      </div>
    </div>
  </div>

  <h2 class="lede">Closest to a buy</h2>
  <p class="rule" id="rule">Ranked by distance from the bot's breakout rule.</p>
  <div id="list"><div class="blank">Loading&hellip;</div></div>

  <footer><p id="foot"></p></footer>
</div>

<button id="ask" aria-expanded="false" aria-controls="panel">Ask</button>
<div id="panel" role="dialog" aria-label="Assistant">
  <header>Answers from collected data. No AI credits used.</header>
  <div id="log"></div>
  <div id="entry">
    <input id="q" type="text" placeholder="Is it running?" aria-label="Ask a question">
    <button id="send">Send</button>
  </div>
</div>

<script>
  const money = p => p >= 1 ? '$' + p.toFixed(3)
    : p >= 0.0001 ? '$' + p.toFixed(6) : '$' + p.toExponential(2);
  const vol = v => v >= 1e6 ? '$' + (v/1e6).toFixed(1) + 'M'
    : v >= 1e3 ? '$' + (v/1e3).toFixed(0) + 'K' : '$' + v.toFixed(0);
  const pct = v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
  const esc = s => String(s).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  let painted = false;

  function beat(times) {
    const el = document.getElementById('beat');
    const note = document.getElementById('beatnote');
    if (!times || times.length < 2) {
      el.innerHTML = '';
      note.textContent = 'Waiting for the first polls';
      return;
    }
    const gaps = [];
    for (let i = 1; i < times.length; i++) gaps.push(times[i] - times[i-1]);
    const med = [...gaps].sort((a,b) => a-b)[Math.floor(gaps.length/2)] || 30;
    el.innerHTML = times.map((t, i) => {
      const d = i === 0 ? med : times[i] - times[i-1];
      const missed = d > med * 1.8;
      return '<i class="' + (missed ? 'miss' : '') + '" style="height:' +
             (missed ? 22 : 100) + '%"></i>';
    }).join('');
    const mins = Math.round((times[times.length-1] - times[0]) / 60);
    note.textContent = times.length + ' polls over ' + mins + ' min';
  }

  async function refresh() {
    let d;
    try { d = await (await fetch('/api/data')).json(); }
    catch (e) { document.getElementById('stat').textContent = 'dashboard unreachable'; return; }

    const dot = document.getElementById('dot'), stat = document.getElementById('stat');
    if (d.last_update === null) { dot.className = 'dot idle'; stat.textContent = 'no data yet'; }
    else if (d.seconds_since_update > 120) {
      dot.className = 'dot stale';
      stat.textContent = 'stalled ' + Math.round(d.seconds_since_update/60) + ' min';
    } else {
      dot.className = 'dot';
      stat.textContent = 'live, ' + Math.round(d.seconds_since_update) + 's ago';
    }

    beat(d.poll_times);

    const c = d.config, n = d.near_trigger_count;
    const fig = document.getElementById('fig');
    fig.textContent = n;
    fig.className = 'figure' + (n > 0 ? ' hot' : '');
    document.getElementById('cap').textContent = n === 0
      ? 'No token currently meets the breakout rule.'
      : n === 1 ? 'One token meets the breakout rule right now.'
      : n + ' tokens meet the breakout rule right now.';

    document.getElementById('n-tokens').textContent = d.pairs_tracked;
    document.getElementById('n-samples').textContent = d.total_snapshots.toLocaleString();
    document.getElementById('rule').textContent =
      'A buy needs a ' + c.breakout_pct + '% gain over ' + c.lookback_minutes +
      ' minutes, held for ' + c.confirmation_polls + ' polls, on at least ' +
      vol(c.min_volume_usd) + ' of 24h volume.';
    document.getElementById('foot').innerHTML =
      'Mode <b>' + esc(c.mode) + '</b>, dry run <b>' + (c.dry_run ? 'on' : 'off') +
      '</b>. Prices from DexScreener. This page reads collected data only and cannot trade.';

    const list = document.getElementById('list');
    if (!d.movers.length) {
      list.innerHTML = '<div class="blank">No token has enough samples yet. ' +
        'The collector needs a few polls before it can measure change.</div>';
      return;
    }

    list.innerHTML = d.movers.map(m => {
      const w = m.change_lookback_pct, h = m.change_1h_pct, g = m.gap_to_trigger_pct;
      // Bar fills toward the breakout threshold; full means the rule is met.
      let frac = 0, cls = '', label;
      if (w !== null && c.breakout_pct > 0) frac = Math.max(0, Math.min(1, w / c.breakout_pct));
      if (g === null) label = '<em>not enough data</em>';
      else if (g <= 0 && m.meets_volume) { cls = 'hot'; label = '<em class="hot">meets the rule</em>'; }
      else if (g <= 0) { cls = 'inert'; label = '<em>volume too low</em>'; }
      else label = '<em>' + g.toFixed(1) + '% to go</em>';
      if (w !== null && w < 0) cls = 'neg';
      if (!m.meets_volume) cls = 'inert';
      const dir = w === null || Math.abs(w) < 0.05 ? 'soft' : w > 0 ? 'up' : 'down';
      return '<div class="row">' +
        '<div><div class="name">' + esc(m.symbol) + '</div>' +
          '<div class="meta">' + m.samples + ' samples</div></div>' +
        '<div class="track"><div class="bar"><div class="fill ' + cls +
          '" data-w="' + (frac * 100).toFixed(1) + '"></div></div>' + label + '</div>' +
        '<div class="val ' + dir + '">' +
          (w === null ? '&ndash;' : pct(w)) +
          '<small>' + (h === null ? '&nbsp;' : pct(h) + ' 1h') + '</small></div>' +
        '<div class="val vol">' + money(m.price_usd) +
          '<small>' + vol(m.volume_24h_usd) + '</small></div>' +
      '</div>';
    }).join('');

    // Fill the bars after paint so the first render animates once.
    requestAnimationFrame(() => {
      if (!painted) { list.classList.add('anim'); painted = true; }
      list.querySelectorAll('.fill').forEach(el => { el.style.width = el.dataset.w + '%'; });
    });
  }

  const panel = document.getElementById('panel'), askBtn = document.getElementById('ask');
  askBtn.onclick = () => {
    const open = panel.classList.toggle('open');
    askBtn.setAttribute('aria-expanded', open);
    if (open) {
      document.getElementById('q').focus();
      if (!document.getElementById('log').children.length)
        say("Ask about collector status, tokens watched, what's closest to firing, " +
            "or terms like breakout, rug check, honeypot.", 'bot');
    }
  };
  function say(text, who) {
    const el = document.createElement('div');
    el.className = 'msg ' + who; el.textContent = text;
    const log = document.getElementById('log');
    log.appendChild(el); log.scrollTop = log.scrollHeight;
  }
  async function send() {
    const i = document.getElementById('q'), question = i.value.trim();
    if (!question) return;
    say(question, 'you'); i.value = '';
    try {
      const r = await fetch('/api/chat', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question }) });
      say((await r.json()).answer, 'bot');
    } catch (e) { say("Couldn't reach the assistant. The dashboard may have restarted.", 'bot'); }
  }
  document.getElementById('send').onclick = send;
  document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

  refresh();
  setInterval(refresh, 15000);
</script>
</body>
</html>
"""



class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep stdout quiet; systemd journal still captures errors

    def _send(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", ""):
            self._send(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            try:
                summary = compute_summary(load_recent_rows())
                self._send(json.dumps(summary).encode("utf-8"), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}).encode("utf-8"),
                           "application/json", status=500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/chat":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
                question = str(data.get("question", ""))[:500]  # public endpoint, cap length
                answer = answer_question(question)
            except (json.JSONDecodeError, ValueError):
                answer = "Couldn't read that question — try again?"
            except Exception as e:
                answer = f"Something went wrong answering that: {e}"
            self._send(json.dumps({"answer": answer}).encode("utf-8"), "application/json")
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[dashboard] serving on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[dashboard] stopped")
