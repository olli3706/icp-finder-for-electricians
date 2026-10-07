"""Render the ranked prospect list as a self-contained scorecard page."""

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BAND_CLASS = {"High": "high", "Medium": "medium", "Low": "low"}

# Client-side re-scoring. Each business's share of every signal is fixed by the
# collected evidence (points / default weight); the panel's weights decide what
# each share is worth. Applied only while the six weights sum to exactly 100.
SCRIPT = """
<script>
(function () {
  var DATA = JSON.parse(document.getElementById('score-data').textContent);
  DATA.forEach(function (b) {
    b.signals.forEach(function (s) { s.pct = s.w ? s.p / s.w : 0; });
  });
  var inputs = Array.prototype.slice.call(document.querySelectorAll('.weights input'));
  if (!inputs.length) return;
  var totalRow = document.getElementById('wtotal');
  var totalN = document.getElementById('wtotal-n');
  var DEFAULTS = {};
  inputs.forEach(function (i) { DEFAULTS[i.dataset.k] = parseInt(i.value, 10) || 0; });

  function fmt(x) {
    var r = Math.round(x * 10) / 10;
    return r % 1 === 0 ? String(Math.round(r)) : r.toFixed(1);
  }

  function apply(ws) {
    var scores = {};
    DATA.forEach(function (b) {
      var el = document.querySelector('[data-slug="' + b.slug + '"]');
      if (!el) return;
      var total = 0;
      b.signals.forEach(function (s) {
        var w = ws[s.k] || 0;
        var pts = s.pct * w;
        total += pts;
        var sig = el.querySelector('[data-sig="' + s.k + '"] .sig-pts');
        if (sig) sig.innerHTML = fmt(pts) + '<span class="of">/' + w + '</span>';
      });
      var score = Math.round(total);
      scores[b.slug] = score;
      var band = score >= 70 ? 'High' : score >= 50 ? 'Medium' : 'Low';
      el.querySelector('.score').textContent = score;
      var chip = el.querySelector('.chip');
      chip.className = 'chip ' + band.toLowerCase();
      chip.textContent = band;
      el.className = 'row ' + band.toLowerCase();
    });
    ['list-ranked', 'list-unverified'].forEach(function (id) {
      var box = document.getElementById(id);
      if (!box) return;
      var cards = Array.prototype.slice.call(box.children);
      cards.sort(function (a, b) {
        return (scores[b.dataset.slug] || 0) - (scores[a.dataset.slug] || 0);
      });
      cards.forEach(function (c, i) {
        box.appendChild(c);
        if (id === 'list-ranked') c.querySelector('.rank').textContent = i + 1;
      });
    });
  }

  function update() {
    var ws = {}, sum = 0;
    inputs.forEach(function (i) {
      var v = parseInt(i.value, 10);
      if (isNaN(v) || v < 0) v = 0;
      ws[i.dataset.k] = v;
      sum += v;
    });
    totalN.textContent = sum;
    if (sum === 100) {
      totalRow.classList.remove('bad');
      apply(ws);
    } else {
      totalRow.classList.add('bad');
    }
  }

  inputs.forEach(function (i) { i.addEventListener('input', update); });
  document.getElementById('wreset').addEventListener('click', function () {
    inputs.forEach(function (i) { i.value = DEFAULTS[i.dataset.k]; });
    update();
  });
})();
</script>
"""


def esc(value):
    return html.escape(str(value), quote=True)


def meter(signal):
    pct = 0 if not signal["weight"] else round(signal["points"] / signal["weight"] * 100)
    conf = signal.get("confidence")
    flag = ('<span class="uncertain" title="Service list captured in summary form; '
            'absence of a category means not recorded, not not-offered">under-measured</span>'
            if conf == "low" else "")
    return f"""
        <div class="sig" data-sig="{esc(signal['signal'])}">
          <div class="sig-head">
            <span class="sig-name">{esc(signal['signal'])}</span>
            <span class="sig-pts">{signal['points']}<span class="of">/{signal['weight']}</span></span>
          </div>
          <div class="track"><div class="fill" style="width:{pct}%"></div></div>
          <p class="sig-ev">{esc(signal['evidence'])} {flag}
            <span class="src">{esc(signal['source'])}</span></p>
        </div>"""


def card(record, rank=None):
    band = BAND_CLASS.get(record["band"], "low")
    rank_cell = f'<span class="rank">{rank}</span>' if rank else '<span class="rank dash">&mdash;</span>'
    links = [f'<a href="{esc(record["checkatrade_url"])}" target="_blank" rel="noopener">Checkatrade profile</a>']
    if record.get("companies_house_url"):
        label = ("Companies House officer search"
                 if "search/officers" in record["companies_house_url"]
                 else "Companies House record")
        links.append(f'<a href="{esc(record["companies_house_url"])}" target="_blank" rel="noopener">{label}</a>')
    method = record.get("ch_match_method", "")
    method_note = (f'<p class="method-note">Matched by: {esc(method.replace("_", " "))}</p>'
                   if method and method not in ("exact_name",) else "")

    gates = "".join(
        f"""<li class="gate {g['state']}"><span class="gate-state">{g['state'].replace('_', ' ')}</span>
            <span class="gate-name">{esc(name.replace('_', ' '))}</span>
            <span class="gate-ev">{esc(g['evidence'])}</span></li>"""
        for name, g in record["gates"].items())

    return f"""
      <details class="row {band}" data-slug="{esc(record['slug'])}">
        <summary>
          {rank_cell}
          <span class="biz">
            <span class="biz-name">{esc(record['name'])}</span>
            <span class="biz-meta">{esc(record['owner'])} &middot; {esc(record['base_town'])}, {esc(record['county'])}
              &middot; {esc(record['structure'].replace('_', ' '))}</span>
          </span>
          <span class="chip {band}">{esc(record['band'])}</span>
          <span class="score">{record['score']}</span>
          <span class="conf conf-{esc(record['confidence'])}">{esc(record['confidence'])} confidence</span>
        </summary>
        <div class="detail">
          <div class="signals">{''.join(meter(s) for s in record['signals'])}</div>
          <div class="aside">
            <h4>Gates</h4>
            <ul class="gates">{gates}</ul>
            <h4>Sources</h4>
            <p class="links">{' &middot; '.join(links)}</p>
            {method_note}
          </div>
        </div>
      </details>"""


def area_label():
    """Human label for the collected area, from data/collection_meta.json."""
    meta_path = ROOT / "data" / "collection_meta.json"
    towns = []
    if meta_path.exists():
        try:
            towns = json.loads(meta_path.read_text(encoding="utf-8")).get("towns", [])
        except (json.JSONDecodeError, OSError):
            pass
    if not towns:
        return "Reading"
    if len(towns) == 1:
        return towns[0]
    return ", ".join(towns[:-1]) + " and " + towns[-1]


def build_html(data):
    f = data["funnel"]
    area = area_label()
    ranked = "".join(card(r, i) for i, r in enumerate(data["ranked"], 1))
    unverified = "".join(card(r) for r in data["needs_verification"])

    # Weight editor: signal labels + default weights, and per-business earned
    # points so the page can re-score client-side. The share earned of each
    # signal is fixed by the data; the weights decide what that share is worth.
    scored = data["ranked"] + data["needs_verification"]
    weight_defaults = ([(s["signal"], s["weight"]) for s in scored[0]["signals"]]
                       if scored else
                       [("Job throughput", 30), ("Business recency", 20),
                        ("Quoting intensity", 20), ("Revenue band", 15),
                        ("Service breadth", 10), ("Credibility", 5)])
    weight_rows = "".join(
        f'<div class="wrow"><label for="w{i}">{esc(label)}</label>'
        f'<input id="w{i}" data-k="{esc(label)}" type="number" min="0" max="100" '
        f'step="1" value="{weight}" inputmode="numeric"></div>'
        for i, (label, weight) in enumerate(weight_defaults))
    payload = json.dumps([
        {"slug": r["slug"],
         "signals": [{"k": s["signal"], "w": s["weight"], "p": s["points"]}
                     for s in r["signals"]]}
        for r in scored]).replace("</", "<\\/")
    excluded = "".join(
        f"""<li><span class="ex-name">{esc(r['name'])}</span>
             <span class="ex-why">{esc(r['gates']['owner_operator']['evidence']
                if r['gates']['owner_operator']['state'] == 'fail'
                else r['gates']['actively_trading']['evidence'])}</span>
             <a href="{esc(r['checkatrade_url'])}" target="_blank" rel="noopener">source</a></li>"""
        for r in data["excluded"])

    steps = [
        ("Collected", f["collected"], "step-all"),
        ("Not owner-operated", f["failed_owner_operator"], "step-out"),
        ("Not trading", f["failed_active"], "step-out"),
        ("Unverified", f["needs_verification"], "step-hold"),
        ("Qualified", f["qualified"], "step-in"),
    ]
    funnel = "".join(
        f"""<div class="step {cls}">
              <span class="step-n">{n}</span>
              <span class="step-l">{label}</span>
              <div class="step-bar"><div style="width:{round(n / f['collected'] * 100)}%"></div></div>
            </div>""" for label, n, cls in steps)

    return f"""<title>Hardwire ICP Scorecard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {{
  --ground:#EDEFF1; --surface:#FFFFFF; --sunk:#F5F7F8;
  --ink:#14181C; --muted:#59636E; --faint:#8A939D; --hair:#D8DDE2;
  --accent:#B4551F; --accent-soft:#F0E2D9;
  --high:#0F7A52; --high-soft:#DFEFE8;
  --medium:#B0700D; --medium-soft:#F6EBD8;
  --low:#6E7884; --low-soft:#E7EAED;
  --bad:#B3261E;
  --display:'Archivo','Helvetica Neue',Arial,sans-serif;
  --body:'IBM Plex Sans','Helvetica Neue',Arial,sans-serif;
  --mono:'IBM Plex Mono','SF Mono',Consolas,monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#121518; --surface:#1A1E22; --sunk:#20252A;
    --ink:#E4E8EC; --muted:#98A2AD; --faint:#79838E; --hair:#2A3138;
    --accent:#E08A56; --accent-soft:#3A2A20;
    --high:#3FB287; --high-soft:#16302A;
    --medium:#D9A63F; --medium-soft:#332918;
    --low:#7E8894; --low-soft:#242A30;
    --bad:#E5695E;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#121518; --surface:#1A1E22; --sunk:#20252A;
  --ink:#E4E8EC; --muted:#98A2AD; --faint:#79838E; --hair:#2A3138;
  --accent:#E08A56; --accent-soft:#3A2A20;
  --high:#3FB287; --high-soft:#16302A;
  --medium:#D9A63F; --medium-soft:#332918;
  --low:#7E8894; --low-soft:#242A30;
  --bad:#E5695E;
}}
* {{ box-sizing:border-box; }}
body {{ background:var(--ground); color:var(--ink); font-family:var(--body);
  line-height:1.55; margin:0; padding:0 20px 72px; }}
.wrap {{ max-width:1080px; margin:0 auto; }}
h1,h2,h3,h4 {{ font-family:var(--display); text-wrap:balance; margin:0; }}
a {{ color:var(--accent); }}
a:focus-visible, summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}

header {{ padding:56px 0 32px; border-bottom:2px solid var(--ink); }}
.eyebrow {{ font-family:var(--mono); font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--accent); margin:0 0 14px; }}
h1 {{ font-size:clamp(30px,5vw,46px); font-weight:700; letter-spacing:-.02em; line-height:1.05; }}
.thesis {{ max-width:62ch; color:var(--muted); font-size:16px; margin:16px 0 0; }}

.funnel {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1px; background:var(--hair); border:1px solid var(--hair); margin:28px 0 0; }}
.step {{ background:var(--surface); padding:16px 14px 14px; }}
.step-n {{ font-family:var(--mono); font-size:28px; font-weight:600; display:block;
  line-height:1; font-variant-numeric:tabular-nums; }}
.step-l {{ font-size:12px; color:var(--muted); display:block; margin:6px 0 10px; }}
.step-bar {{ height:3px; background:var(--hair); }}
.step-bar div {{ height:100%; background:var(--faint); }}
.step-in .step-n {{ color:var(--high); }} .step-in .step-bar div {{ background:var(--high); }}
.step-out .step-n {{ color:var(--faint); }}
.step-hold .step-n {{ color:var(--medium); }} .step-hold .step-bar div {{ background:var(--medium); }}
.step-all .step-bar div {{ background:var(--ink); }}

section {{ margin:52px 0 0; }}
.sec-head {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  border-bottom:1px solid var(--hair); padding-bottom:10px; margin-bottom:4px; }}
.sec-head h2 {{ font-size:20px; font-weight:600; }}
.sec-head p {{ margin:0; color:var(--muted); font-size:13px; max-width:58ch; }}

.row {{ background:var(--surface); border:1px solid var(--hair); border-top:none; }}
.row:first-of-type {{ border-top:1px solid var(--hair); }}
.row summary {{ display:grid; grid-template-columns:34px 1fr auto auto auto;
  gap:14px; align-items:center; padding:13px 16px; cursor:pointer; list-style:none; }}
.row summary::-webkit-details-marker {{ display:none; }}
.row:hover {{ background:var(--sunk); }}
.rank {{ font-family:var(--mono); font-size:13px; color:var(--faint);
  font-variant-numeric:tabular-nums; }}
.biz {{ min-width:0; }}
.biz-name {{ display:block; font-weight:600; font-size:15px; }}
.biz-meta {{ display:block; font-size:12px; color:var(--muted); }}
.chip {{ font-family:var(--mono); font-size:10.5px; letter-spacing:.08em;
  text-transform:uppercase; padding:3px 8px; border-radius:2px; font-weight:600; }}
.chip.high {{ background:var(--high-soft); color:var(--high); }}
.chip.medium {{ background:var(--medium-soft); color:var(--medium); }}
.chip.low {{ background:var(--low-soft); color:var(--low); }}
.score {{ font-family:var(--mono); font-size:22px; font-weight:600; min-width:44px;
  text-align:right; font-variant-numeric:tabular-nums; }}
.conf {{ font-size:11px; color:var(--muted); min-width:118px; text-align:right; }}
.conf-low {{ color:var(--medium); }}

.detail {{ display:grid; grid-template-columns:1.6fr 1fr; gap:28px;
  padding:6px 16px 20px; border-top:1px dashed var(--hair); }}
.signals {{ display:flex; flex-direction:column; gap:13px; padding-top:14px; }}
.sig-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
.sig-name {{ font-size:13px; font-weight:500; }}
.sig-pts {{ font-family:var(--mono); font-size:13px; font-variant-numeric:tabular-nums; }}
.of {{ color:var(--faint); }}
.track {{ height:6px; background:var(--sunk); border:1px solid var(--hair); margin:5px 0 4px; }}
.fill {{ height:100%; background:var(--accent); }}
.sig-ev {{ margin:0; font-size:12px; color:var(--muted); }}
.src {{ font-family:var(--mono); font-size:10px; letter-spacing:.05em;
  text-transform:uppercase; color:var(--faint); margin-left:6px; }}
.uncertain {{ font-family:var(--mono); font-size:10px; color:var(--medium);
  border-bottom:1px dotted var(--medium); cursor:help; }}
.aside {{ padding-top:14px; }}
.aside h4 {{ font-size:11px; font-family:var(--mono); letter-spacing:.1em;
  text-transform:uppercase; color:var(--faint); margin:0 0 8px; }}
.aside h4 + * {{ margin-bottom:18px; }}
.gates {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:9px; }}
.gate-state {{ font-family:var(--mono); font-size:9.5px; letter-spacing:.08em;
  text-transform:uppercase; padding:2px 6px; border-radius:2px; }}
.gate.pass .gate-state {{ background:var(--high-soft); color:var(--high); }}
.gate.fail .gate-state {{ background:var(--low-soft); color:var(--low); }}
.gate.unverified .gate-state {{ background:var(--medium-soft); color:var(--medium); }}
.gate-name {{ font-size:12px; font-weight:500; margin-left:6px; }}
.gate-ev {{ display:block; font-size:11.5px; color:var(--muted); margin-top:3px; }}
.links {{ font-size:12px; margin:0; }}
.method-note {{ font-size:11px; color:var(--medium); font-family:var(--mono); margin:8px 0 0; }}

.excluded {{ list-style:none; margin:0; padding:0; border:1px solid var(--hair); }}
.excluded li {{ display:grid; grid-template-columns:220px 1fr auto; gap:14px;
  padding:11px 16px; background:var(--surface); border-bottom:1px solid var(--hair);
  align-items:baseline; font-size:13px; }}
.excluded li:last-child {{ border-bottom:none; }}
.ex-name {{ font-weight:600; }}
.ex-why {{ color:var(--muted); font-size:12px; }}
.excluded a {{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase;
  letter-spacing:.06em; }}

.method {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
  gap:24px; margin-top:20px; }}
.panel {{ background:var(--surface); border:1px solid var(--hair); padding:20px; }}
.panel h3 {{ font-size:15px; font-weight:600; margin-bottom:10px; }}
.panel p, .panel li {{ font-size:13px; color:var(--muted); }}
.panel ul {{ margin:0; padding-left:18px; display:flex; flex-direction:column; gap:7px; }}
.panel strong {{ color:var(--ink); font-weight:600; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; padding:6px 0; border-bottom:1px solid var(--hair); }}
th {{ font-family:var(--mono); font-size:10px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--faint); font-weight:500; }}
td.w {{ font-family:var(--mono); text-align:right; font-variant-numeric:tabular-nums; }}
.scroll {{ overflow-x:auto; }}
footer {{ margin-top:44px; padding-top:18px; border-top:1px solid var(--hair);
  font-size:12px; color:var(--faint); }}

.layout {{ display:grid; grid-template-columns:225px minmax(0,1fr); gap:28px;
  align-items:start; }}
.layout main {{ min-width:0; }}
.weights {{ position:sticky; top:20px; margin-top:52px; background:var(--surface);
  border:1px solid var(--hair); padding:16px; }}
.weights h3 {{ font-size:11px; font-family:var(--mono); letter-spacing:.12em;
  text-transform:uppercase; color:var(--faint); font-weight:500; }}
.whint {{ font-size:11.5px; color:var(--muted); margin:7px 0 12px; }}
.wrow {{ display:flex; justify-content:space-between; align-items:center; gap:8px;
  margin:7px 0; }}
.wrow label {{ font-size:12.5px; }}
.wrow input {{ width:58px; font-family:var(--mono); font-size:13px; text-align:right;
  padding:4px 6px; background:var(--sunk); border:1px solid var(--hair);
  color:var(--ink); border-radius:0; }}
.wrow input:focus-visible {{ outline:2px solid var(--accent); outline-offset:1px; }}
.wtotal {{ border-top:1px solid var(--hair); margin-top:10px; padding-top:9px;
  display:flex; justify-content:space-between; flex-wrap:wrap;
  font-family:var(--mono); font-size:13px; font-weight:600;
  font-variant-numeric:tabular-nums; }}
.wtotal.bad {{ color:var(--bad); }}
.wtotal.bad::after {{ content:"must equal 100 \2014 showing last valid weights";
  flex-basis:100%; text-align:right; font-weight:400; font-size:10px;
  margin-top:3px; }}
.wreset {{ margin-top:12px; width:100%; font-family:var(--mono); font-size:10.5px;
  letter-spacing:.06em; text-transform:uppercase; padding:7px 0;
  background:var(--sunk); border:1px solid var(--hair); color:var(--muted);
  cursor:pointer; }}
.wreset:hover {{ color:var(--ink); border-color:var(--faint); }}

@media (max-width:920px) {{
  .layout {{ display:block; }}
  .weights {{ position:static; margin-top:40px; }}
}}
@media (max-width:760px) {{
  .row summary {{ grid-template-columns:26px 1fr auto; row-gap:6px; }}
  .score {{ grid-column:3; }} .chip {{ grid-column:3; justify-self:end; }}
  .conf {{ grid-column:2/4; text-align:left; }}
  .detail {{ grid-template-columns:1fr; }}
  .excluded li {{ grid-template-columns:1fr; }}
}}
</style>

<div class="wrap">
<header>
  <h1>Which {esc(area)}-area electricians should we call first?</h1>
  <p class="thesis">{f['collected']} domestic electricians serving the {esc(area)} area, collected
  from Checkatrade and cross-checked against Companies House. Hardwire's wedge is estimating and
  quoting, so the ranking rewards businesses that quote constantly, are run by the person who would
  use the product, and have nothing already doing the job. Every score below opens to show the
  evidence and the source it came from.</p>
  <div class="funnel">{funnel}</div>
</header>

<div class="layout">
<aside class="weights">
  <h3>Weights</h3>
  <p class="whint">Set what each signal is worth. Scores and ranks update live
  when the total is exactly 100.</p>
  {weight_rows}
  <div class="wtotal" id="wtotal"><span>Total</span><span id="wtotal-n">100</span></div>
  <button id="wreset" class="wreset" type="button">Reset to defaults</button>
</aside>
<main>

<section>
  <div class="sec-head">
    <h2>Ranked prospects</h2>
    <p>{f['qualified']} businesses cleared both gates. Click any row for the score breakdown.</p>
  </div>
  <div id="list-ranked">{ranked}</div>
</section>

<section>
  <div class="sec-head">
    <h2>Needs verification</h2>
    <p>Scored, but the Companies House match is unresolved &mdash; so we could not confirm
    owner-operator scale. Not a rejection: a work queue.</p>
  </div>
  <div id="list-unverified">{unverified}</div>
</section>

<section>
  <div class="sec-head">
    <h2>Excluded</h2>
    <p>Failed a gate outright, with the evidence that failed them.</p>
  </div>
  <ul class="excluded">{excluded}</ul>
</section>

</main>
</div>

<footer>
  Data collected 30&ndash;31 August 2026 from public Checkatrade profiles and the Companies House
  public register. Collection was limited to paths permitted by Checkatrade's robots.txt. Scores
  are a judgment model, not a measurement &mdash; every input is linked to its source so the
  reasoning can be checked and the weights argued with.
</footer>
</div>
<script id="score-data" type="application/json">{payload}</script>
""" + SCRIPT


if __name__ == "__main__":
    data = json.load(open(ROOT / "out" / "ranked.json", encoding="utf-8"))
    target = ROOT / "out" / "scorecard.html"
    target.write_text(build_html(data), encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
