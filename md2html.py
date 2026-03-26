#!/usr/bin/env python3
"""
md2html.py — Convert Markdown to retro terminal-style HTML
Generates: fixed sidebar TOC + main content + statusbar + CRT scanlines

Usage (everything auto-detected):
    python md2html.py input.md

Override anything:
    python md2html.py input.md -o output.html
    python md2html.py input.md --title "Title"
    python md2html.py input.md --subtitle "Subtitle shown in topbar"
    python md2html.py input.md --label "SYSTEM // PROJECT"
    python md2html.py input.md --status "CPU · RAM · OS"
    python md2html.py input.md --no-scanlines
    python md2html.py input.md --verbose

Or embed in the .md file (takes priority over auto-detection):
    <!-- md2html
    title: Custom Title
    subtitle: Custom subtitle
    label: SYSTEM // NAME
    status: CPU · RAM
    -->
"""

import sys, os, re, argparse

try:
    import markdown
    from markdown.extensions.toc import TocExtension
except ImportError:
    print("ERROR: pip install markdown")
    sys.exit(1)

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap');

:root {
  --bg:         #0a0c0a;
  --bg2:        #0f130f;
  --bg3:        #141a14;
  --border:     #1e3a1e;
  --green:      #39ff14;
  --green-dim:  #1a7a08;
  --green-mid:  #22c905;
  --amber:      #ffb300;
  --amber-dim:  #7a5500;
  --text:       #b8e8b0;
  --text-dim:   #6a9460;
  --heading:    #39ff14;
  --code-bg:    #060d06;
  --table-head: #0f2010;
  --table-alt:  #0c160c;
  --glow:       0 0 8px rgba(57,255,20,0.4);
  --glow-strong:0 0 16px rgba(57,255,20,0.7), 0 0 32px rgba(57,255,20,0.3);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Share Tech Mono', monospace;
  font-size: 14px;
  line-height: 1.7;
  display: flex;
  min-height: 100vh;
  overflow-x: hidden;
}

/* CRT scanlines */
body.scanlines::before {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent, transparent 2px,
    rgba(0,0,0,0.08) 2px, rgba(0,0,0,0.08) 4px
  );
  pointer-events: none;
  z-index: 999;
}

/* ── SIDEBAR ── */
#sidebar {
  width: 280px;
  min-width: 280px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
  flex-shrink: 0;
}
#sidebar::-webkit-scrollbar { width: 4px; }
#sidebar::-webkit-scrollbar-track { background: var(--bg2); }
#sidebar::-webkit-scrollbar-thumb { background: var(--green-dim); }

.sidebar-header {
  padding: 20px 16px 12px;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: var(--bg2);
  z-index: 10;
}
.sidebar-header .sys-label {
  font-family: 'Orbitron', monospace;
  font-size: 9px;
  color: var(--green-dim);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 6px;
}
.sidebar-header h2 {
  font-family: 'Orbitron', monospace;
  font-size: 11px;
  color: var(--green);
  text-shadow: var(--glow);
  letter-spacing: 1px;
  line-height: 1.4;
  font-weight: 700;
  margin: 0;
  border: none;
  padding: 0;
  background: none;
  text-shadow: var(--glow);
}

#toc { padding: 12px 0; }
#toc ul { list-style: none; padding: 0; }
#toc li { padding: 0; }
#toc a {
  display: block;
  padding: 3px 16px;
  color: var(--text-dim);
  text-decoration: none;
  font-size: 11.5px;
  line-height: 1.5;
  border-left: 2px solid transparent;
  transition: all 0.15s;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
#toc a:hover {
  color: var(--green);
  border-left-color: var(--green);
  background: rgba(57,255,20,0.05);
  text-shadow: var(--glow);
}
#toc > ul > li > a {
  color: var(--green-mid);
  font-size: 12px;
  padding-top: 6px;
  padding-bottom: 6px;
}
#toc > ul > li > ul > li > a {
  padding-left: 28px;
  font-size: 11px;
}
#toc > ul > li > ul > li > ul > li > a {
  padding-left: 42px;
  font-size: 10.5px;
  color: #4a7440;
}
#toc a.active {
  color: var(--green) !important;
  border-left-color: var(--green) !important;
  background: rgba(57,255,20,0.08) !important;
  text-shadow: var(--glow) !important;
}

/* ── MAIN CONTENT ── */
#content {
  flex: 1;
  max-width: calc(100vw - 280px);
  padding: 40px 56px 80px;
  overflow-x: auto;
}
#content::-webkit-scrollbar { width: 6px; height: 6px; }
#content::-webkit-scrollbar-track { background: var(--bg); }
#content::-webkit-scrollbar-thumb { background: var(--green-dim); border-radius: 3px; }

/* ── TOPBAR ── */
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 40px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
}
.topbar .prompt {
  font-family: 'Orbitron', monospace;
  font-size: 10px;
  color: var(--green-dim);
  letter-spacing: 4px;
}
.topbar .blink {
  display: inline-block;
  width: 10px;
  height: 16px;
  background: var(--green);
  box-shadow: var(--glow);
  animation: blink 1.2s step-end infinite;
  vertical-align: middle;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
.topbar .topbar-subtitle {
  color: var(--text-dim);
  font-size: 11px;
  letter-spacing: 2px;
}

/* ── TYPOGRAPHY ── */
h1, h2, h3, h4 {
  font-family: 'Orbitron', monospace;
  color: var(--heading);
  line-height: 1.3;
  scroll-margin-top: 20px;
}
h1 {
  font-size: 24px;
  font-weight: 900;
  text-shadow: var(--glow-strong);
  margin-bottom: 6px;
  letter-spacing: 1px;
  border: none;
  padding: 0;
  margin-top: 0;
}
h1::after { display: none; }
h1 + p, h1 + h3 { margin-top: 4px; }
h2 {
  font-size: 16px;
  font-weight: 700;
  margin-top: 48px;
  margin-bottom: 16px;
  padding-bottom: 8px;
  border: none;
  border-bottom: 1px solid var(--border);
  background: none;
  text-shadow: var(--glow);
  letter-spacing: 1px;
  color: var(--heading);
}
h3 {
  font-size: 13px;
  font-weight: 700;
  color: var(--amber);
  margin-top: 28px;
  margin-bottom: 10px;
  text-shadow: 0 0 8px rgba(255,179,0,0.5);
  letter-spacing: 0.5px;
  border: none;
  padding: 0;
}
h3::before { display: none; }
h4 {
  font-size: 12px;
  font-weight: 400;
  color: var(--green-mid);
  margin-top: 20px;
  margin-bottom: 8px;
  text-shadow: none;
}
h4::before { display: none; }
h5 { font-size: 12px; color: var(--text); margin-top: 16px; margin-bottom: 6px; }
h6 { font-size: 11px; color: var(--text-dim); margin-top: 14px; margin-bottom: 6px; }

p { margin-bottom: 12px; }
strong { color: var(--green); font-weight: normal; text-shadow: var(--glow); }
em { color: var(--amber); font-style: normal; }
a { color: var(--green-mid); text-decoration: none; border-bottom: 1px dotted var(--green-dim); }
a:hover { color: var(--green); text-shadow: var(--glow); }

/* ── CODE ── */
code {
  background: var(--code-bg);
  color: var(--green);
  padding: 1px 5px;
  border-radius: 2px;
  font-size: 13px;
  font-family: 'Share Tech Mono', monospace;
  border: 1px solid var(--border);
  text-shadow: var(--glow);
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--green-dim);
  padding: 16px 20px;
  overflow-x: auto;
  margin: 16px 0;
  border-radius: 2px;
  position: relative;
}
pre::before {
  content: '> ';
  color: var(--green-dim);
  font-size: 12px;
}
pre code {
  background: none; border: none; padding: 0;
  color: var(--green);
  font-size: 13px; line-height: 1.6;
  text-shadow: 0 0 4px rgba(57,255,20,0.3);
}

/* ── TABLES ── */
table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }
thead tr { background: var(--table-head); border-bottom: 1px solid var(--green-dim); }
th {
  padding: 8px 12px; text-align: left;
  color: var(--green); font-weight: normal;
  font-family: 'Orbitron', monospace; font-size: 10px;
  letter-spacing: 1px; text-shadow: var(--glow); text-transform: uppercase;
}
td { padding: 7px 12px; border-bottom: 1px solid rgba(30,58,30,0.5); color: var(--text); vertical-align: top; }
tr:nth-child(even) td { background: var(--table-alt); }
tr:hover td { background: rgba(57,255,20,0.04); }
td code { font-size: 12px; }

/* ── BLOCKQUOTE ── */
blockquote {
  border-left: 3px solid var(--amber-dim);
  padding: 10px 16px; margin: 16px 0;
  background: rgba(255,179,0,0.04);
  color: #c8a060; font-size: 13px;
}
blockquote p { margin-bottom: 0; }
blockquote strong { color: var(--amber); text-shadow: 0 0 8px rgba(255,179,0,0.5); }

/* ── LISTS ── */
ul, ol { padding-left: 24px; margin-bottom: 12px; }
li { margin-bottom: 4px; }
li p { margin-bottom: 4px; }
ul li::marker { color: var(--green-dim); content: '> '; }
ol li::marker { color: var(--green-dim); }

/* ── HR ── */
hr { border: none; border-top: 1px solid var(--border); margin: 32px 0; }

/* ── STATUSBAR ── */
.statusbar {
  position: fixed;
  bottom: 0; left: 280px; right: 0;
  height: 24px;
  background: var(--green-dim);
  display: flex; align-items: center;
  padding: 0 16px; gap: 24px;
  z-index: 100;
  font-size: 10px; color: var(--bg);
  font-family: 'Orbitron', monospace; letter-spacing: 1px;
}
.statusbar span { opacity: 0.8; }
.statusbar .right { margin-left: auto; }

/* ── SCROLLBARS ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--green-dim); border-radius: 3px; }

/* ── RESPONSIVE ── */
@media (max-width: 900px) {
  #sidebar { display: none; }
  #content { max-width: 100vw; padding: 20px; }
  .statusbar { left: 0; }
}
@media print {
  #sidebar, .statusbar, body::before { display: none; }
  #content { max-width: 100%; padding: 20px; }
}
"""

# ── THEMES ────────────────────────────────────────────────────────────────────
# Each theme is a CSS :root override block + optional extra rules.
# Default theme (green phosphor) is baked into CSS above — no override needed.
THEMES = {

"green": "",  # default — no override

# ── AMBER — classic amber phosphor monitor ──────────────────────────────────
"amber": """
:root {
  --bg:#0d0900;--bg2:#120d00;--bg3:#181100;--border:#3d2600;
  --green:#ffaa00;--green-dim:#7a4800;--green-mid:#cc8000;
  --amber:#ff6600;--amber-dim:#7a3000;
  --text:#ffe0a0;--text-dim:#997040;--heading:#ffaa00;
  --code-bg:#080500;--table-head:#1a1000;--table-alt:#130d00;
  --glow:0 0 8px rgba(255,170,0,0.5);
  --glow-strong:0 0 16px rgba(255,170,0,0.8),0 0 32px rgba(255,170,0,0.3);
}
.sidebar-header .sys-label{color:#7a4800}
.topbar .prompt{color:#7a4800}
""",

# ── BLUE — cold cyberpunk neon ───────────────────────────────────────────────
"blue": """
:root {
  --bg:#000d1a;--bg2:#001022;--bg3:#00152b;--border:#003366;
  --green:#00aaff;--green-dim:#003d7a;--green-mid:#0088cc;
  --amber:#00ffff;--amber-dim:#006666;
  --text:#a0d8f0;--text-dim:#4a7090;--heading:#00aaff;
  --code-bg:#00060f;--table-head:#001528;--table-alt:#000e1f;
  --glow:0 0 8px rgba(0,170,255,0.5);
  --glow-strong:0 0 16px rgba(0,170,255,0.8),0 0 32px rgba(0,170,255,0.3);
}
.sidebar-header .sys-label{color:#003d7a}
.topbar .prompt{color:#003d7a}
h3{color:#00ffff;text-shadow:0 0 8px rgba(0,255,255,0.5)}
blockquote{border-left-color:#006666;background:rgba(0,255,255,0.04);color:#80b8d0}
blockquote strong{color:#00ffff}
""",

# ── RED — alert / danger terminal ───────────────────────────────────────────
"red": """
:root {
  --bg:#0d0000;--bg2:#120000;--bg3:#180000;--border:#3d0000;
  --green:#ff2200;--green-dim:#7a0000;--green-mid:#cc1100;
  --amber:#ff8800;--amber-dim:#7a3000;
  --text:#ffb8a0;--text-dim:#904040;--heading:#ff2200;
  --code-bg:#080000;--table-head:#1a0000;--table-alt:#130000;
  --glow:0 0 8px rgba(255,34,0,0.5);
  --glow-strong:0 0 16px rgba(255,34,0,0.8),0 0 32px rgba(255,34,0,0.3);
}
.sidebar-header .sys-label{color:#7a0000}
.topbar .prompt{color:#7a0000}
h3{color:#ff8800;text-shadow:0 0 8px rgba(255,136,0,0.5)}
blockquote{border-left-color:#7a3000;background:rgba(255,136,0,0.04);color:#d09060}
blockquote strong{color:#ff8800}
""",

# ── PURPLE — synthwave / vaporwave ──────────────────────────────────────────
"purple": """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Exo+2:wght@300;400;600&display=swap');
:root {
  --bg:#0a0010;--bg2:#0e0018;--bg3:#130020;--border:#2d0060;
  --green:#cc44ff;--green-dim:#5500aa;--green-mid:#aa22ee;
  --amber:#ff44aa;--amber-dim:#880044;
  --text:#e0b8ff;--text-dim:#7040a0;--heading:#cc44ff;
  --code-bg:#060008;--table-head:#12001e;--table-alt:#0d0016;
  --glow:0 0 8px rgba(204,68,255,0.5);
  --glow-strong:0 0 16px rgba(204,68,255,0.8),0 0 32px rgba(204,68,255,0.3);
}
body{font-family:'Exo 2',sans-serif}
pre code{font-family:'Share Tech Mono',monospace}
.sidebar-header .sys-label{color:#5500aa}
.topbar .prompt{color:#5500aa}
h3{color:#ff44aa;text-shadow:0 0 8px rgba(255,68,170,0.5)}
blockquote{border-left-color:#880044;background:rgba(255,68,170,0.04);color:#c080b0}
blockquote strong{color:#ff44aa}
""",

# ── C64 — Commodore 64 blue ──────────────────────────────────────────────────
"c64": """
:root {
  --bg:#3535a0;--bg2:#2a2a8a;--bg3:#2020a8;--border:#4040b8;
  --green:#8080ff;--green-dim:#5050c0;--green-mid:#7070ee;
  --amber:#c0c0ff;--amber-dim:#8080cc;
  --text:#9090f0;--text-dim:#6060c0;--heading:#a0a0ff;
  --code-bg:#2828a0;--table-head:#303090;--table-alt:#2c2c98;
  --glow:0 0 6px rgba(128,128,255,0.4);
  --glow-strong:0 0 12px rgba(128,128,255,0.7),0 0 24px rgba(128,128,255,0.3);
}
body{background:var(--bg)}
#sidebar{background:var(--bg2);border-right:1px solid var(--border)}
.statusbar{background:#6060c0;color:#2020a0}
.sidebar-header .sys-label{color:#5050c0}
.topbar .prompt{color:#5050c0}
h3{color:#c0c0ff;text-shadow:0 0 6px rgba(192,192,255,0.4)}
blockquote{border-left-color:#8080cc;background:rgba(192,192,255,0.06);color:#a0a0e0}
blockquote strong{color:#c0c0ff}
hr{border-top-color:#4040b8}
tbody tr{border-bottom-color:#3535b0}
""",

# ── DOS — classic DOS blue editor (like EDIT.COM / QBASIC) ──────────────────
"dos": """
:root {
  --bg:#0000aa;--bg2:#000080;--bg3:#0000cc;--border:#5555ff;
  --green:#ffffff;--green-dim:#aaaaaa;--green-mid:#cccccc;
  --amber:#ffff55;--amber-dim:#aaaa00;
  --text:#ffffff;--text-dim:#aaaaaa;--heading:#ffffff;
  --code-bg:#000055;--table-head:#000088;--table-alt:#000077;
  --glow:none;--glow-strong:none;
}
body{background:var(--bg);font-family:'Share Tech Mono',monospace}
#sidebar{background:var(--bg2);border-right:2px solid #5555ff}
.statusbar{background:#aaaaaa;color:#000000;font-family:'Share Tech Mono',monospace}
.topbar{border-bottom:1px solid #5555ff}
.topbar .prompt{color:#aaaaaa}
.blink{background:#ffffff;box-shadow:none}
a{color:#ffff55;border-bottom:none}
a:hover{color:#ffffff;text-shadow:none}
h1{text-shadow:none;color:#ffff55}
h2{text-shadow:none;color:#ffffff;border-bottom:1px solid #5555ff;background:none}
h3{color:#ffff55;text-shadow:none}
h4{color:#aaaaaa;text-shadow:none}
strong{color:#ffff55;text-shadow:none}
em{color:#55ffff}
code{background:#000055;border-color:#5555ff;color:#ffffff;text-shadow:none}
pre{border-left-color:#5555ff;background:#000055;box-shadow:none}
pre code{color:#ffffff;text-shadow:none}
pre::before{color:#aaaaaa}
blockquote{border-left-color:#aaaa00;background:rgba(255,255,85,0.05);color:#ddddaa}
blockquote strong{color:#ffff55}
thead tr{background:#000088;border-bottom:1px solid #5555ff}
th{color:#ffffff;text-shadow:none}
td{border-bottom-color:#333399}
tr:nth-child(even) td{background:#000077}
tr:hover td{background:rgba(255,255,255,0.05)}
ul li::marker{color:#aaaaaa}
ol li::marker{color:#aaaaaa}
hr{border-top-color:#5555ff}
::-webkit-scrollbar-thumb{background:#5555ff}
""",

# ── NORD — cool nordic blues and teals ──────────────────────────────────────
"nord": """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Inter:wght@300;400;600&display=swap');
:root {
  --bg:#1e2030;--bg2:#252740;--bg3:#2a2d44;--border:#3d4060;
  --green:#88c0d0;--green-dim:#3d6070;--green-mid:#81a1c1;
  --amber:#ebcb8b;--amber-dim:#806a30;
  --text:#d8dee9;--text-dim:#7a8499;--heading:#88c0d0;
  --code-bg:#1a1c2e;--table-head:#222438;--table-alt:#1e2034;
  --glow:0 0 6px rgba(136,192,208,0.3);
  --glow-strong:0 0 12px rgba(136,192,208,0.5),0 0 24px rgba(136,192,208,0.2);
}
body{font-family:'Inter',sans-serif;font-weight:300}
pre,code,#toc a,.topbar,.statusbar,.sidebar-header h2,.sidebar-header .sys-label{font-family:'Share Tech Mono',monospace}
#sidebar{background:var(--bg2);border-right:1px solid var(--border)}
.statusbar{background:#3d4060;color:#d8dee9}
.sidebar-header .sys-label{color:#3d6070}
.topbar .prompt{color:#3d6070}
h2{color:#81a1c1}
h3{color:#ebcb8b;text-shadow:0 0 6px rgba(235,203,139,0.3)}
h4{color:#a3be8c}
strong{color:#eceff4;font-weight:600;text-shadow:none}
em{color:#ebcb8b}
a{color:#88c0d0;border-bottom:1px dotted #3d6070}
a:hover{color:#eceff4;text-shadow:var(--glow)}
blockquote{border-left-color:#806a30;background:rgba(235,203,139,0.05);color:#c0a870}
blockquote strong{color:#ebcb8b}
""",

# ── PAPER — light mode, clean technical document ────────────────────────────
"paper": """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Source+Serif+4:wght@300;400;600&display=swap');
:root {
  --bg:#f5f0e8;--bg2:#ede8dc;--bg3:#e5e0d4;--border:#ccbfa8;
  --green:#1a6040;--green-dim:#4a8060;--green-mid:#2a7050;
  --amber:#8b4000;--amber-dim:#bb7030;
  --text:#2a2018;--text-dim:#7a6050;--heading:#1a3050;
  --code-bg:#eae5d8;--table-head:#e0d8c8;--table-alt:#ede8dc;
  --glow:none;--glow-strong:none;
}
body{background:var(--bg);color:var(--text);font-family:'Source Serif 4',serif;font-weight:300}
body.scanlines::before{display:none}
pre,code,#toc a,.topbar,.statusbar,.sidebar-header{font-family:'Share Tech Mono',monospace}
#sidebar{background:var(--bg2);border-right:1px solid var(--border)}
.sidebar-header{background:var(--bg2);border-bottom:1px solid var(--border)}
.sidebar-header .sys-label{color:#7a6050}
.sidebar-header h2{color:#1a3050;text-shadow:none}
#toc a{color:#4a5060;border-left-color:transparent}
#toc a:hover{color:#1a3050;background:rgba(26,48,80,0.05);border-left-color:#1a3050;text-shadow:none}
#toc > ul > li > a{color:#1a3050}
#toc a.active{color:#1a6040 !important;border-left-color:#1a6040 !important;background:rgba(26,96,64,0.08) !important;text-shadow:none !important}
.topbar{border-bottom:1px solid var(--border)}
.topbar .prompt{color:#7a6050}
.blink{background:#1a3050;box-shadow:none}
.topbar-subtitle{color:#7a6050}
.statusbar{background:#1a3050;color:#d0c8b8}
h1{color:#1a3050;text-shadow:none;font-family:'Orbitron',monospace}
h2{color:#1a3050;text-shadow:none;border-bottom:1px solid var(--border);background:none}
h3{color:#8b4000;text-shadow:none;font-family:'Orbitron',monospace}
h4{color:#1a6040;text-shadow:none}
strong{color:#1a3050;font-weight:600;text-shadow:none}
em{color:#8b4000;font-style:italic}
a{color:#1a6040;border-bottom:1px solid #4a8060}
a:hover{color:#1a3050;text-shadow:none}
code{background:var(--code-bg);border-color:var(--border);color:#1a6040;text-shadow:none}
pre{background:var(--code-bg);border:1px solid var(--border);border-left:3px solid #4a8060;box-shadow:none}
pre::before{color:#7a6050}
pre code{color:#1a3050;text-shadow:none}
blockquote{border-left-color:#bb7030;background:rgba(139,64,0,0.04);color:#7a5030}
blockquote strong{color:#8b4000;text-shadow:none}
thead tr{background:var(--table-head);border-bottom:1px solid var(--border)}
th{color:#1a3050;text-shadow:none}
td{border-bottom-color:var(--border);color:var(--text)}
tr:nth-child(even) td{background:var(--table-alt)}
tr:hover td{background:rgba(26,48,80,0.04)}
ul li::marker{color:#4a8060;content:'· '}
ol li::marker{color:#4a8060}
hr{border-top-color:var(--border)}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border)}
""",

# ── HACKER — classic dark green hacker movie aesthetic ──────────────────────
"hacker": """
:root {
  --bg:#000000;--bg2:#020802;--bg3:#031003;--border:#0a2a0a;
  --green:#00ff41;--green-dim:#006614;--green-mid:#00cc33;
  --amber:#00ff41;--amber-dim:#006614;
  --text:#00cc33;--text-dim:#006614;--heading:#00ff41;
  --code-bg:#000500;--table-head:#010f01;--table-alt:#000a00;
  --glow:0 0 10px rgba(0,255,65,0.6);
  --glow-strong:0 0 20px rgba(0,255,65,0.9),0 0 40px rgba(0,255,65,0.4),0 0 80px rgba(0,255,65,0.1);
}
body{background:#000}
h3{color:#00cc33;text-shadow:0 0 10px rgba(0,204,51,0.6)}
h4{color:#00ff41}
strong{color:#00ff41}
em{color:#00cc33}
a{color:#00cc33;border-bottom:1px dotted #006614}
a:hover{color:#00ff41}
blockquote{border-left-color:#006614;background:rgba(0,255,65,0.03);color:#00994d}
blockquote strong{color:#00ff41}
.statusbar{background:#006614;color:#000}
tbody tr{border-bottom-color:#0a2a0a}
tr:nth-child(even) td{background:#010f01}
td{border-bottom-color:#0a2a0a}
""",

# ── RETRO — warm sepia/brown vintage computer ────────────────────────────────
"retro": """
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&family=Courier+Prime:wght@400;700&display=swap');
:root {
  --bg:#1a1200;--bg2:#221800;--bg3:#2a1e00;--border:#4a3000;
  --green:#d4a030;--green-dim:#6a4800;--green-mid:#b88020;
  --amber:#ff8c00;--amber-dim:#804000;
  --text:#e8d090;--text-dim:#806040;--heading:#d4a030;
  --code-bg:#0f0c00;--table-head:#1e1600;--table-alt:#181200;
  --glow:0 0 8px rgba(212,160,48,0.4);
  --glow-strong:0 0 16px rgba(212,160,48,0.7),0 0 32px rgba(212,160,48,0.3);
}
body{font-family:'Courier Prime',monospace}
pre,code,#toc a,.topbar,.statusbar,.sidebar-header{font-family:'Share Tech Mono',monospace}
.sidebar-header .sys-label{color:#6a4800}
.topbar .prompt{color:#6a4800}
h3{color:#ff8c00;text-shadow:0 0 8px rgba(255,140,0,0.4)}
blockquote{border-left-color:#804000;background:rgba(255,140,0,0.04);color:#c09050}
blockquote strong{color:#ff8c00}
""",

}

THEME_NAMES = list(THEMES.keys())

def apply_theme(css_base, theme_name):
    """Append theme CSS override to base CSS."""
    override = THEMES.get(theme_name, "")
    if not override:
        return css_base
    # Strip @import from override if already present in base
    override_clean = re.sub(
        r"@import url\(['\"]https://fonts\.googleapis[^)]+\)['\"];?\s*\n?",
        "", override
    )
    return css_base + "\n/* ── THEME: " + theme_name.upper() + " ── */\n" + override_clean

JS = """
const sections = document.querySelectorAll('h2[id], h3[id]');
const tocLinks = document.querySelectorAll('#toc a');
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      tocLinks.forEach(l => l.classList.remove('active'));
      const active = document.querySelector('#toc a[href="#' + entry.target.id + '"]');
      if (active) {
        active.classList.add('active');
        active.scrollIntoView({ block: 'nearest' });
      }
    }
  });
}, { rootMargin: '-10% 0px -80% 0px' });
sections.forEach(s => observer.observe(s));
"""

# ── helpers ───────────────────────────────────────────────────────────────────
def _strip_tags(html):
    return re.sub(r'<[^>]+>', '', html).strip()

def _slug(text):
    s = re.sub(r'<[^>]+>', '', text).strip()
    s = re.sub(r'[^\w\s-]', '', s.lower())
    return re.sub(r'[\s_]+', '-', s).strip('-')

def parse_frontmatter(md_text):
    meta = {}
    m = re.match(r'\s*<!--\s*md2html\s*\n(.*?)-->', md_text, re.DOTALL | re.IGNORECASE)
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                meta[k.strip().lower()] = v.strip()
    return meta

def autodetect(md_text, filename):
    """Auto-detect title, subtitle, label, status from document."""
    lines = md_text.splitlines()
    title = subtitle = label = status = ''

    _SUBTITLE_RX = re.compile(
        r'(v\d|ver\d|\d{3,}|gb|mb|kb|mhz|ghz|awe|dos|win|pci|isa|ide|'
        r'sata|usb|ms-dos|windows|pentium|ryzen|intel|amd|roland|·|—|\+)',
        re.IGNORECASE
    )

    h1_idx = h2_idx = h3_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not title and s.startswith('# ') and not s.startswith('## '):
            title = s[2:].strip(); h1_idx = i; continue
        if h1_idx is not None and h2_idx is None:
            if s.startswith('## '):
                cand = s[3:].strip()
                if i - h1_idx <= 4 and _SUBTITLE_RX.search(cand):
                    subtitle = cand; h2_idx = i
                continue
        if h2_idx is not None and h3_idx is None:
            if s.startswith('### '):
                cand = s[4:].strip()
                if i - h2_idx <= 2:
                    label = cand; h3_idx = i
                break
            elif s.startswith('## ') or s.startswith('**'):
                break

    # Label fallback: bold **Machine:** / **CPU:** lines
    if not label:
        parts = []
        for line in lines[:20]:
            m = re.match(r'\*\*([^*]+)\*\*:?\s*(.+)', line.strip())
            if m:
                key = m.group(1).strip().rstrip(':').lower()
                val = re.sub(r'\*+', '', m.group(2)).strip()
                if key in ('machine', 'cpu', 'system', 'board', 'model'):
                    parts.append(val)
                    if len(parts) >= 2: break
        label = ' · '.join(parts) if parts else ''

    # Status bar: combine key info from bold lines
    if not status:
        parts = []
        for line in lines[:20]:
            m = re.match(r'\*\*([^*]+)\*\*:?\s*(.+)', line.strip())
            if m:
                key = m.group(1).strip().rstrip(':').lower()
                val = re.sub(r'\*+', '', m.group(2)).strip()
                if key in ('machine', 'cpu', 'os', 'ram', 'sound', 'chipset'):
                    parts.append(val)
                    if len(parts) >= 4: break
        status = ' · '.join(parts) if parts else ''

    stem = os.path.splitext(os.path.basename(filename))[0].upper()
    if not title:   title  = stem
    if not label:   label  = stem.replace('_', ' ')
    if not status:  status = stem.replace('_', ' ')

    return title, subtitle, label, status

def inject_ids(html):
    seen = {}
    def repl(m):
        tag, attrs, content = m.group(1), m.group(2), m.group(3)
        if 'id=' in attrs: return m.group(0)
        base = _slug(content)
        n = seen.get(base, 0); seen[base] = n + 1
        slug = base if n == 0 else f'{base}-{n}'
        return f'<{tag}{attrs} id="{slug}">{content}</{tag}>'
    return re.sub(r'<(h[1-6])([^>]*)>(.*?)</\1>', repl, html,
                  flags=re.IGNORECASE | re.DOTALL)

def build_toc_html(body_html):
    """
    Build hierarchical TOC from h1/h2/h3 headings.
    Returns HTML string with nested <ul> structure.
    """
    heading_rx = re.compile(
        r'<(h[123])[^>]*id="([^"]+)"[^>]*>(.*?)</\1>',
        re.IGNORECASE | re.DOTALL
    )
    items = []
    for m in heading_rx.finditer(body_html):
        level = int(m.group(1)[1])
        anchor = m.group(2)
        label  = _strip_tags(m.group(3))
        items.append((level, anchor, label))

    if not items:
        return ''

    html = ['<ul>']
    prev_level = 1
    open_counts = [0]  # stack of open <ul> counts per nesting

    for level, anchor, label in items:
        if level > prev_level:
            html.append('<ul>')
            open_counts.append(0)
        elif level < prev_level:
            for _ in range(prev_level - level):
                html.append('</ul></li>')
                if len(open_counts) > 1:
                    open_counts.pop()
        html.append(f'<li><a href="#{anchor}">{label}</a>')
        prev_level = level

    # close remaining
    for _ in range(len(open_counts)):
        html.append('</li></ul>')

    return '\n'.join(html)

def count_steps_phases(md_text):
    """Count phases and steps for statusbar."""
    phases = len(re.findall(r'^## Phase', md_text, re.MULTILINE | re.IGNORECASE))
    steps  = len(re.findall(r'^### Step', md_text, re.MULTILINE | re.IGNORECASE))
    parts  = []
    if steps  > 0: parts.append(f'{steps} STEPS')
    if phases > 0: parts.append(f'{phases} PHASES')
    return ' // '.join(parts)

def build_html(title, subtitle, label, status, step_info, body, scanlines, theme="green"):
    sc = 'scanlines' if scanlines else ''
    toc_html = build_toc_html(body)
    topbar_sub = subtitle.upper() if subtitle else title.upper()
    right_info = step_info or title[:30].upper()
    status_parts = [s.strip() for s in status.split('·') if s.strip()] if status else [title]
    status_spans = ''.join(f'<span>{p}</span>' for p in status_parts[:4])
    themed_css = apply_theme(CSS, theme)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{themed_css}</style>
</head>
<body class="{sc}">

<nav id="sidebar">
  <div class="sidebar-header">
    <div class="sys-label">{label}</div>
    <h2>{title.upper()}</h2>
  </div>
  <div id="toc">
    {toc_html}
  </div>
</nav>

<main id="content">
  <div class="topbar">
    <span class="prompt">C:\\&gt;</span>
    <span class="blink"></span>
    <span class="topbar-subtitle">{topbar_sub}</span>
  </div>

  {body}

  <div style="height:40px;"></div>
</main>

<div class="statusbar">
  {status_spans}
  <span class="right">{right_info}</span>
</div>

<script>
{JS}
</script>
</body>
</html>"""

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description='MD → retro terminal HTML (sidebar + statusbar layout)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    p.add_argument('input')
    p.add_argument('-o', '--output', default=None)
    p.add_argument('--title',    default=None, metavar='TEXT')
    p.add_argument('--subtitle', default=None, metavar='TEXT',
                   help='Shown in topbar (default: first H2 after H1)')
    p.add_argument('--label',    default=None, metavar='TEXT',
                   help='Sidebar header label (default: H3 or Machine/CPU lines)')
    p.add_argument('--status',   default=None, metavar='TEXT',
                   help='Statusbar text, separate items with · (default: auto)')
    p.add_argument('--no-scanlines', action='store_true')
    p.add_argument('--theme', default='green',
                   choices=THEME_NAMES,
                   metavar='THEME',
                   help=f'Visual theme. Choices: {", ".join(THEME_NAMES)} (default: green)')
    p.add_argument('--list-themes', action='store_true',
                   help='Print available themes and exit')
    p.add_argument('--verbose', '-v', action='store_true')
    args = p.parse_args()

    if args.list_themes:
        print("Available themes:")
        for name in THEME_NAMES:
            marker = " (default)" if name == "green" else ""
            print(f"  {name}{marker}")
        sys.exit(0)

    if not os.path.isfile(args.input):
        print(f"ERROR: not found: {args.input}"); sys.exit(1)

    with open(args.input, 'r', encoding='utf-8') as f:
        md_text = f.read()

    meta = parse_frontmatter(md_text)
    auto_title, auto_sub, auto_label, auto_status = autodetect(md_text, args.input)

    title    = args.title    or meta.get('title')    or auto_title
    subtitle = args.subtitle or meta.get('subtitle') or auto_sub
    label    = args.label    or meta.get('label')    or auto_label
    status   = args.status   or meta.get('status')   or auto_status

    md   = markdown.Markdown(extensions=['tables', 'fenced_code'])
    body = inject_ids(md.convert(md_text))

    step_info = count_steps_phases(md_text)

    html = build_html(title, subtitle, label, status, step_info, body,
                      not args.no_scanlines, theme=args.theme)

    out = args.output or os.path.splitext(args.input)[0] + '.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    kb = os.path.getsize(out) / 1024
    print(f"OK  {out}  ({kb:.1f} KB)")
    if args.verbose:
        print(f"    title:    {title}")
        print(f"    subtitle: {subtitle or '(empty)'}")
        print(f"    label:    {label}")
        print(f"    status:   {status}")
        print(f"    theme:    {args.theme}")
        print(f"    steps:    {step_info or '(none detected)'}")

if __name__ == '__main__':
    main()
