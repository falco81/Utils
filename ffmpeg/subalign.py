#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subalign.py - Content-aware subtitle re-timing.

PROBLEM
    You have a subtitle file (.srt) whose TEXT is correct but whose TIMING is
    wrong, and a REFERENCE subtitle whose timing is correct (often a different
    translation of the same dialogue). Goal: copy the reference's timing onto
    the target, sentence by sentence, WITHOUT ever changing the target's text.

METHOD (why it is built this way)
    1. Similarity. Two different translations rarely share exact words, but they
       share character shapes, names and numbers. We score every (target,
       reference) cue pair with a character-3gram cosine plus a bonus for shared
       distinctive tokens (long words / digits). Pure stdlib, language-agnostic.

    2. Anchors, not greedy matching. Short generic lines ("Yes.", "What?")
       appear identically all over the reference, so matching them by text is a
       trap. We trust ONLY confident, distinctive, locally-unique matches as
       "anchors". Everything else is positioned by interpolation, never by its
       own weak text match. This is the single most important idea here.

    3. Two passes. A coarse GLOBAL pass finds a sparse set of very distinctive
       anchors with no time assumption (so it copes with gross / piecewise
       desync, even minutes). It yields a rough time-warp. A fine pass then
       searches near that warp to add more anchors and tighten everything.

    4. Warp + local snap. Anchors define a monotone piecewise-linear time map.
       Non-anchor cues are mapped through it, then optionally snapped to a
       genuinely matching reference cue but only within a tiny window (no
       teleporting). Runs of target cues that map to one reference cue are
       subdivided proportionally. A final pass removes overlaps and enforces
       monotonic, minimum-length cues.

    5. Guarantees. The target text and cue order are asserted to be byte-for-
       byte identical to the input. Original encoding (BOM) and CRLF line
       endings are preserved.

USAGE
    Single pair:
        python3 subalign.py fix wrong.srt reference.srt -o fixed.srt --report

    Whole folder (auto-pair *.orig with the matching *.srt):
        python3 subalign.py batch ./subs --out ./fixed --ref-suffix .orig --report

    Two folders matched by SxxExx episode code:
        python3 subalign.py batch ./targets --ref-dir ./refs --out ./fixed

No third-party packages required (Python 3.8+).
"""

import sys, os, re, argparse, bisect, unicodedata, glob
from collections import Counter

# --------------------------------------------------------------------------- #
#  SRT parsing / writing (preserves text, encoding and line endings)
# --------------------------------------------------------------------------- #

TIME_RE = re.compile(
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*'
    r'(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})')


def _read_text(path):
    """Read a file, remember whether it had a UTF-8 BOM."""
    data = open(path, 'rb').read()
    bom = data.startswith(b'\xef\xbb\xbf')
    return data.decode('utf-8-sig'), bom


def parse_srt(path):
    """Return (cues, meta). Each cue: dict(start, end, text). meta keeps BOM."""
    raw, bom = _read_text(path)
    cues = []
    for block in re.split(r'\r?\n\r?\n', raw.strip('\ufeff').strip()):
        lines = block.split('\n')
        ti = next((k for k, l in enumerate(lines) if '-->' in l), None)
        if ti is None:
            continue
        m = TIME_RE.search(lines[ti])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0]*3600000 + g[1]*60000 + g[2]*1000 + g[3]
        end   = g[4]*3600000 + g[5]*60000 + g[6]*1000 + g[7]
        text = '\n'.join(x.rstrip('\r') for x in lines[ti+1:])
        cues.append({'start': start, 'end': end, 'text': text})
    return cues, {'bom': bom}


def fmt_time(ms):
    if ms < 0:
        ms = 0
    h, ms = divmod(ms, 3600000)
    mi, ms = divmod(ms, 60000)
    s, mm = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, mi, s, mm)


def write_srt(path, cues, times, meta):
    """Write cues with new `times` ([(start,end),...]); text untouched, CRLF."""
    out = []
    for idx, (c, (st, en)) in enumerate(zip(cues, times), 1):
        body = c['text'].replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        out.append("%d\r\n%s --> %s\r\n%s\r\n" % (idx, fmt_time(st), fmt_time(en), body))
    blob = "\r\n".join(out) + "\r\n"
    data = blob.encode('utf-8')
    if meta.get('bom'):
        data = b'\xef\xbb\xbf' + data
    open(path, 'wb').write(data)


# --------------------------------------------------------------------------- #
#  Text similarity (stdlib only, deaccented, language-agnostic)
# --------------------------------------------------------------------------- #

def _deaccent(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(c))


def normalize(text):
    s = _deaccent(text).lower()
    s = re.sub(r'[^0-9a-z\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _grams(s, n=3):
    s = '\u2581' + s.replace(' ', '\u2581') + '\u2581'
    return Counter(s[i:i+n] for i in range(len(s) - n + 1))


def _distinctive(norm_text):
    return set(w for w in norm_text.split() if len(w) >= 4) | \
           set(re.findall(r'\d+', norm_text))


def prepare(cues):
    """Attach normalized form, n-gram vector, norm, and token set to each cue."""
    for c in cues:
        n = normalize(c['text'])
        g = _grams(n)
        c['_n'] = n
        c['_g'] = g
        c['_gn'] = (sum(v*v for v in g.values()) ** 0.5) or 1.0
        c['_tk'] = _distinctive(n)
        c['_music'] = ('\u266a' in c['text'] or '\u266b' in c['text']
                       or '\u2669' in c['text'])
    return cues


def sim(a, b):
    """Cosine over char-3grams (0..1) + small bonus for shared distinctive tokens."""
    g1, g2 = a['_g'], b['_g']
    if len(g1) > len(g2):
        g1, g2 = g2, g1
    dot = sum(c * g2[k] for k, c in g1.items() if k in g2)
    cos = dot / (a['_gn'] * b['_gn'])
    bonus = min(0.25, 0.08 * len(a['_tk'] & b['_tk']))
    return min(1.0, cos + bonus)


def _combined(texts):
    """A pseudo-cue representing several texts concatenated (for 1<->2 matches)."""
    n = ' '.join(normalize(t) for t in texts)
    g = _grams(n)
    return {'_n': n, '_g': g, '_gn': (sum(v*v for v in g.values()) ** 0.5) or 1.0,
            '_tk': _distinctive(n)}


# --------------------------------------------------------------------------- #
#  Anchor finding
# --------------------------------------------------------------------------- #

def _lis_monotone(pairs):
    """Keep the longest strictly-increasing-in-j subsequence of (i,j) anchors."""
    if not pairs:
        return []
    js = [p[1] for p in pairs]
    tails, tails_idx, parent = [], [], [-1] * len(pairs)
    for k, jv in enumerate(js):
        pos = bisect.bisect_left(tails, jv)
        if pos == len(tails):
            tails.append(jv); tails_idx.append(k)
        else:
            tails[pos] = jv; tails_idx[pos] = k
        parent[k] = tails_idx[pos-1] if pos > 0 else -1
    seq, k = [], (tails_idx[-1] if tails_idx else -1)
    while k != -1:
        seq.append(k); k = parent[k]
    seq.reverse()
    return [pairs[k] for k in seq]


def find_anchors(S, O, band_ms, min_len, min_sim, margin, prior=None):
    """
    Confident (target_i -> ref_j) matches.
      band_ms : search radius in time. If `prior` (a warp fn) is given, the
                centre is prior(S_i.start); otherwise the whole reference is
                scanned (coarse global pass).
      min_len : ignore short generic target lines.
      min_sim : minimum best similarity to accept.
      margin  : best must beat the 2nd-best in-band candidate by this much
                (local uniqueness -> rejects repeated generic lines).
    """
    m = len(O)
    ostart = [o['start'] for o in O]
    raw = []
    for i, c in enumerate(S):
        if len(c['_n']) < min_len:
            continue
        if prior is not None:
            centre = prior(c['start'])
            lo = bisect.bisect_left(ostart, centre - band_ms)
            hi = bisect.bisect_right(ostart, centre + band_ms)
            rng = range(lo, hi)
        else:
            rng = range(m)
        best = (-1.0, -1)
        second = -1.0
        for j in rng:
            sc = sim(c, O[j])
            if sc > best[0]:
                second = best[0]; best = (sc, j)
            elif sc > second:
                second = sc
        if best[1] >= 0 and best[0] >= min_sim and (best[0] - max(second, 0.0)) >= margin:
            raw.append((i, best[1], best[0]))
    return _lis_monotone(raw)


def build_warp(S, O, anchors, identity_fallback=True):
    """Monotone piecewise-linear map target-time -> reference-time."""
    pts = sorted({(S[i]['start'], O[j]['start']) for i, j, _ in anchors})
    if not pts:
        return (lambda t: t) if identity_fallback else None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]

    def warp(t):
        if t <= xs[0]:
            return t + (ys[0] - xs[0])          # constant shift before 1st anchor
        if t >= xs[-1]:
            return t + (ys[-1] - xs[-1])         # constant shift after last anchor
        k = bisect.bisect_right(xs, t) - 1
        x0, x1, y0, y1 = xs[k], xs[k+1], ys[k], ys[k+1]
        if x1 == x0:
            return y0
        return y0 + (t - x0) / (x1 - x0) * (y1 - y0)
    return warp


# --------------------------------------------------------------------------- #
#  Re-timing pipeline
# --------------------------------------------------------------------------- #

def retime(S, O, cfg):
    n, m = len(S), len(O)
    ostart = [o['start'] for o in O]

    # --- pass 1: coarse global anchors -> rough warp -------------------------
    coarse = find_anchors(S, O, band_ms=10**9,
                          min_len=cfg['coarse_len'], min_sim=cfg['coarse_sim'],
                          margin=cfg['coarse_margin'], prior=None)
    warp0 = build_warp(S, O, coarse)

    # --- pass 2: fine anchors near the coarse prediction ---------------------
    anchors = find_anchors(S, O, band_ms=cfg['band'],
                          min_len=cfg['min_len'], min_sim=cfg['min_sim'],
                          margin=cfg['margin'], prior=warp0)
    if len(anchors) < len(coarse):       # fall back if fine pass found less
        anchors = coarse
    warp = build_warp(S, O, anchors)
    amap = {i: j for i, j, _ in anchors}

    # --- place every cue -----------------------------------------------------
    out = [None] * n
    snapj = [None] * n
    used_prev = -1
    for i in range(n):
        if i in amap:                                   # confident anchor
            j = amap[i]
            out[i] = [O[j]['start'], O[j]['end']]; snapj[i] = j; used_prev = j
            continue
        tw, twe = int(round(warp(S[i]['start']))), int(round(warp(S[i]['end'])))
        if not S[i]['_music']:                          # local snap (never teleports)
            lo = bisect.bisect_left(ostart, tw - cfg['snap_win'])
            hi = bisect.bisect_right(ostart, tw + cfg['snap_win'])
            best = (-1.0, -1)
            for j in range(lo, hi):
                sc = sim(S[i], O[j])
                if sc > best[0]:
                    best = (sc, j)
            if best[1] >= 0 and best[0] >= cfg['snap_sim'] and best[1] != used_prev:
                j = best[1]
                out[i] = [O[j]['start'], O[j]['end']]; snapj[i] = j; used_prev = j
                continue
        out[i] = [tw, twe]; snapj[i] = None

    # --- subdivide runs mapped to the SAME reference cue ---------------------
    i = 0
    while i < n:
        j = snapj[i]
        if j is None:
            i += 1; continue
        k = i
        while k + 1 < n and snapj[k+1] == j:
            k += 1
        if k > i:
            s, e = O[j]['start'], O[j]['end']
            tot = sum(max(1, len(S[t]['text'])) for t in range(i, k+1))
            acc = 0
            for t in range(i, k+1):
                w = max(1, len(S[t]['text']))
                st = s + int((e - s) * acc / tot); acc += w
                out[t] = [st, s + int((e - s) * acc / tot)]
        i = k + 1

    # --- enforce monotonic starts, min duration, no overlap ------------------
    MIN = cfg['min_dur']
    for i in range(n):
        if i > 0 and out[i][0] < out[i-1][0]:
            out[i][0] = out[i-1][0]
        if out[i][1] < out[i][0] + MIN:
            out[i][1] = out[i][0] + MIN
    for i in range(n - 1):
        if out[i][1] > out[i+1][0]:
            if out[i+1][0] > out[i][0] + MIN:
                out[i][1] = out[i+1][0]
            else:
                mid = (out[i][0] + out[i+1][1]) // 2
                out[i][1] = max(out[i][0] + MIN, min(out[i][1], mid))
                if out[i+1][0] < out[i][1]:
                    out[i+1][0] = out[i][1]
                if out[i+1][1] < out[i+1][0] + MIN:
                    out[i+1][1] = out[i+1][0] + MIN
    return [tuple(x) for x in out], anchors


DEFAULTS = dict(
    coarse_len=20, coarse_sim=0.55, coarse_margin=0.15,   # global pass
    band=45000, min_len=12, min_sim=0.50, margin=0.10,    # fine pass
    snap_win=3000, snap_sim=0.30,                         # local snap
    min_dur=350,                                          # cue hygiene
)


# --------------------------------------------------------------------------- #
#  Validation / metrics
# --------------------------------------------------------------------------- #

def _intervals(items):
    return sorted((s, e) for s, e in items)


def iou(times, O):
    """Intersection-over-union of the two speech timelines (0..1, higher=better)."""
    def union_len(iv):
        iv = _intervals(iv); tot = 0; cs = ce = None
        for s, e in iv:
            if cs is None:
                cs, ce = s, e
            elif s <= ce:
                ce = max(ce, e)
            else:
                tot += ce - cs; cs, ce = s, e
        if cs is not None:
            tot += ce - cs
        return tot
    A = _intervals(times)
    B = _intervals((o['start'], o['end']) for o in O)
    # intersection via sweep
    inter = 0; j = 0
    for s, e in A:
        while j < len(B) and B[j][1] < s:
            j += 1
        k = j
        while k < len(B) and B[k][0] < e:
            inter += max(0, min(e, B[k][1]) - max(s, B[k][0])); k += 1
    ua = union_len(A); ub = union_len([(o['start'], o['end']) for o in O])
    uni = ua + ub - inter
    return inter / uni if uni else 0.0


def report(S, O, times, anchors, old_times=None):
    print("    anchors used      : %d / %d cues" % (len(anchors), len(S)))
    print("    IoU vs reference  : %.4f" % iou(times, O))
    bad = sum(1 for s, e in times if e <= s)
    ov = sum(1 for i in range(1, len(times)) if times[i][0] < times[i-1][1])
    nm = sum(1 for i in range(1, len(times)) if times[i][0] < times[i-1][0])
    print("    sanity            : bad_dur=%d  overlaps=%d  non_monotonic=%d" % (bad, ov, nm))
    if old_times:
        d = sorted(abs(a[0] - b[0]) / 1000 for a, b in zip(old_times, times))
        med = d[len(d)//2]; mean = sum(d)/len(d)
        print("    shift vs input    : median=%.2fs mean=%.2fs max=%.1fs (>0.5s: %d)"
              % (med, mean, max(d), sum(x > 0.5 for x in d)))


# --------------------------------------------------------------------------- #
#  Top-level operations
# --------------------------------------------------------------------------- #

def fix_pair(target_path, ref_path, out_path, cfg, do_report=False):
    S_raw, meta = parse_srt(target_path)
    O_raw, _ = parse_srt(ref_path)
    if not S_raw:
        raise SystemExit("No cues parsed from target: %s" % target_path)
    if not O_raw:
        raise SystemExit("No cues parsed from reference: %s" % ref_path)
    S = prepare([dict(c) for c in S_raw])
    O = prepare([dict(c) for c in O_raw])
    times, anchors = retime(S, O, cfg)
    # text-integrity guarantee
    assert [c['text'] for c in S_raw] == [c['text'] for c in S], "text changed!"
    write_srt(out_path, S_raw, times, meta)
    if do_report:
        print("  %s -> %s" % (os.path.basename(target_path), os.path.basename(out_path)))
        report(S, O, times, anchors,
               old_times=[(c['start'], c['end']) for c in S_raw])
    return out_path


_EP_RE = re.compile(r'[sS](\d{1,2})[\W_]*[eE](\d{1,2})')


def _ep_code(name):
    m = _EP_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def batch(target_dir, out_dir, cfg, ref_suffix=None, ref_dir=None, do_report=False):
    os.makedirs(out_dir, exist_ok=True)
    pairs = []
    if ref_dir:                                  # match two folders by SxxExx
        refs = {}
        for p in glob.glob(os.path.join(ref_dir, '*')):
            ec = _ep_code(os.path.basename(p))
            if ec:
                refs[ec] = p
        for p in glob.glob(os.path.join(target_dir, '*.srt')):
            ec = _ep_code(os.path.basename(p))
            if ec and ec in refs:
                pairs.append((p, refs[ec]))
    else:                                        # ref = target name + suffix
        suffix = ref_suffix or '.orig'
        for ref in glob.glob(os.path.join(target_dir, '*' + suffix)):
            target = ref[:-len(suffix)]
            if not target.endswith('.srt'):
                # also accept e.g. name.srt.orig -> name.srt
                cand = ref[:-len(suffix)]
                target = cand if cand.endswith('.srt') else cand + '.srt'
            if os.path.exists(target):
                pairs.append((target, ref))
    if not pairs:
        raise SystemExit("No (target, reference) pairs found.")
    print("Found %d pair(s).\n" % len(pairs))
    for target, ref in sorted(pairs):
        out = os.path.join(out_dir, os.path.basename(target))
        fix_pair(target, ref, out, cfg, do_report)
        if do_report:
            print()
    print("Done. Output in: %s" % out_dir)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def _add_tuning(ap):
    ap.add_argument('--band', type=float, help='fine-pass search radius (seconds, default 45)')
    ap.add_argument('--snap-win', type=float, help='local snap window (seconds, default 3)')
    ap.add_argument('--min-sim', type=float, help='fine anchor min similarity (default 0.50)')


def _cfg_from_args(a):
    cfg = dict(DEFAULTS)
    if getattr(a, 'band', None) is not None:
        cfg['band'] = int(a.band * 1000)
    if getattr(a, 'snap_win', None) is not None:
        cfg['snap_win'] = int(a.snap_win * 1000)
    if getattr(a, 'min_sim', None) is not None:
        cfg['min_sim'] = a.min_sim
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Content-aware subtitle re-timing: copy a reference's timing "
                    "onto a correctly-translated but mistimed subtitle, per sentence, "
                    "without changing any text.")
    sub = ap.add_subparsers(dest='cmd', required=True)

    f = sub.add_parser('fix', help='re-time a single subtitle against a reference')
    f.add_argument('target', help='subtitle with correct text but wrong timing (.srt)')
    f.add_argument('reference', help='subtitle with correct timing (.srt / .orig)')
    f.add_argument('-o', '--out', help='output path (default: <target>.fixed.srt)')
    f.add_argument('--report', action='store_true', help='print quality metrics')
    _add_tuning(f)

    b = sub.add_parser('batch', help='re-time a whole folder of pairs')
    b.add_argument('dir', help='folder containing the mistimed .srt files')
    b.add_argument('--out', default='./fixed', help='output folder (default ./fixed)')
    b.add_argument('--ref-suffix', default='.orig',
                   help='reference files are <name><suffix> (default .orig)')
    b.add_argument('--ref-dir', help='instead: take references from this folder, '
                                     'matched by SxxExx code')
    b.add_argument('--report', action='store_true', help='print quality metrics')
    _add_tuning(b)

    a = ap.parse_args(argv)
    cfg = _cfg_from_args(a)

    if a.cmd == 'fix':
        out = a.out or (re.sub(r'\.srt$', '', a.target) + '.fixed.srt')
        fix_pair(a.target, a.reference, out, cfg, do_report=a.report)
        print("Wrote: %s" % out)
    elif a.cmd == 'batch':
        batch(a.dir, a.out, cfg, ref_suffix=a.ref_suffix,
              ref_dir=a.ref_dir, do_report=a.report)


if __name__ == '__main__':
    main()
