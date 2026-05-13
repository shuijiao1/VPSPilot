#!/usr/bin/env python3
import argparse, gzip, ipaddress, re, sys, time, socket, zlib
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser

OUTDIR = Path('/data/media/bgp')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Referer': 'https://bgp.tools/',
    'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
    'Sec-Ch-Ua': '"Chromium";v="122", "Google Chrome";v="122", "Not=A?Brand";v="99"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Sec-Fetch-Dest': 'document',
    'Dnt': '1',
    'Sec-Gpc': '1',
    'Pragma': 'no-cache',
}

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
    def handle_data(self, data):
        if data and data.strip(): self.parts.append(data.strip())
    def text(self): return '\n'.join(self.parts)

def resolve_target(s):
    raw = s.strip()
    m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', raw)
    if m:
        try:
            return ipaddress.IPv4Address(m.group(1)), None
        except Exception:
            raise SystemExit('ERROR: invalid IPv4')

    # Treat as domain/hostname: strip scheme/path/port and resolve A records.
    host = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', '', raw).split('/')[0].split('?')[0].strip('[]')
    if '@' in host:
        host = host.rsplit('@', 1)[-1]
    if ':' in host and host.count(':') == 1:
        host = host.rsplit(':', 1)[0]
    host = host.strip().rstrip('.')
    if not host or not re.match(r'^[A-Za-z0-9.-]+$', host):
        raise SystemExit('ERROR: no IPv4 or valid domain found')

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SystemExit(f'ERROR: failed to resolve domain {host}: {e}')
    ips = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise SystemExit(f'ERROR: no IPv4 A record found for {host}')
    return ipaddress.IPv4Address(ips[0]), host

def prefixes(ip):
    # Prefer real visible prefixes from bgp.tools search, sorted by highest visibility.
    # Tie-breaker: more-specific first, then bgp.tools row order. Blind /24 can be wrong.
    real = search_prefixes(ip)
    if real:
        return [net for net, _visibility, _asn in real]
    p24 = ipaddress.IPv4Network(f'{ip}/24', strict=False)
    p23 = ipaddress.IPv4Network(f'{ip}/23', strict=False)
    res=[p24]
    if p23 != p24: res.append(p23)
    return res


def search_prefixes(ip):
    url=f'https://bgp.tools/search?q={ip}'
    try:
        html,_=fetch(url)
    except Exception:
        return []
    rows=[]
    text=html.decode('utf-8','ignore')
    visibility_rank={'high': 3, 'medium': 2, 'low': 1}
    seen=set()
    for rm in re.finditer(r'<tr\b[^>]*>(.*?)</tr>', text, re.I | re.S):
        row=rm.group(1)
        pm=re.search(r'/prefix/(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})', row)
        if not pm:
            continue
        try:
            net=ipaddress.IPv4Network(pm.group(1), strict=False)
        except Exception:
            continue
        if ip not in net or net in seen:
            continue
        seen.add(net)
        am=re.search(r'/as/(\d+)', row, re.I)
        asn=f'AS{am.group(1)}' if am else ''
        cells=re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.I | re.S)
        cell_text=[re.sub(r'<[^>]+>', ' ', c).strip() for c in cells]
        visibility=''
        for c in reversed(cell_text):
            lc=re.sub(r'\s+', ' ', c).strip().lower()
            if lc in visibility_rank:
                visibility=lc
                break
        rows.append((net, visibility, asn, visibility_rank.get(visibility, 0), len(rows)))
    # bgp.tools search may show route objects (e.g. RADB) without /prefix links or
    # visibility cells. Include those containing prefixes so /21-/16 announcements
    # are not missed when the visibility table is absent/noisy.
    for m in re.finditer(r'\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b', text):
        try:
            net=ipaddress.IPv4Network(m.group(1), strict=False)
        except Exception:
            continue
        if ip not in net or net in seen:
            continue
        seen.add(net)
        # Unknown visibility ranks below explicit High/Medium/Low rows, but above blind fallback.
        rows.append((net, 'unknown', '', 0, len(rows)))
    rows.sort(key=lambda x: (x[3], x[0].prefixlen, -x[4]), reverse=True)
    return [(net, visibility, asn) for net, visibility, asn, _rank, _idx in rows]

def fetch(url, timeout=20):
    req=Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as r:
        data = r.read()
        enc = (r.headers.get('content-encoding') or '').lower()
        if enc == 'gzip':
            data = gzip.decompress(data)
        elif enc == 'deflate':
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        return data, r.headers.get('content-type','')

def placeholder(svg: bytes):
    txt = svg[:20000].decode('utf-8', 'ignore')
    return 'Not_Visible' in txt and 'in_DFZ' in txt

def svg_to_png(svg_path: Path, png_path: Path):
    # Prefer cairosvg if present, fallback to rsvg-convert, then ImageMagick.
    try:
        import cairosvg
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), output_width=2400)
        return
    except Exception as e:
        last=e
    import subprocess, shutil
    if shutil.which('rsvg-convert'):
        subprocess.check_call(['rsvg-convert','-w','2400','-f','png','-o',str(png_path),str(svg_path)])
        return
    if shutil.which('magick'):
        subprocess.check_call(['magick','-density','300',str(svg_path),'-resize','2400x1800>',str(png_path)])
        return
    raise RuntimeError(f'no SVG converter available; install cairosvg/sharp/librsvg/imagemagick. last={last}')

def fetch_bgp(ip, domain=None, outdir=OUTDIR):
    outdir.mkdir(parents=True, exist_ok=True)
    tried=[]; ph=None
    for net in prefixes(ip):
        pfx=str(net)
        urlip=pfx.replace('/','_')
        url=f'https://bgp.tools/pathimg/rt-{urlip}?4c1db184-e649-4491-8b7f-06177bcb4f25&loggedin'
        tried.append(url)
        try:
            data, ctype = fetch(url)
        except HTTPError as e:
            if e.code == 404: continue
            continue
        except URLError:
            continue
        if placeholder(data):
            ph=pfx; continue
        stamp=int(time.time())
        base=f'bgp-{str(net).replace("/","_")}-{stamp}'
        svg=outdir/(base+'.svg')
        png=outdir/(base+'.png')
        target_safe=re.sub(r'[^A-Za-z0-9_.-]+', '_', str(domain or ip)).strip('_') or 'target'
        latest=outdir/(f'latest-{target_safe}.png')
        svg.write_bytes(data)
        svg_to_png(svg, png)
        latest.write_bytes(png.read_bytes())
        try: svg.unlink()
        except Exception: pass
        print(f'OK\nTARGET={domain or ip}\nIP={ip}\nPREFIX={pfx}\nPNG={png}\nLATEST={latest}\nURL=https://bgp.tools/prefix/{pfx}')
        return 0
    if ph:
        print(f'PLACEHOLDER\nTARGET={domain or ip}\nIP={ip}\nPREFIX={ph}\nURL=https://bgp.tools/prefix/{ph}\nREASON=bgp.tools temporarily returned no path image; please retry once')
        return 2
    pfx=str(prefixes(ip)[0])
    print(f'NONE\nTARGET={domain or ip}\nIP={ip}\nPREFIX={pfx}\nURL=https://bgp.tools/prefix/{pfx}\nREASON=no usable BGP path image found')
    return 3

def tld(domain):
    parts=domain.split('.')
    return '.'.join(parts[-2:]) if len(parts)>=2 else domain

def fetch_dns(ip, domain=None):
    for net in prefixes(ip):
        pfx=str(net)
        url=f'https://bgp.tools/prefix/{pfx}#dns'
        try:
            html,_=fetch(url)
        except Exception:
            continue
        parser=TextParser(); parser.feed(html.decode('utf-8','ignore'))
        text=parser.text()
        rows=re.findall(r'(\d{1,3}(?:\.\d{1,3}){3})\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text)
        counts={}
        for _,d in rows: counts[tld(d)]=counts.get(tld(d),0)+1
        lines=[f'{a}\t{d}' for a,d in rows if counts.get(tld(d),0)<=2]
        if lines:
            print('OK_DNS')
            print(f'TARGET={domain or ip}\nIP={ip}\nPREFIX={pfx}\nURL={url}')
            print('DNS_LINES_BEGIN')
            print('\n'.join(lines[:80]))
            print('DNS_LINES_END')
            return 0
    print(f'NONE_DNS\nTARGET={domain or ip}\nIP={ip}\nREASON=no DNS records found')
    return 3

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--dns', action='store_true')
    ap.add_argument('--outdir', default=str(OUTDIR), help='directory for generated BGP images')
    ap.add_argument('ip')
    args=ap.parse_args()
    ip, domain = resolve_target(args.ip)
    return fetch_dns(ip, domain) if args.dns else fetch_bgp(ip, domain, Path(args.outdir))

if __name__ == '__main__':
    raise SystemExit(main())
