"""Small server-rendered SVG charts (no JS library, CSP-safe). Colors via Tailwind classes so dark mode works.
Tooltips: every column has data-tip; app.js shows it on hover/focus."""
import math
from markupsafe import Markup, escape

def _nice(mx):
    if mx <= 4: return max(1, int(mx)), 1 if mx > 0 else 1
    raw = mx / 4; mag = 10 ** math.floor(math.log10(raw)); step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    return math.ceil(mx / step) * step, step

def chart(labels, series, kind="bar", height=220, unit="", width=640):
    """labels: [str]; series: [(name, [values], 'text-xxx')] -> Markup <figure>."""
    W, H, L, R, T, B = width, height, 30, 6, 12, 26
    mx = max([0] + [v for _, vs, _ in series for v in vs]); top, step = _nice(mx) if mx else (4, 1)
    pw, ph = W - L - R, H - T - B; n = max(1, len(labels)); cw = pw / n
    y = lambda v: T + ph - ph * v / top
    out = [f'<figure class="chart relative select-none" role="img" aria-label="{escape(", ".join(s[0] for s in series))} by period">',
           '<div class="flex flex-wrap gap-x-4 gap-y-1 mb-2 text-[11.5px] text-stone-500 dark:text-neutral-400">']
    for name, _, col in series: out.append(f'<span class="flex items-center gap-1.5"><i class="w-2.5 h-2.5 rounded-sm {col} bg-current"></i>{escape(name)}</span>')
    out.append(f'</div><svg viewBox="0 0 {W} {H}" class="w-full h-auto overflow-visible" preserveAspectRatio="none">')
    t = 0
    while t <= top + 1e-9:
        yy = y(t); out.append(f'<line x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}" class="stroke-stone-200 dark:stroke-white/10" stroke-width="1"{"" if t == 0 else " stroke-dasharray=\"3 4\""}/>'
                              f'<text x="{L - 8}" y="{yy + 3.5:.1f}" text-anchor="end" class="fill-stone-400 dark:fill-neutral-500" font-size="10.5">{t:g}</text>'); t += step
    k = len(series)
    for i, lab in enumerate(labels):
        x0 = L + i * cw
        tip = f"{lab}: " + " · ".join(f"{vs[i]}{unit} {name.lower()}" for name, vs, _ in series)
        out.append(f'<g data-tip="{escape(tip)}" tabindex="0" class="outline-none group"><rect x="{x0:.1f}" y="{T}" width="{cw:.1f}" height="{ph}" fill="transparent" class="group-hover:fill-stone-100/70 dark:group-hover:fill-white/[.04] group-focus:fill-stone-100/70"/>')
        if kind == "bar":
            bw = min(22, cw * 0.62 / k)
            for j, (name, vs, col) in enumerate(series):
                v = vs[i]; bx = x0 + cw / 2 - bw * k / 2 - (k - 1) + j * (bw + 2); hh = max(0, ph * v / top)
                out.append(f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{hh:.1f}" rx="{min(4, bw / 3):.1f}" class="{col} fill-current"/>')
        out.append(f'<text x="{x0 + cw / 2:.1f}" y="{H - 8}" text-anchor="middle" class="fill-stone-400 dark:fill-neutral-500" font-size="10.5">{escape(lab)}</text></g>')
    if kind == "line":
        for name, vs, col in series:
            pts = " ".join(f"{L + (i + .5) * cw:.1f},{y(v):.1f}" for i, v in enumerate(vs))
            out.append(f'<polyline points="{pts}" fill="none" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" class="{col} stroke-current pointer-events-none"/>')
            out += [f'<circle cx="{L + (i + .5) * cw:.1f}" cy="{y(v):.1f}" r="3.2" class="{col} fill-current stroke-white dark:stroke-neutral-900 pointer-events-none" stroke-width="1.5"/>' for i, v in enumerate(vs)]
    if not mx: out.append(f'<text x="{L + pw / 2:.1f}" y="{T + ph / 2:.1f}" text-anchor="middle" class="fill-stone-400 dark:fill-neutral-500" font-size="12">No data yet: fills in after the first scans</text>')
    out.append('</svg><div class="chart-tip pointer-events-none absolute z-10 hidden rounded-lg bg-ink text-white dark:bg-white dark:text-ink text-[11.5px] font-medium px-2.5 py-1.5 shadow-lg whitespace-nowrap"></div></figure>')
    return Markup("".join(out))
