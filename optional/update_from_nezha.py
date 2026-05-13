#!/usr/bin/env python3
import base64, json, os, socket, ssl, time, urllib.request, shutil, http.cookiejar
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
OUT = Path(os.environ.get('JIAOOPS_INV', ROOT / 'servers.json'))
DEFAULT_KEY = os.environ.get('JIAOOPS_DEFAULT_KEY', '/data/keys/id_ed25519')
DEFAULT_PORT = int(os.environ.get('JIAOOPS_DEFAULT_PORT', '22'))
DEFAULT_USER = os.environ.get('JIAOOPS_DEFAULT_USER', 'root')
PANEL = os.environ.get('NEZHA_URL', os.environ.get('NEZHA_PANEL', '')).strip().removeprefix('https://').removeprefix('http://').rstrip('/')
WS_PATH = os.environ.get('NEZHA_WS_PATH', '/api/v1/ws/server')
SERVICE_URL = f'https://{PANEL}/api/v1/service' if PANEL else ''
SERVER_URL = f'https://{PANEL}/api/v1/server' if PANEL else ''
LOGIN_URL = f'https://{PANEL}/api/v1/login' if PANEL else ''
NEZHA_USER = os.environ.get('NEZHA_USER', '')
NEZHA_PASSWORD = os.environ.get('NEZHA_PASSWORD', '')
ALIASES = json.loads(os.environ.get('JIAOOPS_ALIASES_JSON', '{}') or '{}')

def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def fetch_json(url, opener=None, data=None):
    req = urllib.request.Request(url)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(data).encode()
    if opener is None:
        rctx = urllib.request.urlopen(req, timeout=10)
    else:
        rctx = opener.open(req, timeout=10)
    with rctx as r:
        return json.load(r)

def fetch_admin_servers():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = fetch_json(LOGIN_URL, opener, {'username': NEZHA_USER, 'password': NEZHA_PASSWORD})
    if not login.get('success'):
        raise RuntimeError(login.get('error') or 'Nezha login failed')
    data = fetch_json(SERVER_URL, opener)
    if not data.get('success'):
        raise RuntimeError(data.get('error') or 'Nezha server API failed')
    return data.get('data') or []

def ws_read_one(host=PANEL, path=WS_PATH):
    key = base64.b64encode(os.urandom(16)).decode()
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(socket.create_connection((host, 443), timeout=10), server_hostname=host)
    try:
        req = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            f'Origin: https://{host}\r\n\r\n'
        )
        sock.sendall(req.encode())
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        head, rest = resp.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head:
            raise RuntimeError(head.decode(errors='replace'))
        buf = rest
        while len(buf) < 2:
            buf += sock.recv(4096)
        b2 = buf[1]
        length = b2 & 0x7f
        pos = 2
        if length == 126:
            while len(buf) < pos + 2:
                buf += sock.recv(4096)
            length = int.from_bytes(buf[pos:pos+2], 'big'); pos += 2
        elif length == 127:
            while len(buf) < pos + 8:
                buf += sock.recv(4096)
            length = int.from_bytes(buf[pos:pos+8], 'big'); pos += 8
        mask = None
        if b2 >> 7:
            while len(buf) < pos + 4:
                buf += sock.recv(4096)
            mask = buf[pos:pos+4]; pos += 4
        while len(buf) < pos + length:
            buf += sock.recv(4096)
        payload = bytearray(buf[pos:pos+length])
        if mask:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        return json.loads(payload.decode())
    finally:
        sock.close()

def fetch_service_stats():
    data = fetch_json(SERVICE_URL)
    stats = data.get('data', {}).get('cycle_transfer_stats', {}) if data.get('success') else {}
    by_sid = {}
    for item in stats.values():
        names = item.get('server_name') or {}
        transfers = item.get('transfer') or {}
        nexts = item.get('next_update') or {}
        for sid in names:
            try:
                sid_i = int(sid)
            except Exception:
                continue
            by_sid[sid_i] = {
                'traffic_name': item.get('name'),
                'cycle_from': item.get('from'),
                'cycle_to': item.get('to'),
                'traffic_max': item.get('max'),
                'traffic_used': transfers.get(sid),
                'next_update': nexts.get(sid),
            }
    return by_sid

def main():
    if not PANEL:
        raise SystemExit('NEZHA_URL is not configured')
    try:
        source_servers = fetch_admin_servers()
        source = f'nezha:{PANEL} admin api + service api; pruned absent servers'
        now = int(time.time() * 1000)
        online = None
    except Exception as e:
        print(f'admin api failed, falling back to public websocket: {e}')
        ws = ws_read_one()
        source_servers = ws.get('servers', [])
        source = f'nezha:{PANEL} public websocket + service api; pruned absent servers'
        now = ws.get('now')
        online = ws.get('online')

    traffic = fetch_service_stats()
    if OUT.exists():
        backup = OUT.with_name(f'{OUT.name}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
        shutil.copy2(OUT, backup)

    old_by_id = {}
    if OUT.exists():
        try:
            old_data = json.loads(OUT.read_text() or '{}')
            old_by_id = {int(x['nezha_id']): x for x in old_data.get('servers', []) if x.get('nezha_id') is not None}
        except Exception:
            old_by_id = {}

    servers = []
    online_count = 0
    for s in source_servers:
        sid = int(s['id'])
        host = s.get('host') or {}
        state = s.get('state') or {}
        geoip = s.get('geoip') or {}
        ip = geoip.get('ip') or {}
        if s.get('last_active'):
            online_count += 1
        old = old_by_id.get(sid, {})
        old_ssh = old.get('ssh') or {}
        item = {
            'name': s.get('name'),
            'host': ip.get('ipv4_addr'),
            'ipv6': ip.get('ipv6_addr'),
            'port': old_ssh.get('port') or old.get('port') or DEFAULT_PORT,
            'user': old_ssh.get('user') or old.get('user') or DEFAULT_USER,
            'key': old_ssh.get('key') or old.get('key') or DEFAULT_KEY,
            'role': 'nezha',
            'risk': 'normal',
            'source': f'nezha:{PANEL}',
            'nezha_id': sid,
            'country': geoip.get('country_code') or s.get('country_code'),
            'platform': host.get('platform'),
            'platform_version': host.get('platform_version'),
            'arch': host.get('arch'),
            'virtualization': host.get('virtualization'),
            'cpu': ', '.join(host.get('cpu') or []),
            'mem_total': host.get('mem_total'),
            'disk_total': host.get('disk_total'),
            'display_index': s.get('display_index'),
            'last_active': s.get('last_active'),
            'uuid': s.get('uuid'),
            'public_note': s.get('public_note'),
            'state': state,
        }
        password = old_ssh.get('password') or old.get('password')
        auth = old_ssh.get('auth') or old.get('auth')
        if password:
            item['password'] = password
        if auth:
            item['auth'] = auth
        aliases = ALIASES.get(str(sid)) or ALIASES.get(sid)
        if aliases:
            item['aliases'] = aliases
        item.update(traffic.get(sid, {}))
        servers.append(item)

    data = {
        'updated_at': now_iso(),
        'source': source,
        'now': now,
        'online': online if online is not None else online_count,
        'servers': servers,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(f'updated {OUT}: {len(servers)} servers')
    for s in servers:
        print(f"{s['nezha_id']:>3} {s['name']:<18} {s.get('host') or '<no-host>':<15} {s.get('last_active')}")

if __name__ == '__main__':
    main()
