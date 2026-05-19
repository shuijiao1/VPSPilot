#!/usr/bin/env python3
import asyncio
import html
import json
import os
import select
import signal
import re
import subprocess
import tempfile
import time
from datetime import datetime
import urllib.request
import shutil
import shlex
import socket
from pathlib import Path
from typing import Iterable
from collections import OrderedDict, deque
from urllib.parse import urlparse
import urllib.error
from PIL import Image, ImageDraw, ImageFont

from auth import inventory_defaults, resolve_ssh, scp_from_args, ssh_args as build_ssh_args

from telegram import BotCommand, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

GUKO_VERSION = os.environ.get('GUKO_VERSION', '0.1.0').strip() or '0.1.0'
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
SERVERS_JSON = Path(os.environ.get('GUKO_INV') or os.environ.get('VPSPILOT_INV') or DATA_DIR / 'servers.json')
MEDIA_DIR = Path(os.environ.get('MEDIA_DIR', DATA_DIR / 'media'))
TMP_DIR = Path(os.environ.get('TMP_DIR', DATA_DIR / 'tmp'))
KEYS_DIR = Path(os.environ.get('KEYS_DIR', DATA_DIR / 'keys'))
RENDER_CHECKPLACE = Path(os.environ.get('RENDER_CHECKPLACE', '/app/render_checkplace.py'))
BGP_FETCH = Path(os.environ.get('BGP_FETCH', DATA_DIR / 'tools/bgp_fetch.py'))
IPPURE_DOWNLOAD = Path(os.environ.get('IPPURE_DOWNLOAD', DATA_DIR / 'tools/download_ippure.js'))
BGP_OUT_ROOT = Path(os.environ.get('BGP_OUT_ROOT', MEDIA_DIR / 'guko-bgp'))
IPPURE_TMP_ROOT = Path(os.environ.get('IPPURE_TMP_ROOT', TMP_DIR / 'guko-ippure'))
BOT_TOKEN = os.environ.get('BOT_TOKEN', '').strip()
ALLOWED_USERS = {x.strip() for x in os.environ.get('ALLOWED_USERS', '').split(',') if x.strip()}
ADMIN_USERS = {x.strip() for x in os.environ.get('ADMIN_USERS', '').split(',') if x.strip()} or set(ALLOWED_USERS)
ALLOW_INSECURE_STARTUP = os.environ.get('ALLOW_INSECURE_STARTUP', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
SCRIPT_SOURCES = {
    'nexttrace': ('NextTrace', 'https://github.com/nxtrace/NTrace-core'),
    'stream': ('RegionRestrictionCheck', 'https://github.com/lmc999/RegionRestrictionCheck'),
    'ipq': ('Check.Place', 'https://github.com/xykt/NetQuality'),
    'nq': ('NodeQuality / Check.Place', 'https://github.com/xykt/NodeQuality'),
}
GB5_VERSION = '5.5.1'
GB5_URL = f'https://cdn.geekbench.com/Geekbench-{GB5_VERSION}-Linux.tar.gz'
JOBS = {}
RUNNING = set()
QUEUES = {}
QUEUE_WORKERS = {}
PENDING_NEXTTRACE = {}
ADD_SESSIONS = {}
HISTORY_JSON = Path(os.environ.get('HISTORY_JSON', DATA_DIR / 'history.json'))
RESULTS_DIR = Path(os.environ.get('RESULTS_DIR', DATA_DIR / 'results'))
# Each server/test kind only keeps the latest finished result; this stays intentionally small.
HISTORY_LIMIT = int(os.environ.get('HISTORY_LIMIT', '500'))


def startup_check():
    problems = []
    if not BOT_TOKEN or BOT_TOKEN in {'123456:replace-me', '123:abc', 'CHANGE_ME'}:
        problems.append('BOT_TOKEN is empty or still an example value')
    if not ALLOWED_USERS:
        problems.append('ALLOWED_USERS is empty; GUKO requires whitelist mode')
    if '*' in ALLOWED_USERS or '0' in ALLOWED_USERS:
        problems.append('ALLOWED_USERS contains unsafe wildcard-like value')
    for d in (DATA_DIR, MEDIA_DIR, TMP_DIR, KEYS_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
    if SERVERS_JSON.exists():
        try:
            inv = json.loads(SERVERS_JSON.read_text() or '{}')
            leaked = []
            blob = json.dumps(inv, ensure_ascii=False)
            for marker in ('BOT_TOKEN', 'CHANGE_ME', 'PRIVATE KEY'):
                if marker in blob:
                    leaked.append(marker)
            if leaked:
                problems.append('servers.json appears to contain private/example markers: ' + ', '.join(leaked))
        except Exception as e:
            problems.append(f'cannot parse servers inventory: {e}')
    if problems and not ALLOW_INSECURE_STARTUP:
        raise SystemExit('安全启动检查失败：\n- ' + '\n- '.join(problems) + '\n\n请配置 .env；如确实要临时跳过，设置 ALLOW_INSECURE_STARTUP=true')
    if problems:
        print('WARNING: insecure startup allowed:\n- ' + '\n- '.join(problems), flush=True)


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and str(user.id) in ALLOWED_USERS)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and str(user.id) in ADMIN_USERS)


async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    user = update.effective_user
    uid = user.id if user else 'unknown'
    if update.callback_query:
        await update.callback_query.answer('无权限', show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(f'无权限使用这个GUKO bot。你的 ID：{uid}')
    return False


async def admin_guard(update: Update) -> bool:
    if not await guard(update):
        return False
    if is_admin(update):
        return True
    if update.callback_query:
        await update.callback_query.answer('需要管理员权限', show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text('需要管理员权限。')
    return False


def load_inventory() -> dict:
    if not SERVERS_JSON.exists():
        inv = {
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'source': 'local',
            'defaults': {
                'ssh': {
                    'user': os.environ.get('GUKO_DEFAULT_USER') or os.environ.get('VPSPILOT_DEFAULT_USER') or os.environ.get('JIAOOPS_DEFAULT_USER', 'root'),
                    'port': int(os.environ.get('GUKO_DEFAULT_PORT') or os.environ.get('VPSPILOT_DEFAULT_PORT') or os.environ.get('JIAOOPS_DEFAULT_PORT', '22')),
                    'key': os.environ.get('GUKO_DEFAULT_KEY') or os.environ.get('VPSPILOT_DEFAULT_KEY') or os.environ.get('JIAOOPS_DEFAULT_KEY', '/data/keys/id_ed25519'),
                }
            },
            'servers': [],
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SERVERS_JSON.write_text(json.dumps(inv, ensure_ascii=False, indent=2) + '\n')
        return inv
    return json.loads(SERVERS_JSON.read_text())


def save_inventory(inv: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SERVERS_JSON.exists():
        backup = SERVERS_JSON.with_name(f'{SERVERS_JSON.name}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
        shutil.copy2(SERVERS_JSON, backup)
    SERVERS_JSON.write_text(json.dumps(inv, ensure_ascii=False, indent=2) + '\n')


def next_manual_id(servers):
    used = set()
    for s in servers:
        try:
            used.add(int(server_id(s)))
        except Exception:
            pass
    n = -1
    while n in used:
        n -= 1
    return n


def redact_inventory(inv: dict):
    def clean_server(s):
        out = json.loads(json.dumps(s, ensure_ascii=False))
        ssh = out.get('ssh') or {}
        if 'password' in ssh:
            ssh['password'] = '***'
        if out.get('password'):
            out['password'] = '***'
        if 'key' in ssh and ssh.get('key'):
            ssh['key'] = str(ssh['key']).replace(str(DATA_DIR), '/data')
        if out.get('key'):
            out['key'] = str(out['key']).replace(str(DATA_DIR), '/data')
        out['ssh'] = ssh
        return out
    data = {k: v for k, v in inv.items() if k != 'servers'}
    defaults = json.loads(json.dumps(data.get('defaults') or {}, ensure_ascii=False))
    dssh = defaults.get('ssh') or {}
    if 'password' in dssh:
        dssh['password'] = '***'
    defaults['ssh'] = dssh
    data['defaults'] = defaults
    data['servers'] = [clean_server(s) for s in inv.get('servers', [])]
    return data


def server_id(s):
    for key in ('id', 'legacy_id', 'nezha_id'):
        if s.get(key) is not None:
            return s.get(key)
    host = str(s.get('host') or '').strip()
    if host:
        port = (s.get('ssh') or {}).get('port') or s.get('port')
        if port:
            safe_host = re.sub(r'[^A-Za-z0-9_.-]+', '_', host).strip('_')
            return f'{safe_host}-{port}'
        return host
    return s.get('name')

def update_server_by_id(sid: str, patch: dict):
    inv = load_inventory()
    servers = inv.get('servers') or []
    for i, s in enumerate(servers):
        if str(server_id(s)) == str(sid):
            merged = dict(s)
            ssh = dict(merged.get('ssh') or {})
            for k in ('name', 'host', 'aliases', 'role'):
                if k in patch:
                    merged[k] = patch[k]
            if 'ssh' in patch:
                ssh.update(patch['ssh'])
                merged['ssh'] = ssh
            servers[i] = merged
            inv['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            save_inventory(inv)
            return merged
    return None


def delete_server_by_id(sid: str):
    inv = load_inventory()
    servers = inv.get('servers') or []
    kept = []
    removed = None
    for s in servers:
        if str(server_id(s)) == str(sid):
            removed = s
        else:
            kept.append(s)
    if removed is None:
        return None
    inv['servers'] = kept
    inv['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    save_inventory(inv)
    return removed


def upsert_server(item: dict):
    inv = load_inventory()
    servers = inv.setdefault('servers', [])
    q = {str(x).lower() for x in [item.get('name'), str(item.get('id') or item.get('legacy_id') or '')] if x}
    q.update(str(x).lower() for x in (item.get('aliases') or []))
    item_host = str(item.get('host') or '').lower()
    item_port = (item.get('ssh') or {}).get('port') or item.get('port')
    replaced = False
    for i, old in enumerate(servers):
        fields = {str(x).lower() for x in [old.get('name'), str(old.get('id') or old.get('legacy_id') or '')] if x}
        fields.update(str(x).lower() for x in (old.get('aliases') or []))
        old_host = str(old.get('host') or '').lower()
        old_port = (old.get('ssh') or {}).get('port') or old.get('port')
        same_endpoint = item_host and old_host == item_host and (not item_port or not old_port or str(item_port) == str(old_port))
        if (q & fields) or same_endpoint:
            item.setdefault('id', old.get('id') or old.get('legacy_id') or next_manual_id(servers))
            item.setdefault('state', old.get('state') or {})
            merged = dict(old)
            merged.update(enrich_server_geo(item))
            servers[i] = merged
            replaced = True
            break
    if not replaced:
        item.setdefault('id', next_manual_id(servers))
        item.setdefault('role', 'manual')
        item.setdefault('source', 'local-manual')
        item.setdefault('state', {})
        servers.append(enrich_server_geo(item))
    inv['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    save_inventory(inv)
    return item, 'updated' if replaced else 'added'


def ssh_config(s):
    return resolve_ssh(s, load_inventory())


def fmt_bytes(n):
    if n is None:
        return '-'
    n = float(n)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(n) < 1024:
            return f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.1f}PB'


def pct_num(used, total):
    if used is None or total in (None, 0):
        return None
    return float(used) / float(total) * 100


def pct(used, total):
    p = pct_num(used, total)
    return '-' if p is None else f'{p:.1f}%'


def meter(value, total=100, width=22):
    p = pct_num(value, total)
    if p is None:
        return '▕' + '▱' * width + '▏', '-'
    p = max(0, min(100, p))
    filled = max(0, min(width, round(p / 100 * width)))
    if p >= 85:
        icon = '🔴'
    elif p >= 65:
        icon = '🟠'
    else:
        icon = '🟢'
    # Braille/box chars render cleaner in Telegram than block+shade when wrapped in <code>.
    bar_text = '▕' + '▰' * filled + '▱' * (width - filled) + '▏'
    return bar_text, f'{icon} {p:.1f}%'


def usage_block(label, emoji, used, total):
    b, p = meter(used, total)
    return f'{emoji} <b>{label}</b>  {p}\n<code>{b}</code>\n{fmt_bytes(used)} / {fmt_bytes(total)}'


def cpu_block(cpu, width=22):
    b, p = meter(float(cpu or 0), 100, width)
    return f'🧠 <b>CPU</b>  {p}\n<code>{b}</code>'


def fmt_duration(seconds):
    if seconds is None:
        return '-'
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f'{days}天 {hours}小时'
    if hours:
        return f'{hours}小时 {minutes}分'
    return f'{minutes}分'


def short_cpu_name(name):
    if not name:
        return '-'
    text = str(name).replace('(R)', '').replace('(TM)', '')
    text = ' '.join(text.split())
    return text[:58] + ('…' if len(text) > 58 else '')


IPV4_RE = re.compile(r'(?:^|\D)((?:\d{1,3}\.){3}\d{1,3})(?:\D|$)')
DOMAIN_RE = re.compile(r'^(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#].*)?$')


def is_ipv4(value):
    m = IPV4_RE.search(str(value or ''))
    if not m:
        return False
    try:
        parts = [int(x) for x in m.group(1).split('.')]
        return len(parts) == 4 and all(0 <= x <= 255 for x in parts)
    except Exception:
        return False


def extract_ipv4(value):
    m = IPV4_RE.search(str(value or ''))
    return m.group(1) if m and is_ipv4(m.group(1)) else ''


def normalize_domain(value):
    text = str(value or '').strip()
    if not text or text.startswith('/'):
        return ''
    first = text.split()[0].strip()
    if extract_ipv4(first):
        return ''
    if not DOMAIN_RE.match(first):
        return ''
    parsed = urlparse(first if '://' in first else '//' + first)
    host = (parsed.hostname or '').strip().rstrip('.')
    return host if re.match(r'^[A-Za-z0-9.-]+$', host) else ''


def safe_target(value):
    text = str(value or '').strip()
    text = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', '', text).split('/')[0].split('?')[0].replace(':', '_')
    text = re.sub(r'[^A-Za-z0-9_.-]+', '_', text).strip('_')
    return text or 'target'


def country_flag(code):
    code = (code or '').strip().upper()
    if len(code) != 2 or not code.isalpha():
        return '🌐'
    return chr(0x1F1E6 + ord(code[0]) - ord('A')) + chr(0x1F1E6 + ord(code[1]) - ord('A'))


def geolocate_host(host, timeout=4):
    ip = extract_ipv4(host or '')
    if not ip:
        return None
    try:
        url = f'http://ip-api.com/json/{ip}?fields=status,countryCode,query,message'
        req = urllib.request.Request(url, headers={'User-Agent': 'GUKO/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode(errors='replace'))
        if data.get('status') == 'success' and data.get('countryCode'):
            return str(data['countryCode']).lower()
    except Exception:
        return None
    return None


def enrich_server_geo(item):
    if item.get('country'):
        item['country'] = str(item.get('country')).lower()
        return item
    code = geolocate_host(item.get('host'))
    if code:
        item['country'] = code
    return item


ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def strip_ansi(text):
    return ANSI_RE.sub('', text or '')


def safe(s):
    return html.escape(str(s)) if s is not None else '-'


def running_text(s, task):
    return f'正在运行中：{safe(s.get("name"))} {task}'


async def send_running_notice(bot, chat_id, s, task):
    await bot.send_message(chat_id, running_text(s, task), parse_mode=ParseMode.HTML)


async def bot_queue_notice(bot, chat_id, s, task, pos):
    if pos <= 1:
        await bot.send_message(chat_id, running_text(s, task), parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id, f'已加入队列：{safe(s.get("name"))} {task}（第 {pos} 个）', parse_mode=ParseMode.HTML)


def is_valid_hostname(value):
    text = str(value or '').strip()
    if not text or len(text) > 253 or ' ' in text:
        return False
    if is_ipv4(text):
        return True
    return bool(re.match(r'^[A-Za-z0-9.-]+$', text) and '.' in text)


def parse_host_port(text):
    raw = str(text or '').strip()
    if not raw:
        return '', None
    if raw.count(':') == 1 and not raw.startswith('['):
        host, port = raw.rsplit(':', 1)
        if port.isdigit():
            return host.strip(), int(port)
    return raw, None


def source_repo(kind):
    return SCRIPT_SOURCES.get(kind, ('', ''))[1]


def script_command_text(kind, **kwargs):
    if kind == 'nexttrace':
        target = kwargs.get('target') or '<目标IP或域名>'
        return (
            '脚本命令：\n'
            'curl -sL https://nxtrace.org/nt | bash\n'
            f'nexttrace {target}'
        )
    if kind == 'stream':
        region_id = kwargs.get('region_id') or '<地区编号>'
        proto_arg = kwargs.get('proto_arg') or '-M 4'
        return (
            '脚本命令：\n'
            'bash <(curl -L -s check.unlock.media) '
            f'{proto_arg} -R {region_id}'
        )
    if kind == 'ipq':
        return '脚本命令：\nbash <(curl -Ls https://IP.Check.Place) -y'
    if kind == 'nq':
        selected = kwargs.get('selected')
        ip_mode = kwargs.get('ip_mode')
        extra = ''
        if selected or ip_mode:
            extra = f'\n选择：{selected or "-"}；{ip_mode or "-"}'
        return '脚本命令：\nbash <(curl -sL https://run.NodeQuality.com)' + extra
    return ''


def script_command_html(kind, **kwargs):
    return safe(script_command_text(kind, **kwargs))


def find_server(name: str, servers: Iterable[dict]):
    q = name.lower()
    for s in servers:
        fields = [s.get('name'), s.get('host'), str(server_id(s))]
        fields += s.get('aliases') or []
        if any((f or '').lower() == q for f in fields):
            return s
    for s in servers:
        fields = [s.get('name'), s.get('host')] + (s.get('aliases') or [])
        if any(q in (f or '').lower() for f in fields):
            return s
    return None


def find_server_by_id(sid: str):
    return find_server(sid, load_inventory().get('servers', []))


def server_button_label(s):
    return f"{country_flag(s.get('country'))} {s.get('name')}"


def main_menu_markup():
    servers = load_inventory().get('servers', [])
    rows = []
    for i in range(0, len(servers), 2):
        rows.append([
            InlineKeyboardButton(server_button_label(s), callback_data=f"srv:{server_id(s)}")
            for s in servers[i:i+2]
        ])
    rows.append([
        InlineKeyboardButton('➕ 添加服务器', callback_data='add:start'),
        InlineKeyboardButton('📥 批量导入', callback_data='add:bulk'),
    ])
    return InlineKeyboardMarkup(rows)


def server_has_ipv6(s):
    return bool(s.get('ipv6') and str(s.get('ipv6')).strip() not in ('-', 'None'))


def add_start_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ 添加单台', callback_data='add:one')],
        [InlineKeyboardButton('📥 批量导入', callback_data='add:bulk')],
        [InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')],
    ])


def add_auth_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('♻️ 沿用默认密钥/配置', callback_data='addauth:default')],
        [InlineKeyboardButton('📁 使用已有密钥路径', callback_data='addauth:keypath')],
        [InlineKeyboardButton('🔑 上传/粘贴新私钥', callback_data='addauth:key')],
        [InlineKeyboardButton('🔐 使用密码', callback_data='addauth:password')],
        [InlineKeyboardButton('📦 先只保存，不测试登录', callback_data='addauth:skip')],
        [InlineKeyboardButton('❌ 取消', callback_data='add:cancel')],
    ])


def bulk_mode_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ 全部同一个端口', callback_data='bulkport:same')],
        [InlineKeyboardButton('🧩 每台自己写端口', callback_data='bulkport:per')],
        [InlineKeyboardButton('❌ 取消', callback_data='add:cancel')],
    ])


def bulk_auth_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔑 全部同一把密钥', callback_data='bulkauth:key')],
        [InlineKeyboardButton('🔐 全部同一个密码', callback_data='bulkauth:password')],
        [InlineKeyboardButton('🧩 每台自己写认证', callback_data='bulkauth:per')],
        [InlineKeyboardButton('📦 先只导入，不测试登录', callback_data='bulkauth:skip')],
        [InlineKeyboardButton('❌ 取消', callback_data='add:cancel')],
    ])


def add_help_text():
    return (
        '➕ <b>添加服务器</b>\n\n'
        '可以单台添加，也可以批量导入。\n'
        '支持：不同端口、不同用户名、沿用默认密钥、已有密钥路径、上传私钥、密码登录。'
    )


def bulk_help_text():
    return (
        '📥 <b>批量导入服务器</b>\n\n'
        '先选端口策略，再选认证策略。\n\n'
        '每行格式：\n'
        '<code>名称 IP 用户</code>\n'
        '<code>名称 IP:端口 用户</code>\n\n'
        '如果选择“每台自己写认证”，每行可以写：\n'
        '<code>名称 IP 端口 用户 key:/data/keys/a</code>\n'
        '<code>名称 IP 端口 用户 password:你的密码</code>'
    )


def tool_enabled(name):
    val = os.environ.get(f'ENABLE_{name.upper()}', 'true').strip().lower()
    return val in ('1', 'true', 'yes', 'on')



def button_rows(buttons, per_row=2):
    return [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]


def server_markup(s):
    sid = server_id(s)
    host = s.get('host') or ''
    rows = []
    if host:
        rows.append([InlineKeyboardButton('📋 复制 IPv4', copy_text=CopyTextButton(host))])
    if s.get('ipv6'):
        rows.append([InlineKeyboardButton('📋 复制 IPv6', copy_text=CopyTextButton(s.get('ipv6')))])

    test_buttons = []
    if tool_enabled('ipq'):
        test_buttons.append(InlineKeyboardButton('🧪 IP质量', callback_data=f'ipq:{sid}'))
    if tool_enabled('nq'):
        test_buttons.append(InlineKeyboardButton('📊 NodeQuality', callback_data=f'nqask:{sid}'))
    if tool_enabled('gb5'):
        test_buttons.append(InlineKeyboardButton('🏁 GB5', callback_data=f'gb5:{sid}'))
    if tool_enabled('stream'):
        test_buttons.append(InlineKeyboardButton('🎬 流媒体', callback_data=f'stream:{sid}'))
    if tool_enabled('bgp'):
        test_buttons.append(InlineKeyboardButton('🧭 BGP图', callback_data=f'bgp:{sid}'))
    if tool_enabled('ippure'):
        test_buttons.append(InlineKeyboardButton('🧼 IPPure', callback_data=f'ippure:{sid}'))
    rows.extend(button_rows(test_buttons, 2))

    ops_buttons = [
        InlineKeyboardButton('📋 当前任务', callback_data=f'jobsrv:{sid}'),
        InlineKeyboardButton('📜 历史记录', callback_data=f'hist:{sid}'),
        InlineKeyboardButton('🧪 测试SSH', callback_data=f'testssh:{sid}'),
    ]
    if tool_enabled('nexttrace'):
        ops_buttons.append(InlineKeyboardButton('🛣 NextTrace', callback_data=f'ntask:{sid}'))
    rows.extend(button_rows(ops_buttons, 2))
    rows.extend([
        [InlineKeyboardButton('✏️ 编辑', callback_data=f'edit:{sid}'), InlineKeyboardButton('🗑 删除', callback_data=f'delask:{sid}')],
        [InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')],
    ])
    return InlineKeyboardMarkup(rows)


NQ_ITEMS = [
    ('hardware', '硬件', 'HardwareQuality', 1),
    ('ip', 'IP质量', 'IPQuality', 2),
    ('net', '网络', 'NetQuality', 4),
    ('backroute', '回程', 'Backroute Trace', 8),
]
NQ_ALL_MASK = sum(x[3] for x in NQ_ITEMS)
NQ_DEFAULT_MASK = 0
NQ_IP_MODES = {'4': '仅 IPv4', '46': 'IPv4 + IPv6'}


def confirm_nq_markup(s, mask=NQ_DEFAULT_MASK, ip_mode='4'):
    sid = server_id(s)
    rows = []
    for _key, label, _full, bit in NQ_ITEMS:
        mark = '✅' if mask & bit else '☐'
        new_mask = mask & ~bit if mask & bit else mask | bit
        rows.append([InlineKeyboardButton(f'{mark} {label}', callback_data=f'nqtoggle:{sid}:{new_mask}:{ip_mode}')])
    rows.append([
        InlineKeyboardButton('全选', callback_data=f'nqsel:{sid}:{NQ_ALL_MASK}:{ip_mode}'),
        InlineKeyboardButton('清空', callback_data=f'nqsel:{sid}:0:{ip_mode}'),
    ])
    if server_has_ipv6(s):
        rows.append([
            InlineKeyboardButton(('✅ ' if ip_mode == '4' else '☐ ') + '仅 IPv4', callback_data=f'nqproto:{sid}:{mask}:4'),
            InlineKeyboardButton(('✅ ' if ip_mode == '46' else '☐ ') + 'IPv4 + IPv6', callback_data=f'nqproto:{sid}:{mask}:46'),
        ])
    run_text = '✅ 开始测试' if mask != NQ_ALL_MASK else '✅ 开始全测'
    rows.append([InlineKeyboardButton(run_text, callback_data=f'nqrun:{sid}:{mask}:{ip_mode}')])
    rows.append([InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')])
    return InlineKeyboardMarkup(rows)


def nq_selected(mask):
    return [item for item in NQ_ITEMS if mask & item[3]]


def nq_selected_text(mask):
    items = nq_selected(mask)
    if not items:
        return '未选择'
    return ' / '.join(item[2] for item in items)


def nq_ip_mode_text(ip_mode):
    return NQ_IP_MODES.get(str(ip_mode), NQ_IP_MODES['4'])


def nq_answer_script(mask):
    return ''.join(('y\n' if mask & bit else 'n\n') for _key, _label, _full, bit in NQ_ITEMS)


def nq_remote_ipv_arg(s, ip_mode):
    # NodeQuality default runs dual-stack when IPv6 exists. Force -4 for v4-only.
    return '' if (ip_mode == '46' and server_has_ipv6(s)) else '-4'


STREAM_REGION_BY_COUNTRY = {
    'tw': ('1', '跨国 + 台湾'),
    'hk': ('2', '跨国 + 香港'),
    'mo': ('2', '跨国 + 香港'),
    'jp': ('3', '跨国 + 日本'),
    'us': ('4', '跨国 + 北美'),
    'ca': ('4', '跨国 + 北美'),
    'br': ('5', '跨国 + 南美'),
    'ar': ('5', '跨国 + 南美'),
    'cl': ('5', '跨国 + 南美'),
    'gb': ('6', '跨国 + 欧洲'),
    'uk': ('6', '跨国 + 欧洲'),
    'de': ('6', '跨国 + 欧洲'),
    'fr': ('6', '跨国 + 欧洲'),
    'nl': ('6', '跨国 + 欧洲'),
    'au': ('7', '跨国 + 大洋洲'),
    'nz': ('7', '跨国 + 大洋洲'),
    'kr': ('8', '跨国 + 韩国'),
    'sg': ('9', '跨国 + 东南亚'),
    'my': ('9', '跨国 + 东南亚'),
    'th': ('9', '跨国 + 东南亚'),
    'vn': ('9', '跨国 + 东南亚'),
    'ph': ('9', '跨国 + 东南亚'),
    'id': ('9', '跨国 + 东南亚'),
    'in': ('10', '跨国 + 印度'),
    'za': ('11', '跨国 + 非洲'),
}


def stream_region_for_server(s):
    code = str(s.get('country') or '').lower()
    if not code:
        code = geolocate_host(s.get('host')) or ''
    return STREAM_REGION_BY_COUNTRY.get(code, ('0', '只测跨国平台'))


def stream_menu_text(s):
    rid, label = stream_region_for_server(s)
    proto = '优先 IPv4；若无 IPv4 自动改测 IPv6'
    return (
        f'🎬 准备在 <b>{safe(s.get("name"))}</b> 跑流媒体检测：\n\n'
        f'地区选项：<b>{safe(label)}</b>\n'
        f'协议策略：<b>{safe(proto)}</b>\n\n'
        '会在目标机器本机执行 RegionRestrictionCheck，并把结果整理成更好读的摘要。'
    )


def stream_markup(s):
    sid = server_id(s)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ 开始流媒体检测', callback_data=f'streamrun:{sid}')],
        [InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')],
    ])


def nq_menu_text(s, mask, ip_mode):
    return (
        f'📊 选择要在 <b>{safe(s.get("name"))}</b> 跑的 NodeQuality 项目：\n\n'
        f'当前项目：<b>{safe(nq_selected_text(mask))}</b>\n'
        f'IP 协议：<b>{safe(nq_ip_mode_text(ip_mode))}</b>\n\n'
        '点击项目进行选择/取消；全选就是完整 NodeQuality。'
    )

def menu_text():
    d = load_inventory()
    servers = d.get('servers', [])
    return (
        f'<b>GUKO</b> <code>v{safe(GUKO_VERSION)}</code>\n'
        f'服务器 <b>{len(servers)}</b> 台\n\n'
        '👇 点服务器打开操作面板。'
    )


def server_detail_text(s):
    name = safe(s.get('name'))
    title = f"{country_flag(s.get('country'))} <b>{name}</b>"
    cfg = ssh_config(s)
    lines = [
        title,
        f"<code>{safe(cfg.get('host'))}</code> · SSH <code>{safe(cfg.get('port'))}</code> · <code>{safe(cfg.get('user'))}</code>",
    ]
    ipv6 = s.get('ipv6')
    if ipv6 and str(ipv6).strip() not in ('-', 'None'):
        lines.append(f"IPv6 <code>{safe(ipv6)}</code>")
    return '\n'.join(lines)


def ssh_env_for(s):
    cfg = ssh_config(s)
    if cfg.get('auth') == 'password' and cfg.get('password'):
        env = os.environ.copy()
        env['SSHPASS'] = str(cfg['password'])
        return env
    return None


async def run_cmd(args, timeout=60, env=None):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, '命令超时'
    return proc.returncode, out.decode(errors='replace')


def ssh_args(s, remote, *, tty=False):
    return build_ssh_args(s, remote, tty=tty, inv=load_inventory())


async def test_server_login(s, timeout=15):
    try:
        code, out = await run_cmd(ssh_args(s, 'printf "ok:"; hostname', tty=False), timeout=timeout, env=ssh_env_for(s))
        return code == 0, strip_ansi(out).strip()
    except FileNotFoundError as e:
        return False, f'缺少依赖：{e}'
    except Exception as e:
        return False, str(e)


def build_server_item(name, host, user, port, auth_kind=None, password=None, key=None):
    item = {
        'name': name,
        'host': host,
        'role': 'manual',
        'source': 'local-manual',
        'ssh': {
            'user': user or 'root',
            'port': int(port or 22),
        },
    }
    if auth_kind == 'password':
        item['ssh']['auth'] = 'password'
        item['ssh']['password'] = password or ''
    elif auth_kind == 'key':
        item['ssh']['auth'] = 'key'
        item['ssh']['key'] = key or ''
    elif auth_kind == 'default':
        item['ssh']['auth'] = 'key'
    return enrich_server_geo(item)


def parse_bulk_lines(text, *, same_port=None, auth_mode='skip', shared_auth=None):
    items = []
    errors = []
    for lineno, raw in enumerate((text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        try:
            parts = shlex.split(line)
        except Exception as e:
            errors.append(f'第 {lineno} 行解析失败：{e}')
            continue
        if len(parts) < 2:
            errors.append(f'第 {lineno} 行字段太少')
            continue
        name = parts[0]
        host, embedded_port = parse_host_port(parts[1])
        idx = 2
        port = same_port or embedded_port
        if port is None and idx < len(parts) and parts[idx].isdigit():
            port = int(parts[idx]); idx += 1
        user = 'root'
        if idx < len(parts) and not parts[idx].startswith(('key:', 'password:', 'auth:')):
            user = parts[idx]; idx += 1
        if not port:
            port = 22
        if not is_valid_hostname(host):
            errors.append(f'第 {lineno} 行 IP/域名不正确：{host}')
            continue
        auth_kind = None
        password = None
        key = None
        if auth_mode == 'key':
            auth_kind, key = 'key', shared_auth
        elif auth_mode == 'password':
            auth_kind, password = 'password', shared_auth
        elif auth_mode == 'per':
            for token in parts[idx:]:
                if token.startswith('key:'):
                    auth_kind, key = 'key', token[4:]
                elif token.startswith('password:'):
                    auth_kind, password = 'password', token[9:]
            if not auth_kind:
                errors.append(f'第 {lineno} 行缺少 key: 或 password:')
                continue
        item = build_server_item(name, host, user, port, auth_kind, password, key)
        items.append(item)
    return items, errors


def save_private_key(chat_id, content, filename='id_key'):
    text = content.decode(errors='replace') if isinstance(content, bytes) else str(content or '')
    if 'PRIVATE KEY' not in text:
        raise ValueError('没有识别到 PRIVATE KEY 内容')
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = safe_target(filename).replace('.', '_')[:40] or 'id_key'
    path = KEYS_DIR / f'{chat_id}-{int(time.time())}-{safe_name}.pem'
    path.write_text(text.strip() + '\n')
    os.chmod(path, 0o600)
    return str(path)


def edit_markup(s):
    sid = server_id(s)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('改名称', callback_data=f'editfield:{sid}:name'), InlineKeyboardButton('改主机/IP', callback_data=f'editfield:{sid}:host')],
        [InlineKeyboardButton('改端口', callback_data=f'editfield:{sid}:port'), InlineKeyboardButton('改用户', callback_data=f'editfield:{sid}:user')],
        [InlineKeyboardButton('改密钥路径', callback_data=f'editfield:{sid}:key'), InlineKeyboardButton('改密码', callback_data=f'editfield:{sid}:password')],
        [InlineKeyboardButton('改为默认密钥', callback_data=f'editdefault:{sid}')],
        [InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')],
    ])


def add_session(chat_id, **data):
    cur = ADD_SESSIONS.setdefault(chat_id, {})
    cur.update(data)
    return cur


def clear_add_session(chat_id):
    ADD_SESSIONS.pop(chat_id, None)


async def finish_single_add(update: Update, context: ContextTypes.DEFAULT_TYPE, sess: dict):
    item = build_server_item(
        sess.get('name'), sess.get('host'), sess.get('user') or 'root', sess.get('port') or 22,
        sess.get('auth_kind'), sess.get('password'), sess.get('key'),
    )
    saved, action = upsert_server(item)
    ok_text = '未测试登录'
    if sess.get('auth_kind') in ('key', 'password', 'default'):
        ok, out = await test_server_login(saved)
        ok_text = ('✅ 登录成功：' if ok else '⚠️ 已保存，但登录测试失败：') + safe(out[-800:])
    clear_add_session(update.effective_chat.id)
    verb = '更新' if action == 'updated' else '添加'
    cfg = ssh_config(saved)
    await update.effective_message.reply_text(
        f'✅ 服务器已{verb}：<b>{safe(saved.get("name"))}</b>\n'
        f'<code>{safe(cfg.get("user"))}@{safe(cfg.get("host"))}:{safe(cfg.get("port"))}</code>\n\n'
        f'{ok_text}',
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_markup(),
    )


async def finish_bulk_add(update: Update, context: ContextTypes.DEFAULT_TYPE, sess: dict, text: str):
    items, errors = parse_bulk_lines(
        text,
        same_port=sess.get('same_port'),
        auth_mode=sess.get('auth_mode') or 'skip',
        shared_auth=sess.get('shared_auth'),
    )
    if not items:
        await update.effective_message.reply_text('没有可导入的服务器。\n' + '\n'.join(errors[:8]))
        return
    results = []
    for item in items:
        saved, action = upsert_server(item)
        results.append((saved, action))
    clear_add_session(update.effective_chat.id)
    lines = [f'✅ 已导入 {len(results)} 台服务器。']
    if errors:
        lines.append(f'⚠️ 跳过 {len(errors)} 行：')
        lines.extend(errors[:6])
    lines.append('\n前几台：')
    for saved, action in results[:8]:
        cfg = ssh_config(saved)
        lines.append(f'- {saved.get("name")}  {cfg.get("user")}@{cfg.get("host")}:{cfg.get("port")}  {action}')
    await update.effective_message.reply_text('\n'.join(safe(x) for x in lines), parse_mode=ParseMode.HTML, reply_markup=main_menu_markup())


def job_id(kind, s):
    return f"{kind}-{server_id(s)}-{int(time.time() * 1000)}"

KIND_NAME = {
    'ipq': 'IP质量', 'nq': 'NodeQuality', 'gb5': 'GB5', 'stream': '流媒体检测',
    'nexttrace': 'NextTrace', 'bgp': 'BGP图', 'ippure': 'IPPure图',
}
STATUS_ICON = {'queued': '🟡', 'running': '🟢', 'done': '✅', 'failed': '🔴'}


def iso_now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def kind_result_dir(s, kind):
    return RESULTS_DIR / str(server_id(s)) / str(kind)


def latest_result_files(s, kind):
    root = kind_result_dir(s, kind)
    if not root.exists():
        return []
    files = [p for p in root.iterdir() if p.is_file() and p.stat().st_size > 0]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def legacy_media_files(s, kind):
    host = str(s.get('host') or '').strip()
    files = []
    if kind == 'bgp' and host:
        for base in ('guko-bgp', 'vpspilot-bgp'):
            files.extend([p for p in (MEDIA_DIR / base).glob(f'*/latest-{host}.png') if p.is_file() and p.stat().st_size > 0])
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def all_result_files(s, kind):
    files = latest_result_files(s, kind)
    return files if files else legacy_media_files(s, kind)


def persist_result_file(s, kind, src, suffix=None):
    if not src:
        return None
    src = Path(src)
    if not src.exists() or not src.is_file() or src.stat().st_size <= 0:
        return None
    root = kind_result_dir(s, kind)
    root.mkdir(parents=True, exist_ok=True)
    for old in root.iterdir():
        if old.is_file():
            try:
                old.unlink()
            except Exception:
                pass
    ext = suffix or src.suffix or '.bin'
    dst = root / f'latest{ext}'
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return str(dst)


def latest_media_path(item):
    paths = item.get('media_paths') or []
    for x in paths:
        p = Path(x)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def load_history():
    if not HISTORY_JSON.exists():
        return []
    try:
        data = json.loads(HISTORY_JSON.read_text() or '[]')
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(items):
    HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_JSON.write_text(json.dumps(items[-HISTORY_LIMIT:], ensure_ascii=False, indent=2) + '\n')


def history_append(jid, job):
    item = {
        'job_id': jid,
        'server': job.get('server'),
        'server_id': job.get('server_id'),
        'kind': job.get('kind'),
        'status': job.get('status'),
        'target': job.get('target'),
        'selected': job.get('selected'),
        'ip_mode': job.get('ip_mode'),
        'region': job.get('region'),
        'started_at': job.get('started_at'),
        'completed_at': job.get('completed_at'),
        'duration_sec': job.get('duration_sec'),
        'urls': extract_urls(job.get('log') or '')[:8],
        'media_paths': job.get('media_paths') or ([] if not job.get('media_path') else [job.get('media_path')]),
        'log_tail': trim_log(strip_ansi(job.get('log') or ''), 3500),
    }
    hist = [
        x for x in load_history()
        if not (
            x.get('job_id') == jid
            or (str(x.get('server_id') or '') == str(item.get('server_id') or '') and x.get('kind') == item.get('kind'))
        )
    ]
    hist.append(item)
    save_history(hist)


def create_job(s, kind, status='queued', **extra):
    jid = job_id(kind, s)
    now = iso_now()
    JOBS[jid] = {
        'status': status,
        'server': s.get('name'),
        'server_id': str(server_id(s)),
        'kind': kind,
        'created_at': now,
        **extra,
    }
    if status == 'running':
        JOBS[jid]['started_at'] = now
    return jid


def start_job(s, kind, **extra):
    key = (server_id(s), kind)
    if key in RUNNING:
        return None, key
    RUNNING.add(key)
    jid = create_job(s, kind, status='running', **extra)
    return jid, key


def finish_job(jid, key=None):
    job = JOBS.get(jid) or {}
    now = iso_now()
    job.setdefault('status', 'done')
    job['completed_at'] = now
    try:
        st = datetime.fromisoformat(str(job.get('started_at') or job.get('created_at')))
        en = datetime.fromisoformat(now)
        job['duration_sec'] = max(0, int((en - st).total_seconds()))
    except Exception:
        pass
    JOBS[jid] = job
    history_append(jid, job)
    if key:
        RUNNING.discard(key)


def queue_key(s):
    return str(server_id(s))


def queue_position(s, jid):
    q = QUEUES.get(queue_key(s)) or deque()
    for i, entry in enumerate(q, start=1):
        if entry.get('jid') == jid:
            return i
    return None


async def queue_worker(s):
    qk = queue_key(s)
    q = QUEUES.setdefault(qk, deque())
    while q:
        entry = q[0]
        jid = entry['jid']
        job = JOBS.get(jid) or {}
        job['status'] = 'running'
        job['started_at'] = iso_now()
        JOBS[jid] = job
        RUNNING.add((server_id(s), job.get('kind')))
        try:
            await entry['runner'](*entry['args'])
        finally:
            q.popleft()
    QUEUE_WORKERS.pop(qk, None)


def enqueue_job(s, kind, runner, bot, chat_id, server, *runner_tail, **extra):
    qk = queue_key(s)
    jid = create_job(s, kind, status='queued', **extra)
    entry = {'jid': jid, 'runner': runner, 'args': (bot, chat_id, server, jid, *runner_tail)}
    q = QUEUES.setdefault(qk, deque())
    q.append(entry)
    if qk not in QUEUE_WORKERS or QUEUE_WORKERS[qk].done():
        QUEUE_WORKERS[qk] = asyncio.create_task(queue_worker(s))
    return jid, len(q)


def server_history(s, limit=20):
    sid = str(server_id(s))
    name = str(s.get('name') or '')
    host = str(s.get('host') or '')
    out = []
    seen = set()
    for item in load_history():
        if str(item.get('server_id') or '') == sid or str(item.get('server') or '') in (name, host, sid):
            kind = item.get('kind')
            if kind:
                seen.add(kind)
            out.append(item)
    scan_kinds = []
    root = RESULTS_DIR / sid
    if root.exists():
        scan_kinds.extend([p.name for p in root.iterdir() if p.is_dir()])
    scan_kinds.extend(['bgp'])
    for kind in sorted(set(scan_kinds)):
        if kind in seen:
            continue
        files = all_result_files(s, kind)
        if not files:
            continue
        newest = files[0]
        out.append({
            'job_id': f'file-{sid}-{kind}',
            'server': s.get('name'),
            'server_id': sid,
            'kind': kind,
            'status': 'done',
            'completed_at': datetime.fromtimestamp(newest.stat().st_mtime).astimezone().isoformat(timespec='seconds'),
            'media_paths': [str(p) for p in files],
            'log_tail': str(newest),
        })
    return out[-limit:]


def history_item_for(s, kind):
    for item in reversed(server_history(s, 50)):
        if item.get('kind') == kind:
            return item
    return None


def history_markup(s):
    sid = server_id(s)
    items = list(reversed(server_history(s, 50)))
    buttons = []
    seen = set()
    for item in items:
        kind = item.get('kind')
        if not kind or kind in seen:
            continue
        seen.add(kind)
        icon = STATUS_ICON.get(item.get('status'), '•')
        label = f'{icon} {KIND_NAME.get(kind, kind)}'
        buttons.append(InlineKeyboardButton(label, callback_data=f'histd:{sid}:{kind}'))
    rows = button_rows(buttons, 2)
    rows.append([InlineKeyboardButton('🔄 刷新历史', callback_data=f'hist:{sid}')])
    rows.append([InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')])
    return InlineKeyboardMarkup(rows)


def history_detail_text(s, kind):
    item = history_item_for(s, kind)
    if not item:
        return f'📜 <b>{safe(s.get("name"))}</b> 暂无 {safe(KIND_NAME.get(kind, kind))} 历史。'
    icon = STATUS_ICON.get(item.get('status'), '•')
    lines = [
        f'{icon} <b>{safe(s.get("name"))} · {safe(KIND_NAME.get(kind, kind))}</b>',
        f'状态：<b>{safe(item.get("status") or "-")}</b>',
    ]
    when = item.get('completed_at') or item.get('started_at')
    if when:
        lines.append(f'时间：<code>{safe(when)}</code>')
    if item.get('duration_sec') is not None:
        lines.append(f'耗时：<code>{safe(item.get("duration_sec"))}s</code>')
    params = []
    for label, key in [('目标', 'target'), ('选择', 'selected'), ('IP模式', 'ip_mode'), ('地区', 'region')]:
        if item.get(key):
            params.append(f'{label}：{item.get(key)}')
    if params:
        lines.append('参数：' + safe('；'.join(params)))
    urls = item.get('urls') or []
    if urls:
        lines.append('\n链接：\n' + '\n'.join(safe(u) for u in urls[:8]))
    media = latest_media_path(item)
    if media:
        lines.append('\n图片：点击后会重新发送最近一次结果图。')
    log_tail = (item.get('log_tail') or '').strip()
    if log_tail and not media:
        lines.append('\n详情：\n<pre>' + safe(log_tail[-3200:]) + '</pre>')
    else:
        lines.append('\n详情：暂无可展示内容。')
    return '\n'.join(lines)


def history_text(s):
    items = server_history(s, 20)
    if not items:
        return f'📜 <b>{safe(s.get("name"))}</b> 暂无测试历史。'
    lines = [f'📜 <b>{safe(s.get("name"))}</b> 最近一次测试结果', '点下面的功能按钮可以查看具体内容。']
    for item in reversed(items):
        icon = STATUS_ICON.get(item.get('status'), '•')
        kind = KIND_NAME.get(item.get('kind'), item.get('kind') or '-')
        extra = []
        for k in ('target', 'selected', 'ip_mode', 'region'):
            if item.get(k):
                extra.append(str(item.get(k)))
        dur = f" · {item.get('duration_sec')}s" if item.get('duration_sec') is not None else ''
        when = item.get('completed_at') or item.get('started_at') or '-'
        suffix = f" — {'；'.join(extra)}" if extra else ''
        lines.append(f'{icon} {safe(kind)} · {safe(item.get("status"))}{safe(dur)}\n   <code>{safe(when)}</code>{safe(suffix)}')
        urls = item.get('urls') or []
        if urls:
            lines.append('   ' + safe(urls[0]))
    return '\n'.join(lines)


def server_jobs(s):
    sid = str(server_id(s))
    name = str(s.get('name') or '')
    host = str(s.get('host') or '')
    found = []
    for jid, j in JOBS.items():
        if (j.get('status') or '') not in ('running', 'queued'):
            continue
        j_server = str(j.get('server') or '')
        if f'-{sid}-' in str(jid) or j_server in (name, host, sid):
            found.append((jid, j))
    found.sort(key=lambda kv: kv[1].get('created_at') or kv[1].get('started_at') or kv[0])
    return found


def compact_job_line(s, jid, j):
    status = j.get('status') or '-'
    icon = STATUS_ICON.get(status, '•')
    kind = KIND_NAME.get(j.get('kind'), j.get('kind') or '-')
    parts = [kind]
    if j.get('selected'):
        parts.append(str(j.get('selected')))
    elif j.get('target'):
        parts.append(str(j.get('target')))
    elif j.get('region'):
        parts.append(str(j.get('region')))
    label = '·'.join(parts)
    tail = status
    extras = []
    if j.get('ip_mode'):
        extras.append(str(j.get('ip_mode')))
    if status == 'queued':
        pos = queue_position(s, jid)
        if pos:
            extras.append(f'队列第 {pos} 个')
    if extras:
        tail += f"（{'；'.join(extras)}）"
    return f'{icon} {safe(label)} - {safe(tail)}'


def job_status_text(s):
    jobs = server_jobs(s)
    if not jobs:
        return f'📋 <b>{safe(s.get("name"))}</b> 当前没有任务。'
    lines = [f'📋 <b>{safe(s.get("name"))}</b> 当前任务']
    for jid, j in jobs[-10:]:
        lines.append(compact_job_line(s, jid, j))
    return '\n'.join(lines)


def extract_urls(text):
    text = strip_ansi(text or '')
    urls = re.findall(r'https?://[^\s<>"\'\x00-\x1f\x7f]+', text)
    return [u.rstrip('.,;，。)】]') for u in urls]


def first_report_url(text, category=None):
    urls = extract_urls(text)
    reports = [u for u in urls if 'Report.Check.Place' in u]
    if category:
        needle = f'/Report.Check.Place/{category}/'
        needle2 = f'Report.Check.Place/{category}/'
        for u in reports:
            clean = strip_ansi(u).strip()
            if needle in clean or needle2 in clean:
                return clean
        return None
    return strip_ansi(reports[0]).strip() if reports else None



def geekbench_urls(text):
    urls = []
    seen = set()
    for u in extract_urls(text):
        clean = strip_ansi(u).rstrip('.,;，。)】]')
        if re.search(r'browser\.geekbench\.com/v\d+/cpu/\d+', clean) and clean not in seen:
            urls.append(clean)
            seen.add(clean)
    return urls

def nodequality_url(text):
    for u in extract_urls(text):
        u = strip_ansi(u).rstrip('.,;，。')
        if 'nodequality.com/r/' in u:
            return u
    return None


def all_report_urls(text, category):
    urls = []
    seen = set()
    needle = f'Report.Check.Place/{category}/'
    for u in extract_urls(text):
        clean = strip_ansi(u).strip()
        if needle in clean and clean not in seen:
            urls.append(clean)
            seen.add(clean)
    return urls


def trim_log(text, limit=3200):
    text = (text or '').strip()
    return text[-limit:] if len(text) > limit else text



def run_pty_command_sync(args, timeout=900, send_enter_after=2):
    master, slave = os.openpty()
    try:
        try:
            os.set_blocking(master, False)
        except Exception:
            pass
        proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        slave = None
        out = bytearray()
        start = time.monotonic()
        sent_enter = False
        while True:
            now = time.monotonic()
            if not sent_enter and now - start >= send_enter_after:
                try:
                    os.write(master, b'\r')
                except OSError:
                    pass
                sent_enter = True
            if now - start > timeout:
                try:
                    proc.terminate()
                    time.sleep(1)
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                return 124, out.decode(errors='replace') + '\n命令超时'
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 8192)
                    if chunk:
                        out.extend(chunk)
                except OSError:
                    pass
            if proc.poll() is not None:
                # drain remaining output
                for _ in range(10):
                    r, _, _ = select.select([master], [], [], 0.05)
                    if not r:
                        break
                    try:
                        chunk = os.read(master, 8192)
                        if chunk:
                            out.extend(chunk)
                    except OSError:
                        break
                return proc.returncode, out.decode(errors='replace')
    finally:
        if slave is not None:
            try:
                os.close(slave)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass


async def run_pty_command(args, timeout=900, send_enter_after=2):
    return await asyncio.to_thread(run_pty_command_sync, args, timeout, send_enter_after)


async def send_long_text(bot, chat_id, text, *, parse_mode=None):
    text = text or '无输出'
    max_len = 3600 if parse_mode == ParseMode.HTML else 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or ['无输出']
    for chunk in chunks[:3]:
        await bot.send_message(chat_id, chunk, parse_mode=parse_mode)



async def run_until_report(args, timeout=900, env=None):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    out = bytearray()
    start = time.monotonic()
    report = None
    try:
        while True:
            if time.monotonic() - start > timeout:
                proc.kill()
                await proc.wait()
                return 124, out.decode(errors='replace') + '\n命令超时', report
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(1024), timeout=1)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue
            if chunk:
                out.extend(chunk)
                text = out.decode(errors='replace')
                report = first_report_url(text, 'ip') or report
                if report:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                    return 0, text, report
            elif proc.returncode is not None:
                break
        text = out.decode(errors='replace')
        return proc.returncode, text, first_report_url(text, 'ip')
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def run_subprocess(args, timeout, *, send_enter_after=None, env=None):
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    async def nudge_enter():
        if send_enter_after is None:
            return
        await asyncio.sleep(send_enter_after)
        if proc.returncode is None and proc.stdin:
            try:
                proc.stdin.write(b'\n')
                await proc.stdin.drain()
            except Exception:
                pass
    nudger = asyncio.create_task(nudge_enter())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            out, _ = await proc.communicate()
        except Exception:
            out = b''
        return 124, (out or b'').decode(errors='replace') + '\n命令超时'
    finally:
        nudger.cancel()
    return proc.returncode, out.decode(errors='replace')


async def resolve_target_to_ipv4(target):
    ip = extract_ipv4(target)
    if ip:
        return ip, None
    host = normalize_domain(target)
    if not host:
        raise RuntimeError('没有识别到 IPv4 或域名')
    def lookup():
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        seen = []
        for info in infos:
            addr = info[4][0]
            if addr not in seen:
                seen.append(addr)
        if not seen:
            raise RuntimeError('域名没有解析到 IPv4')
        return seen[0]
    return await asyncio.to_thread(lookup), host


def parse_bgp_png(stdout, target, outdir):
    m = re.search(r'^LATEST=(.+)$', stdout or '', re.M)
    if m:
        return Path(m.group(1).strip())
    return Path(outdir) / f'latest-{safe_target(target)}.png'


def is_bgp_temporary_no_path(output):
    text = str(output or '')
    return (
        'PLACEHOLDER' in text
        or 'NONE' in text
        or 'temporarily returned no path image' in text
        or 'prefix not visible in DFZ' in text
        or 'no usable BGP path image found' in text
        or 'no path data' in text
    )


def bgp_retry_message():
    return 'BGP 图暂时没取到，应该是 bgp.tools 偶发抽风，请再试一次。'


async def ensure_bgp_tool():
    if BGP_FETCH.exists():
        return
    bundled = Path('/app/tools/bgp_fetch.py')
    if bundled.exists():
        BGP_FETCH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled, BGP_FETCH)
        os.chmod(BGP_FETCH, 0o755)
        return
    raise RuntimeError('BGP 工具不存在。请使用项目 Dockerfile 构建镜像，或设置 BGP_FETCH 指向 bgp_fetch.py。')


async def ensure_ippure_tool():
    if not shutil.which('node'):
        raise RuntimeError('容器里没有 node，无法运行 IPPure。请使用项目 Dockerfile 构建镜像。')
    if not IPPURE_DOWNLOAD.exists():
        bundled = Path('/app/tools/download_ippure.js')
        if bundled.exists():
            IPPURE_DOWNLOAD.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, IPPURE_DOWNLOAD)
            os.chmod(IPPURE_DOWNLOAD, 0o755)
        else:
            raise RuntimeError('IPPure 工具不存在。请使用项目 Dockerfile 构建镜像，或设置 IPPURE_DOWNLOAD 指向 download_ippure.js。')
    try:
        code, _ = await run_subprocess(['node', '-e', 'require("playwright"); console.log("ok")'], timeout=20)
        if code != 0:
            raise RuntimeError('missing playwright')
    except Exception:
        code, out = await run_subprocess(['bash', '-lc', 'npm install -g playwright@1.59.1 && PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright} npx playwright install chromium'], timeout=600)
        if code != 0:
            raise RuntimeError('Playwright 自动安装失败：\n' + trim_log(out, 1000))


async def generate_bgp_png(ip):
    await ensure_bgp_tool()
    outdir = BGP_OUT_ROOT / f'bgp-{int(time.time())}-{os.getpid()}'
    outdir.mkdir(parents=True, exist_ok=True)
    code, out = await run_subprocess(['python3', str(BGP_FETCH), '--outdir', str(outdir), ip], timeout=120)
    if code != 0:
        if is_bgp_temporary_no_path(out):
            raise RuntimeError(bgp_retry_message())
        raise RuntimeError(trim_log(out, 1000) or f'BGP 生成失败：{code}')
    png = parse_bgp_png(out, ip, outdir)
    if not png.exists() or png.stat().st_size <= 0:
        raise RuntimeError('BGP 图片生成后未找到文件')
    return png


async def generate_ippure_png(ip):
    await ensure_ippure_tool()
    outdir = IPPURE_TMP_ROOT / f'ippure-{int(time.time())}-{os.getpid()}'
    outdir.mkdir(parents=True, exist_ok=True)
    code, out = await run_subprocess(['node', str(IPPURE_DOWNLOAD), '--ip', ip, '--outdir', str(outdir)], timeout=120)
    if code != 0:
        raise RuntimeError(trim_log(out, 1000) or f'IPPure 生成失败：{code}')
    candidates = [Path(line.strip()) for line in (out or '').splitlines() if line.strip().endswith('.png')]
    png = candidates[-1] if candidates else None
    if not png or not png.exists() or png.stat().st_size <= 0:
        raise RuntimeError('IPPure 图片生成后未找到文件')
    return png


async def send_png_and_cleanup(bot, chat_id, png, cleanup_dir=None):
    with Path(png).open('rb') as f:
        await bot.send_photo(chat_id, photo=f)
    if cleanup_dir:
        await asyncio.to_thread(shutil.rmtree, str(cleanup_dir), True)


async def run_bgp_task(bot, chat_id, s, jid):
    key = (server_id(s), 'bgp')
    try:
        ip = s.get('host')
        png = await generate_bgp_png(ip)
        saved = persist_result_file(s, 'bgp', png, '.png')
        JOBS[jid].update({'status': 'done', 'log': str(png), 'media_path': saved})
        await send_png_and_cleanup(bot, chat_id, png)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} BGP 图失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


async def run_ippure_task(bot, chat_id, s, jid):
    key = (server_id(s), 'ippure')
    try:
        ip = s.get('host')
        png = await generate_ippure_png(ip)
        saved = persist_result_file(s, 'ippure', png, '.png')
        JOBS[jid].update({'status': 'done', 'log': str(png), 'media_path': saved})
        await send_png_and_cleanup(bot, chat_id, png, Path(png).parent)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} IPPure 图失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


def ip_tools_markup(ip):
    row = []
    if tool_enabled('ippure'):
        row.append(InlineKeyboardButton('🧼 IPPure 图', callback_data=f'ippureip:{ip}'))
    if tool_enabled('bgp'):
        row.append(InlineKeyboardButton('🧭 BGP 图', callback_data=f'bgpip:{ip}'))
    if not row:
        row.append(InlineKeyboardButton('未启用 IP 图像工具', callback_data='noop'))
    return InlineKeyboardMarkup([row])


async def ensure_checkplace_renderer():
    if RENDER_CHECKPLACE.exists():
        return
    fallback = Path(__file__).resolve().parent / 'render_checkplace.py'
    if fallback.exists():
        return
    raise RuntimeError('Check.Place PNG 渲染器不存在。请确认镜像包含 /app/render_checkplace.py，或设置 RENDER_CHECKPLACE。')


async def render_checkplace_png(svg_url, out_png):
    await ensure_checkplace_renderer()
    renderer = RENDER_CHECKPLACE if RENDER_CHECKPLACE.exists() else Path(__file__).resolve().parent / 'render_checkplace.py'
    with tempfile.TemporaryDirectory() as td:
        svg_path = Path(td) / 'report.svg'
        def download():
            req = urllib.request.Request(
                svg_url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
                    'Accept': 'image/svg+xml,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': 'https://Report.Check.Place/',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                svg_path.write_bytes(r.read())
        await asyncio.to_thread(download)
        code, out = await run_subprocess(['python', str(renderer), str(svg_path), str(out_png)], timeout=90)
        if code != 0:
            raise RuntimeError(out[-1000:])


async def send_report_images(bot, chat_id, report_links, prefix):
    if not report_links:
        return []
    out_dir = Path('/tmp/guko-results')
    out_dir.mkdir(parents=True, exist_ok=True)
    sent = []
    for label, url in report_links:
        png = out_dir / f"{prefix}-{label}-{int(time.time())}.png"
        await render_checkplace_png(url, png)
        with png.open('rb') as f:
            await bot.send_photo(chat_id, photo=f)
        sent.append((label, url, png))
    return sent


async def run_ip_quality_task(bot, chat_id, s, jid):
    key = (server_id(s), 'ipq')
    try:
        remote = "export TERM=xterm-256color; cd /tmp && bash <(curl -Ls https://IP.Check.Place) -y"
        code, out, url = await run_until_report(ssh_args(s, remote, tty=False), timeout=900, env=ssh_env_for(s))
        JOBS[jid].update({'status': 'done' if code == 0 else 'failed', 'log': out})
        if not url:
            await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} IP质量没拿到报告链接。\n<pre>{safe(trim_log(out))}</pre>", parse_mode=ParseMode.HTML)
            return
        out_dir = Path('/tmp/guko-results')
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / f"ipq-{server_id(s)}-{int(time.time())}.png"
        try:
            await render_checkplace_png(url, png)
            saved = persist_result_file(s, 'ipq', png, '.png')
            JOBS[jid].update({'media_path': saved})
            with png.open('rb') as f:
                await bot.send_photo(chat_id, photo=f)
            await bot.send_message(chat_id, f"✅ {safe(s.get('name'))} IP质量完成\n{safe(url)}\n\n{script_command_html('ipq')}", parse_mode=ParseMode.HTML)
        except Exception as e:
            await bot.send_message(chat_id, f"✅ {safe(s.get('name'))} IP质量报告：\n{safe(url)}\n\n{script_command_html('ipq')}\n\n转 PNG 失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} IP质量任务失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


async def fetch_checkplace_svg_from_json(category, json_path):
    # Check.Place rejects replayed/masked JSON from nodequality.com exports, so this
    # is only a best-effort fallback for future unmasked JSON cases. Normal path
    # should parse the SVG printed by the live sub-script stdout.
    if not json_path.exists() or json_path.stat().st_size <= 0:
        return None
    script = (
        "json_file=$1; category=$2; "
        "curl -s -X POST https://upload.check.place "
        "-d type=$category --data-urlencode json@$json_file --data-urlencode content="
    )
    code, text = await run_subprocess(['bash', '-lc', script, 'bash', str(json_path), category], timeout=60)
    if code != 0:
        return None
    m = re.search(r'https://Report\.Check\.Place/[^\s<>"]+\.svg', text)
    return m.group(0) if m else None


def report_log_from_nodequality_url(text):
    token = None
    nq = nodequality_url(text)
    if nq:
        token = nq.rstrip('/').split('/')[-1]
    return f"https://api.nodequality.com/api/v1/record/{token}" if token else None


def nodequality_token(text):
    nq = nodequality_url(text)
    return nq.rstrip('/').split('/')[-1] if nq else None


async def upload_nodequality_result_from_remote(s):
    """Re-upload exactly like official NodeQuality.sh: base64(result.zip) as raw POST body."""
    remote = r'''set -e
z=""
for d in $(ls -td /root/.nodequality* /tmp/.nodequality* 2>/dev/null); do
  if [ -s "$d/result.zip" ]; then z="$d/result.zip"; break; fi
done
[ -n "$z" ] || exit 2
# Official NodeQuality.sh does: base64 result.zip | curl --data-binary @-
base64 "$z" | curl -fsS -X POST --data-binary @- https://api.nodequality.com/api/v1/record
'''
    code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=120, env=ssh_env_for(s))
    if code != 0:
        return None
    return nodequality_url(out)


async def recover_report_links_from_remote(s, selected):
    cats = []
    for label, cat, bit in [('硬件', 'hardware', 1), ('IP质量', 'ip', 2), ('网络', 'net', 4), ('回程', 'backroute', 8)]:
        if selected & bit:
            cats.append((label, cat))
    if not cats:
        return []
    remote = """td=$(mktemp -d /tmp/nqrecover.XXXXXX)
for d in $(ls -td /root/.nodequality* /tmp/.nodequality* 2>/dev/null); do
  r=\"$d/BenchOs/result\"
  [ -d \"$r\" ] || { [ -s \"$d/result.zip\" ] && unzip -oq \"$d/result.zip\" -d \"$td\" && r=\"$td\" || continue; }
  for pair in hardware:hardware_quality.json ip:ip_quality.json net:net_quality.json backroute:backroute_trace.json; do
    cat=${pair%%:*}; fn=${pair#*:}; p=\"$r/$fn\"
    [ -s \"$p\" ] && printf '%s\t%s\n' \"$cat\" \"$p\"
  done
done || true"""
    code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=30, env=ssh_env_for(s))
    if code != 0:
        return []
    mapping = {}
    for line in out.splitlines():
        if '\t' not in line:
            continue
        cat, path = line.split('\t', 1)
        mapping.setdefault(cat, path)
    recovered = []
    with tempfile.TemporaryDirectory() as td:
        for label, cat in cats:
            rp = mapping.get(cat)
            if not rp:
                continue
            local = Path(td) / f'{cat}.json'
            scp_args = scp_from_args(s, rp, str(local), inv=load_inventory())
            c, scp_out = await run_subprocess(scp_args, timeout=60, env=ssh_env_for(s))
            if c != 0:
                c, data = await run_subprocess(ssh_args(s, f"cat {shlex.quote(rp)}", tty=False), timeout=60, env=ssh_env_for(s))
                if c != 0:
                    continue
                local.write_text(data)
            try:
                u = await fetch_checkplace_svg_from_json(cat, local)
            except Exception:
                u = None
            if u:
                recovered.append((label, u))
    return recovered


def parse_geekbench5_scores(text):
    clean = strip_ansi(text or '')
    scores = {}
    patterns = {
        'single': r'Single-Core Score\s+(\d+)',
        'multi': r'Multi-Core Score\s+(\d+)',
        'url': r'https://browser\.geekbench\.com/v5/cpu/\d+',
    }
    for key, pat in patterns.items():
        m = re.search(pat, clean, re.I)
        if m:
            scores[key] = m.group(1) if key != 'url' else m.group(0)
    return scores


def gb5_result_image(s, scores, out_png):
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    W, H = 1080, 1350
    bg = (245, 247, 250)
    blue = (47, 111, 191)
    dark = (30, 41, 59)
    text = (31, 41, 55)
    muted = (100, 116, 139)
    line = (226, 232, 240)
    green = (22, 163, 74)
    try:
        font_title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 52)
        font_h1 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 42)
        font_h2 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 32)
        font_score = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 86)
        font_txt = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 28)
        font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
    except Exception:
        font_title = font_h1 = font_h2 = font_score = font_txt = font_small = ImageFont.load_default()

    def fit(draw, value, font, width):
        value = str(value or '-')
        if draw.textlength(value, font=font) <= width:
            return value
        ell = '…'
        while value and draw.textlength(value + ell, font=font) > width:
            value = value[:-1]
        return value + ell

    im = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 110], fill=(255, 255, 255))
    d.text((70, 32), 'Geekbench Browser', fill=blue, font=font_title)
    d.rounded_rectangle([70, 150, W-70, 425], radius=18, fill=(255, 255, 255), outline=line, width=2)
    d.text((110, 190), 'Geekbench 5 Score', fill=muted, font=font_txt)
    single = str(scores.get('single') or '-')
    multi = str(scores.get('multi') or '-')
    d.text((145, 255), single, fill=dark, font=font_score)
    d.text((145, 350), 'Single-Core Score', fill=muted, font=font_txt)
    d.line([W//2, 220, W//2, 390], fill=line, width=2)
    d.text((620, 255), multi, fill=dark, font=font_score)
    d.text((620, 350), 'Multi-Core Score', fill=muted, font=font_txt)

    d.rounded_rectangle([70, 465, W-70, 820], radius=18, fill=(255, 255, 255), outline=line, width=2)
    d.text((110, 505), str(s.get('name') or s.get('host') or 'Server'), fill=text, font=font_h1)
    rows = [
        ('Operating System', f"{s.get('platform') or '-'} {s.get('platform_version') or ''}".strip()),
        ('Model', str(s.get('name') or '-')),
        ('Processor', str(s.get('cpu') or '-')),
        ('Memory', fmt_bytes(s.get('mem_total'))),
        ('IPv4', str(s.get('host') or '-')),
    ]
    y = 585
    for k, v in rows:
        d.text((110, y), k, fill=muted, font=font_small)
        d.text((390, y), fit(d, v, font_small, 560), fill=text, font=font_small)
        y += 42

    d.rounded_rectangle([70, 860, W-70, 1180], radius=18, fill=(255, 255, 255), outline=line, width=2)
    d.text((110, 900), 'Benchmark Summary', fill=text, font=font_h2)
    d.text((110, 965), 'Single-Core', fill=muted, font=font_txt)
    d.rounded_rectangle([330, 970, 900, 1000], radius=15, fill=(219, 234, 254))
    try:
        sw = max(8, min(570, int(single) / max(int(multi or 1), int(single), 1) * 570))
    except Exception:
        sw = 20
    d.rounded_rectangle([330, 970, 330 + sw, 1000], radius=15, fill=blue)
    d.text((920, 960), single, fill=text, font=font_txt, anchor='ra')
    d.text((110, 1045), 'Multi-Core', fill=muted, font=font_txt)
    d.rounded_rectangle([330, 1050, 900, 1080], radius=15, fill=(220, 252, 231))
    d.rounded_rectangle([330, 1050, 900, 1080], radius=15, fill=green)
    d.text((920, 1040), multi, fill=text, font=font_txt, anchor='ra')
    if scores.get('url'):
        d.text((110, 1125), fit(d, scores['url'], font_small, 850), fill=blue, font=font_small)

    d.text((70, 1255), 'Generated by GUKO · Geekbench 5', fill=muted, font=font_small)
    im.save(out_png, quality=95)
    return out_png

async def run_gb5_task(bot, chat_id, s, jid):
    key = (server_id(s), 'gb5')
    try:
        remote = (
            "set -e; export TERM=xterm-256color; cd /root; "
            "swapfile=; cleanup(){ if [ -n \"$swapfile\" ]; then swapoff $swapfile 2>/dev/null || true; rm -f $swapfile; fi; }; trap cleanup EXIT; "
            "mem_kb=$(awk '/MemTotal:/ {print $2}' /proc/meminfo); "
            "if [ ${mem_kb:-0} -lt 900000 ]; then "
            "swapfile=/root/geekbench5.swap; rm -f $swapfile; "
            "(fallocate -l 2G $swapfile 2>/dev/null || dd if=/dev/zero of=$swapfile bs=1M count=2048 status=none); "
            "chmod 600 $swapfile; mkswap $swapfile >/dev/null; swapon $swapfile; "
            "fi; "
            "d=/root/Geekbench-" + GB5_VERSION + "-Linux; "
            "if [ ! -x $d/geekbench5 ]; then "
            "curl -fsSL " + shlex.quote(GB5_URL) + " -o /tmp/geekbench5.tar.gz; "
            "tar -xzf /tmp/geekbench5.tar.gz -C /root; "
            "fi; "
            "$d/geekbench5 --upload 2>&1"
        )
        code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=3600, env=ssh_env_for(s))
        scores = parse_geekbench5_scores(out)
        gb_urls = [u for u in extract_urls(out) if 'browser.geekbench.com/v5/cpu/' in u]
        if gb_urls:
            scores['url'] = gb_urls[-1]
        JOBS[jid].update({'status': 'done' if code == 0 and scores.get('url') else 'failed', 'log': out})
        if code != 0 or not scores.get('url'):
            await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} GB5 没拿到结果链接。\n<pre>{safe(trim_log(out))}</pre>", parse_mode=ParseMode.HTML)
            return
        img = gb5_result_image(s, scores, RESULTS_DIR / str(server_id(s)) / 'gb5' / 'latest.jpg')
        JOBS[jid].update({'media_path': str(img)})
        with img.open('rb') as f:
            await bot.send_photo(chat_id, photo=f)
        await bot.send_message(chat_id, f"✅ {safe(s.get('name'))} GB5 完成\n{safe(scores['url'])}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} GB5任务失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)

def stream_clean_output(text):
    clean = strip_ansi(text or '')
    clean = clean.replace('\r', '\n')
    lines = []
    for raw in clean.splitlines():
        line = raw.strip()
        if not line:
            continue
        if set(line) <= set('-_=* '):
            continue
        skip_needles = [
            '请选择检测项目', '请输入正确数字', '检测脚本当天运行次数', '本次测试已结束',
            '感谢使用此脚本', '广告招租', '请联系', 'Github', 'YouTube', '支持系统',
            'RegionRestrictionCheck', 'Streaming Media Unlock Test', '正在下载', 'Downloading',
            'Number of Script Runs', 'Testing Done', 'Press ENTER', 'Input Number', '请输入',
        ]
        if any(x.lower() in line.lower() for x in skip_needles):
            continue
        lines.append(line)
    return lines


def parse_stream_results(text):
    lines = stream_clean_output(text)
    net_type = ''
    section = ''
    results = []
    network = []
    for line in lines:
        l = line.strip()
        if '正在测试 IPv4' in l or 'Checking Results Under IPv4' in l:
            net_type = 'IPv4'
            continue
        if '正在测试 IPv6' in l or 'Checking Results Under IPv6' in l:
            net_type = 'IPv6'
            continue
        if '正在测试默认网络' in l or 'Checking Results Under Default' in l:
            net_type = '默认网络'
            continue
        if '您的网络为:' in l or 'Your Network Provider:' in l:
            network.append(l.replace('**', '').strip())
            continue
        # Original script section lines:
        # ============[ Multination ]============
        #  ---Game---
        msec = re.search(r'\[\s*([^\]]+?)\s*\]', l)
        if msec and set(l.replace(msec.group(0), '')) <= set('=-_ '):
            section = msec.group(1).strip()
            continue
        msub = re.match(r'^-+\s*([^\-]+?)\s*-+$', l)
        if msub:
            section = msub.group(1).strip()
            continue
        if set(l) <= set('= '):
            continue
        m = re.match(r'(.+?):\s*(Yes|No|Failed|Originals Only|IPv6 Is Not Currently Supported|Available For .* Soon|即将推出|Unsupported|N/A)(.*)$', l, re.I)
        if not m:
            continue
        name = m.group(1).strip()
        status = m.group(2).strip()
        extra = m.group(3).strip()
        sec = section or net_type or '-'
        results.append({'section': sec, 'net': net_type, 'name': name, 'status': status, 'extra': extra})
    return network, results


def stream_status_icon(status, extra=''):
    status_l = str(status or '').lower()
    t = f'{status} {extra}'.lower()
    if 'only available' in t or 'only avaliable' in t or 'mobile app' in t:
        return '🟡'
    if status_l == 'yes' or 'region:' in t or 'available for' in t:
        return '✅'
    if status_l == 'no' or 'not available' in t or 'blocked' in t:
        return '❌'
    if 'originals only' in t:
        return '🟡'
    if 'ipv6 is not currently supported' in t or 'unsupported' in t:
        return '➖'
    return '⚠️'


def format_stream_summary(s, out, proto, region_label, region_id):
    network, results = parse_stream_results(out)
    groups = OrderedDict()
    for r in results:
        groups.setdefault(r['section'], []).append(r)
    total = len(results)
    yes = sum(1 for r in results if stream_status_icon(r['status'], r.get('extra')) == '✅')
    no = sum(1 for r in results if stream_status_icon(r['status'], r.get('extra')) == '❌')
    warn = max(0, total - yes - no)
    head = [
        f'🎬 <b>{safe(s.get("name"))} 流媒体检测完成</b>',
        f'协议：<b>{safe(proto)}</b> · 地区：<b>{safe(region_label)}</b>',
        f'检测时间：<code>{safe(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</code>',
    ]
    if network:
        head.append(safe(network[-1]))
    if total:
        head.append(f'结果：Yes {yes} / No {no} / Error {warn}')
    parts = ['\n'.join(head)]
    if not results:
        parts.append('没解析到结构化结果，末尾日志：\n<pre>' + safe(trim_log(strip_ansi(out), 2600)) + '</pre>')
        return '\n\n'.join(parts)
    for sec, items in groups.items():
        lines = [f'<b>{safe(sec)}</b>']
        for r in items[:80]:
            extra = (' ' + r.get('extra', '')) if r.get('extra') else ''
            lines.append(f'{safe(r["name"])}：<code>{safe(r["status"] + extra)}</code>')
        parts.append('\n'.join(lines))
    text = '\n\n'.join(parts)
    return text[:3900] + ('\n\n…结果较长，已截断。' if len(text) > 3900 else '')


def stream_status_color(status, extra=''):
    icon = stream_status_icon(status, extra)
    if icon == '✅':
        return (22, 163, 74)
    if icon == '❌':
        return (220, 38, 38)
    return (202, 138, 4)


def stream_status_label(status, extra=''):
    text = (str(status or '') + (' ' + str(extra).strip() if extra else '')).strip()
    if not text:
        return 'Error'
    if text.lower().startswith('failed'):
        return re.sub(r'^Failed', 'Error', text, flags=re.I)
    return text


def load_font(candidates, size):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def stream_result_image(s, out, proto, region_label, region_id, out_png):
    network, results = parse_stream_results(out)
    if not results:
        return None
    groups = OrderedDict()
    for r in results:
        groups.setdefault(r['section'], []).append(r)
    total = len(results)
    yes = sum(1 for r in results if stream_status_icon(r['status'], r.get('extra')) == '✅')
    no = sum(1 for r in results if stream_status_icon(r['status'], r.get('extra')) == '❌')
    warn = max(0, total - yes - no)

    font_cjk = [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    font_cjk_bold = [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]
    mono_fonts = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]

    W = 1120
    pad = 50
    row_h = 32
    title_h = 190
    section_h = 42
    footer_h = 78
    max_rows = sum(len(v) for v in groups.values())
    H = title_h + footer_h + len(groups) * section_h + max_rows * row_h + 24
    H = max(500, H + 82)

    bg = (247, 249, 252)
    card = (255, 255, 255)
    line = (226, 232, 240)
    line_soft = (241, 245, 249)
    dark = (30, 41, 59)
    muted = (100, 116, 139)
    blue = (37, 99, 235)

    im = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(im)
    title_font = load_font(font_cjk_bold, 36)
    meta_font = load_font(font_cjk, 21)
    mono_font = load_font(mono_fonts, 23)
    mono_small = load_font(mono_fonts, 20)
    section_font = load_font(mono_fonts, 24)
    status_font = load_font(font_cjk_bold, 23)
    small_font = load_font(font_cjk, 20)

    d.rounded_rectangle([26, 22, W-26, H-24], radius=24, fill=card, outline=line, width=2)

    server_name = str(s.get('name') or s.get('host') or 'Server')
    d.text((pad, 44), f'{server_name} 流媒体解锁测试', fill=dark, font=title_font)
    d.text((W-pad, 52), source_repo('stream'), fill=muted, font=meta_font, anchor='ra')
    d.text((pad, 92), f'{proto} · {region_label} · {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', fill=muted, font=meta_font)
    d.text((W-pad, 92), f'Yes {yes}   No {no}   Error {warn}', fill=blue, font=meta_font, anchor='ra')
    if network:
        nt = network[-1].replace('**', '').strip()
        if len(nt) > 90:
            nt = nt[:87] + '…'
        d.text((pad, 126), nt, fill=muted, font=small_font)
    d.line([pad, title_h-18, W-pad, title_h-18], fill=line, width=2)

    y = title_h + 4
    table_left = pad
    table_right = W - pad
    status_x = table_right
    # mirror the script's tabbed layout: service name column then a fixed result column
    name_col_width = 500

    def fit_text(value, font, max_width):
        value = str(value or '-')
        if d.textlength(value, font=font) <= max_width:
            return value
        ell = '…'
        while value and d.textlength(value + ell, font=font) > max_width:
            value = value[:-1]
        return value + ell

    def centered_rule(label):
        text = f'[ {label} ]'
        tw = d.textlength(text, font=section_font)
        dash_w = d.textlength('=', font=section_font)
        left_count = max(2, int((table_right - table_left - tw) / 2 / dash_w))
        right_count = left_count
        return '=' * left_count + text + '=' * right_count

    for sec, items in groups.items():
        rule = fit_text(centered_rule(str(sec or '-')), section_font, table_right - table_left)
        d.text(((W - d.textlength(rule, font=section_font)) / 2, y), rule, fill=blue, font=section_font)
        y += section_h
        for r in items:
            name = fit_text((str(r.get('name') or '-').rstrip(':') + ':'), mono_font, name_col_width)
            status = fit_text(stream_status_label(r.get('status'), r.get('extra')), status_font, 470)
            color = stream_status_color(r.get('status'), r.get('extra'))
            d.text((table_left, y), name, fill=dark, font=mono_font)
            d.text((status_x, y), status, fill=color, font=status_font, anchor='ra')
            y += row_h
        end_rule = '=' * max(8, int((table_right - table_left) / max(d.textlength('=', font=mono_small), 1)))
        d.text((table_left, y), fit_text(end_rule, mono_small, table_right - table_left), fill=line, font=mono_small)
        y += 16

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png, quality=95)
    return out_png


async def remote_has_ipv4(s):
    remote = "curl -4fsS --max-time 8 https://api.ipify.org >/dev/null"
    code, _out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=15, env=ssh_env_for(s))
    return code == 0



def ansi_to_spans(text):
    palette = {
        30: (51, 65, 85), 90: (100, 116, 139),
        31: (220, 38, 38), 91: (220, 38, 38),
        32: (22, 163, 74), 92: (22, 163, 74),
        33: (202, 138, 4), 93: (202, 138, 4),
        34: (37, 99, 235), 94: (37, 99, 235),
        35: (192, 38, 211), 95: (192, 38, 211),
        36: (8, 145, 178), 96: (8, 145, 178),
        37: (30, 41, 59), 97: (15, 23, 42),
    }
    default = (30, 41, 59)
    spans = []
    color = default
    bold = False
    i = 0
    buf = ''
    while i < len(text):
        if text[i] == '\x1b' and i + 1 < len(text) and text[i + 1] == '[':
            m = re.match(r'\x1b\[([0-9;]*)m', text[i:])
            if m:
                if buf:
                    spans.append((buf, color, bold))
                    buf = ''
                codes = [int(x) if x else 0 for x in m.group(1).split(';')]
                if not codes:
                    codes = [0]
                for code in codes:
                    if code == 0:
                        color = default
                        bold = False
                    elif code == 1:
                        bold = True
                    elif code == 22:
                        bold = False
                    elif code in palette:
                        color = palette[code]
                i += len(m.group(0))
                continue
        buf += text[i]
        i += 1
    if buf:
        spans.append((buf, color, bold))
    return spans



def terminal_char_width(ch):
    o = ord(ch)
    if o == 0:
        return 0
    if o < 32 or 0x7f <= o < 0xa0:
        return 0
    # CJK / fullwidth ranges; enough for NextTrace Chinese geo/ISP labels.
    if (
        0x1100 <= o <= 0x115f or 0x2e80 <= o <= 0xa4cf or
        0xac00 <= o <= 0xd7a3 or 0xf900 <= o <= 0xfaff or
        0xfe10 <= o <= 0xfe19 or 0xfe30 <= o <= 0xfe6f or
        0xff00 <= o <= 0xff60 or 0xffe0 <= o <= 0xffe6
    ):
        return 2
    return 1


def terminal_text_width(text):
    return sum(terminal_char_width(ch) for ch in strip_ansi(text or ''))


def draw_terminal_spans(draw, x0, y, spans, *, cell_w, ascii_font, ascii_bold, cjk_font, cjk_bold):
    col = 0
    for text, color, bold in spans:
        for ch in text:
            w = terminal_char_width(ch)
            if w <= 0:
                continue
            is_cjk = w == 2
            font = (cjk_bold if bold else cjk_font) if is_cjk else (ascii_bold if bold else ascii_font)
            # Draw every glyph onto a fixed terminal grid. This keeps NextTrace's original column layout
            # while still letting CJK render with a CJK font.
            draw.text((x0 + col * cell_w, y), ch, fill=color, font=font)
            col += w

def render_nexttrace_image(s, target, out, code, out_png):
    raw = (out or '').replace('\r\n', '\n').replace('\r', '\n')
    raw = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', lambda m: m.group(0) if m.group(0).endswith('m') else '', raw)
    lines = []
    for line in raw.split('\n'):
        if 'Generated by' in strip_ansi(line) or 'MapTrace URL:' in strip_ansi(line):
            continue
        lines.append(line.rstrip())
    while lines and not strip_ansi(lines[0]).strip():
        lines.pop(0)
    while lines and not strip_ansi(lines[-1]).strip():
        lines.pop()
    if not lines:
        lines = ['无输出']

    title_font = load_font([
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ], 42)
    mono_font = load_font([
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
    ], 24)
    mono_bold = load_font([
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
    ], 24)
    cjk_font = load_font([
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ], 24)
    cjk_bold = load_font([
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    ], 24)

    dummy = Image.new('RGB', (1, 1))
    d0 = ImageDraw.Draw(dummy)
    char_w = max(13, int(d0.textlength('M', font=mono_font)))
    line_h = 34
    max_cols = min(150, max(78, max((terminal_text_width(x) for x in lines), default=78)))
    W = max(1180, min(2100, 112 + max_cols * char_w))
    H = 150 + max(1, len(lines)) * line_h + 42

    bg = (248, 250, 252)
    panel = (255, 255, 255)
    border = (203, 213, 225)
    fg = (15, 23, 42)
    muted = (100, 116, 139)
    accent = (2, 132, 199)
    warn = (202, 138, 4)
    err = (220, 38, 38)

    im = Image.new('RGB', (W, H), bg)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((28, 24, W - 28, H - 28), radius=24, fill=panel, outline=border, width=2)
    d.rounded_rectangle((48, 44, W - 48, 118), radius=18, fill=(241, 245, 249), outline=(203, 213, 225), width=1)
    title = 'NextTrace'
    title_box = d.textbbox((0, 0), title, font=title_font)
    title_h = title_box[3] - title_box[1]
    title_y = 44 + (118 - 44 - title_h) / 2 - title_box[1]
    d.text((72, title_y), title, fill=accent, font=title_font)
    source_name = source_repo('nexttrace')
    source_w = d.textlength(source_name, font=mono_font)
    d.text((W - 72 - source_w, 72), source_name, fill=muted, font=mono_font)
    if code != 0:
        d.text((W - 260, 96), f'退出码 {code}', fill=warn, font=mono_font)
    d.line((54, 126, W - 54, 126), fill=border, width=1)

    y = 148
    x0 = 68
    for line in lines:
        x = x0
        plain = strip_ansi(line)
        if plain.startswith('traceroute to') or 'hops max' in plain:
            d.rounded_rectangle((54, y - 4, W - 54, y + line_h - 2), radius=8, fill=(248, 250, 252), outline=(226, 232, 240), width=1)
        draw_terminal_spans(
            d, x0, y, ansi_to_spans(line),
            cell_w=char_w,
            ascii_font=mono_font,
            ascii_bold=mono_bold,
            cjk_font=cjk_font,
            cjk_bold=cjk_bold,
        )
        y += line_h

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_png, quality=95)
    return out_png

def nexttrace_prompt_text(s):
    return (
        f'🛣 <b>{safe(s.get("name"))} NextTrace</b>\n\n'
        '请直接发送要追踪的 <b>IP 或域名</b>。\n'
        '例如：<code>1.1.1.1</code> 或 <code>cloudflare.com</code>\n\n'
        '也可以用命令：<code>/nexttrace 服务器 目标IP或域名</code>'
    )


def format_nexttrace_output(s, target, out, code):
    clean = strip_ansi(out or '').replace('\r', '')
    lines = []
    for raw in clean.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        skip = [
            'NextTrace', 'nali', 'MapTrace', 'IP Geo Data Provider',
            'traceroute to', 'Generated by', '请勿用于商业用途',
        ]
        if any(x.lower() in line.lower() for x in skip):
            continue
        lines.append(line)
    body = '\n'.join(lines).strip() or clean.strip() or '无输出'
    body = trim_log(body, 3200)
    title = f'🛣 <b>{safe(s.get("name"))} NextTrace</b> → <code>{safe(target)}</code>'
    if code != 0:
        title = '⚠️ ' + title + f'\n退出码：<code>{safe(code)}</code>'
    return title + '\n\n<pre>' + safe(body) + '</pre>'


async def run_nexttrace_task(bot, chat_id, s, jid, target='1.1.1.1'):
    key = (server_id(s), 'nexttrace')
    try:
        qt = shlex.quote(str(target))
        trace_cmd = 'nexttrace ' + qt
        remote = (
            "set -e; export TERM=xterm-256color COLORTERM=truecolor; export CLICOLOR_FORCE=1 FORCE_COLOR=1; "
            "if ! command -v nexttrace >/dev/null 2>&1; then "
            "  curl -sL https://nxtrace.org/nt | bash >/tmp/nexttrace-install.log 2>&1 || "
            "  curl -Ls https://raw.githubusercontent.com/nxtrace/NTrace-core/main/nt_install.sh | bash >/tmp/nexttrace-install.log 2>&1; "
            "fi; "
            "script -qfec " + shlex.quote(trace_cmd) + " /dev/null 2>&1"
        )
        code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=300, env=ssh_env_for(s))
        JOBS[jid].update({'status': 'done' if code == 0 else 'failed', 'log': out, 'target': target})
        out_dir = Path('/tmp/guko-results')
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / f"nexttrace-{server_id(s)}-{safe_target(target)}-{int(time.time())}.jpg"
        try:
            img = render_nexttrace_image(s, target, out, code, png)
        except Exception:
            img = None
        if img:
            saved = persist_result_file(s, 'nexttrace', img, '.jpg')
            JOBS[jid].update({'media_path': saved})
            with img.open('rb') as f:
                await bot.send_photo(chat_id, photo=f)
            await bot.send_message(chat_id, script_command_text('nexttrace', target=target))
            if code != 0:
                await bot.send_message(chat_id, '提示：NextTrace 退出码不为 0，图片是已抓到的部分结果。')
        else:
            await bot.send_message(chat_id, format_nexttrace_output(s, target, out, code), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e), 'target': target})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} NextTrace 失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


async def run_stream_task(bot, chat_id, s, jid):
    key = (server_id(s), 'stream')
    try:
        region_id, region_label = stream_region_for_server(s)
        use_v4 = await remote_has_ipv4(s)
        proto_arg = '-M 4' if use_v4 else '-M 6'
        proto_text = 'IPv4' if use_v4 else 'IPv6（无 IPv4，自动切换）'
        remote = (
            "export TERM=xterm-256color; cd /tmp; "
            "script=$(mktemp /tmp/stream-unlock.XXXXXX.sh); "
            "curl -4LfsS --max-time 30 check.unlock.media -o $script || "
            "curl -4LfsS --max-time 30 http://check.unlock.media -o $script || "
            "curl -4LfsS --max-time 30 https://raw.githubusercontent.com/lmc999/RegionRestrictionCheck/main/check.sh -o $script || "
            "curl -6LfsS --max-time 30 https://raw.githubusercontent.com/lmc999/RegionRestrictionCheck/main/check.sh -o $script; "
            "bash $script " + proto_arg + " -R " + shlex.quote(region_id) + " 2>&1"
        )
        code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=1800, env=ssh_env_for(s))
        JOBS[jid].update({'status': 'done' if code == 0 else 'failed', 'log': out, 'proto': proto_text, 'region': region_label})
        out_dir = Path('/tmp/guko-results')
        out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / f"stream-{server_id(s)}-{int(time.time())}.jpg"
        try:
            img = stream_result_image(s, out, proto_text, region_label, region_id, png)
        except Exception:
            img = None
        if img:
            saved = persist_result_file(s, 'stream', img, '.jpg')
            JOBS[jid].update({'media_path': saved})
            with img.open('rb') as f:
                await bot.send_photo(chat_id, photo=f)
            await bot.send_message(chat_id, script_command_text('stream', proto_arg=proto_arg, region_id=region_id))
            if code != 0:
                await bot.send_message(chat_id, '提示：脚本退出码不为 0，图片是已抓到的部分结果。')
        else:
            msg = format_stream_summary(s, out, proto_text, region_label, region_id)
            if code != 0:
                msg = '⚠️ 脚本退出码不为 0，但下面是已抓到的输出：\n\n' + msg
            await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} 流媒体检测失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


async def run_nq_task(bot, chat_id, s, jid, mask=NQ_ALL_MASK, ip_mode='4'):
    key = (server_id(s), 'nq')
    try:
        selected_text = nq_selected_text(mask)
        ip_text = nq_ip_mode_text(ip_mode)
        answers = nq_answer_script(mask)
        ipv_arg = nq_remote_ipv_arg(s, ip_mode)
        remote = "export TERM=xterm-256color; cd /root && script=$(mktemp /root/nodequality.XXXXXX.sh); curl -sL https://run.NodeQuality.com > $script; sed -i 's#rm -rf \"${work_dir}\"/#: # rm -rf \"${work_dir}\"/#' $script; printf %b " + shlex.quote(answers) + " | bash $script " + ipv_arg
        code, out = await run_subprocess(ssh_args(s, remote, tty=False), timeout=7200, env=ssh_env_for(s))
        JOBS[jid].update({'status': 'running', 'log': out, 'selected': selected_text, 'ip_mode': ip_text})
        nq = nodequality_url(out)
        gb_urls = geekbench_urls(out)
        fixed_nq = None
        if nq:
            try:
                fixed_nq = await upload_nodequality_result_from_remote(s)
            except Exception:
                fixed_nq = None
            if fixed_nq:
                nq = fixed_nq
        report_links = []
        for label, cat, bit in [('硬件', 'hardware', 1), ('IP质量', 'ip', 2), ('网络', 'net', 4), ('回程', 'backroute', 8)]:
            if not (mask & bit):
                continue
            urls = all_report_urls(out, cat)
            if urls:
                report_links.append((label, urls[-1]))
        if not report_links:
            report_links = await recover_report_links_from_remote(s, mask)
        image_ok = False
        image_error = ''
        if report_links:
            try:
                sent = await send_report_images(bot, chat_id, report_links, f"nq-{server_id(s)}")
                media_paths = []
                for label, url, png in sent:
                    saved = persist_result_file(s, 'nq', png, f'-{label}.png')
                    if saved:
                        media_paths.append(saved)
                if media_paths:
                    JOBS[jid].update({'media_paths': media_paths})
                image_ok = True
            except Exception as e:
                image_error = str(e)
        final_ok = bool(nq or report_links or image_ok)
        JOBS[jid].update({'status': 'done' if final_ok else 'failed'})
        msg = f"✅ {safe(s.get('name'))} NodeQuality 完成：{safe(selected_text)}；{safe(ip_text)}" if final_ok else f"❌ {safe(s.get('name'))} NodeQuality 失败：{safe(selected_text)}；{safe(ip_text)}"
        if nq:
            msg += f"\n\nNodeQuality:\n{safe(nq)}"
        if gb_urls:
            msg += "\n\nGeekbench:\n" + "\n".join(safe(u) for u in gb_urls)
            if not image_ok:
                token = nodequality_token(nq)
                api = f"https://api.nodequality.com/api/v1/record/{token}" if token else None
                if api:
                    msg += f"\n原始报告接口：\n{safe(api)}"
        if not image_ok and report_links:
            msg += "\n\n分项报告：\n" + "\n".join(f"- {safe(label)}: {safe(url)}" for label, url in report_links)
            if image_error:
                msg += f"\n\n转 PNG 失败：<code>{safe(image_error)}</code>"
        if not nq and not report_links:
            msg += f"\n\n没解析到结果链接，末尾日志：\n<pre>{safe(trim_log(out))}</pre>"
        msg += f"\n\n{script_command_html('nq', selected=selected_text, ip_mode=ip_text)}"
        await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        JOBS[jid].update({'status': 'failed', 'log': repr(e)})
        await bot.send_message(chat_id, f"❌ {safe(s.get('name'))} NQ任务失败：<code>{safe(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        finish_job(jid, key)


async def send_history_result(bot, chat_id, s, kind):
    item = history_item_for(s, kind)
    if not item:
        await bot.send_message(chat_id, f'暂无 {safe(KIND_NAME.get(kind, kind))} 历史。', parse_mode=ParseMode.HTML)
        return False
    media_paths = []
    for x in item.get('media_paths') or []:
        p = Path(x)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            media_paths.append(p)
    for media in media_paths:
        with media.open('rb') as f:
            await bot.send_photo(chat_id, photo=f)
    if kind == 'nq':
        urls = item.get('urls') or []
        nq = next((u for u in urls if 'nodequality.com/r/' in u), None)
        gb_urls = [u for u in urls if 'browser.geekbench.com/' in u]
        report_urls = [u for u in urls if 'Report.Check.Place/' in u]
        selected = item.get('selected') or '-'
        ip_mode = item.get('ip_mode') or '-'
        msg = f"✅ {safe(s.get('name'))} NodeQuality 完成：{safe(selected)}；{safe(ip_mode)}"
        if nq:
            msg += f"\n\nNodeQuality:\n{safe(nq)}"
        if gb_urls:
            msg += "\n\nGeekbench:\n" + "\n".join(safe(u) for u in gb_urls)
        if not media_paths and report_urls:
            msg += "\n\n分项报告：\n" + "\n".join(f"- {safe(u)}" for u in report_urls)
        msg += f"\n\n{script_command_html('nq', selected=selected, ip_mode=ip_mode)}"
        await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return True
    if media_paths:
        return True
    await bot.send_message(chat_id, history_detail_text(s, kind), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return True


async def send_or_edit(update: Update, text, markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await send_or_edit(update, menu_text(), main_menu_markup())


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(f'GUKO v{GUKO_VERSION}')


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await send_or_edit(update, menu_text(), main_menu_markup())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await send_or_edit(update, menu_text(), main_menu_markup())


async def addserver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    clear_add_session(update.effective_chat.id)
    await update.message.reply_text(add_help_text(), parse_mode=ParseMode.HTML, reply_markup=add_start_markup())


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text('用法：/info <名字/IP/ID/别名>')
        return
    s = find_server(' '.join(context.args), load_inventory().get('servers', []))
    if not s:
        await update.message.reply_text('没找到这台。')
        return
    await update.message.reply_text(server_detail_text(s), parse_mode=ParseMode.HTML, reply_markup=server_markup(s))


async def export_config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    data = redact_inventory(load_inventory())
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    path = TMP_DIR / f'guko-export-redacted-{int(time.time())}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    with path.open('rb') as f:
        await update.message.reply_document(document=f, filename='guko-servers-redacted.json', caption='已导出脱敏配置（密码已隐藏）。')


async def testall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    servers = [s for s in load_inventory().get('servers', []) if s.get('host')]
    if not servers:
        await update.message.reply_text('当前没有服务器。')
        return
    msg = await update.message.reply_text(f'🧪 开始批量测试 SSH：{len(servers)} 台，并发 3。')
    sem = asyncio.Semaphore(3)
    results = []
    async def one(s):
        async with sem:
            ok, out = await test_server_login(s, timeout=12)
            results.append((s, ok, out))
    await asyncio.gather(*(one(s) for s in servers))
    lines = []
    for s, ok, out in results:
        mark = '✅' if ok else '❌'
        cfg = ssh_config(s)
        lines.append(f'{mark} {s.get("name")}  {cfg.get("user")}@{cfg.get("host")}:{cfg.get("port")}')
        if not ok:
            lines.append('   ' + strip_ansi(out).splitlines()[-1][:120] if out else '   无输出')
    await msg.edit_text('<pre>' + safe('\n'.join(lines)[-3500:]) + '</pre>', parse_mode=ParseMode.HTML)


async def testssh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update): return
    if not context.args:
        await update.message.reply_text('用法：/testssh <名字/IP/ID/别名>')
        return
    s = find_server(' '.join(context.args), load_inventory().get('servers', []))
    if not s:
        await update.message.reply_text('没找到这台服务器。')
        return
    msg = await update.message.reply_text(f'🧪 正在测试 {safe(s.get("name"))} SSH…', parse_mode=ParseMode.HTML)
    ok, out = await test_server_login(s)
    await msg.edit_text(('✅ SSH 登录成功：' if ok else '⚠️ SSH 登录失败：') + '<pre>' + safe(out[-1200:]) + '</pre>', parse_mode=ParseMode.HTML)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text('用法：/history <名字/IP/ID/别名>')
        return
    s = find_server(' '.join(context.args), load_inventory().get('servers', []))
    if not s:
        await update.message.reply_text('没找到这台服务器。')
        return
    await update.message.reply_text(history_text(s), parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=history_markup(s))


async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not JOBS:
        await update.message.reply_text('暂无后台任务。')
        return
    lines = []
    for jid, j in list(JOBS.items())[-10:]:
        lines.append(f"{jid}: {j.get('server')} {j.get('kind')} {j.get('status')}")
    await update.message.reply_text('<pre>' + safe('\n'.join(lines)) + '</pre>', parse_mode=ParseMode.HTML)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    chat_id = update.effective_chat.id if update.effective_chat else None
    sess = ADD_SESSIONS.get(chat_id) if chat_id is not None else None
    if not sess or sess.get('step') not in ('single_key_text', 'bulk_shared_key_text'):
        await update.message.reply_text('收到文件了，但当前没有等待密钥上传。请先点“添加服务器”。')
        return
    doc = update.message.document
    if not doc:
        return
    if doc.file_size and doc.file_size > 128 * 1024:
        await update.message.reply_text('密钥文件太大了，不像 SSH 私钥。')
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    try:
        key_path = save_private_key(chat_id, bytes(data), doc.file_name or 'telegram-key')
    except Exception as e:
        await update.message.reply_text(f'密钥识别失败：{safe(e)}', parse_mode=ParseMode.HTML)
        return
    if sess.get('step') == 'single_key_text':
        add_session(chat_id, key=key_path, auth_kind='key')
        await finish_single_add(update, context, ADD_SESSIONS[chat_id])
    else:
        add_session(chat_id, auth_mode='key', shared_auth=key_path, step='bulk_lines')
        await update.message.reply_text('密钥已保存。现在发送服务器列表，每行一台。\n\n格式：<code>名称 IP 用户</code>', parse_mode=ParseMode.HTML)


async def fallback_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    text = update.message.text or ''
    chat_id = update.effective_chat.id if update.effective_chat else None
    sess = ADD_SESSIONS.get(chat_id) if chat_id is not None else None
    if sess:
        step = sess.get('step')
        if step == 'single_basic':
            parts = shlex.split(text)
            if len(parts) < 2:
                await update.message.reply_text('格式：<code>名称 IP [端口] [用户]</code>\n例：<code>hk-01 1.2.3.4 22 root</code>', parse_mode=ParseMode.HTML)
                return
            host, embedded_port = parse_host_port(parts[1])
            if not is_valid_hostname(host):
                await update.message.reply_text('IP/域名看起来不对，重新发一次。')
                return
            port = embedded_port
            user = 'root'
            if len(parts) >= 3 and parts[2].isdigit():
                port = int(parts[2])
                if len(parts) >= 4:
                    user = parts[3]
            elif len(parts) >= 3:
                user = parts[2]
            add_session(chat_id, step='single_auth', name=parts[0], host=host, port=port or 22, user=user)
            await update.message.reply_text('选择这台服务器的登录方式：', reply_markup=add_auth_markup())
            return
        if step == 'single_password':
            add_session(chat_id, password=text.strip(), auth_kind='password')
            await finish_single_add(update, context, ADD_SESSIONS[chat_id])
            return
        if step == 'single_key_path':
            key_path = os.path.expanduser(text.strip())
            if not key_path:
                await update.message.reply_text('密钥路径不能为空。')
                return
            add_session(chat_id, key=key_path, auth_kind='key')
            await finish_single_add(update, context, ADD_SESSIONS[chat_id])
            return
        if step == 'single_key_text':
            try:
                key_path = save_private_key(chat_id, text, 'telegram-key')
            except Exception as e:
                await update.message.reply_text(f'密钥识别失败：{safe(e)}', parse_mode=ParseMode.HTML)
                return
            add_session(chat_id, key=key_path, auth_kind='key')
            await finish_single_add(update, context, ADD_SESSIONS[chat_id])
            return
        if step == 'bulk_port':
            if not text.strip().isdigit():
                await update.message.reply_text('端口需要是数字，比如 22 或 53580。')
                return
            add_session(chat_id, same_port=int(text.strip()), step='bulk_auth')
            await update.message.reply_text('端口已设置。现在选择认证方式：', reply_markup=bulk_auth_markup())
            return
        if step == 'bulk_shared_password':
            add_session(chat_id, auth_mode='password', shared_auth=text.strip(), step='bulk_lines')
            await update.message.reply_text('密码已记录。现在发送服务器列表，每行一台。\n\n格式：<code>名称 IP 用户</code>', parse_mode=ParseMode.HTML)
            return
        if step == 'bulk_shared_key_text':
            try:
                key_path = save_private_key(chat_id, text, 'bulk-key')
            except Exception as e:
                await update.message.reply_text(f'密钥识别失败：{safe(e)}', parse_mode=ParseMode.HTML)
                return
            add_session(chat_id, auth_mode='key', shared_auth=key_path, step='bulk_lines')
            await update.message.reply_text('密钥已保存。现在发送服务器列表，每行一台。\n\n格式：<code>名称 IP 用户</code>', parse_mode=ParseMode.HTML)
            return
        if step == 'bulk_lines':
            await finish_bulk_add(update, context, sess, text)
            return
        if step == 'edit_value':
            sid = sess.get('sid')
            field = sess.get('field')
            s = find_server_by_id(sid)
            if not s:
                clear_add_session(chat_id)
                await update.message.reply_text('这台服务器不在当前清单里。')
                return
            val = text.strip()
            patch = {}
            if field == 'name':
                patch['name'] = val
            elif field == 'host':
                host, embedded_port = parse_host_port(val)
                if not is_valid_hostname(host):
                    await update.message.reply_text('IP/域名看起来不对，重新发一次。')
                    return
                patch['host'] = host
                if embedded_port:
                    patch['ssh'] = {'port': embedded_port}
            elif field == 'port':
                if not val.isdigit():
                    await update.message.reply_text('端口需要是数字。')
                    return
                patch['ssh'] = {'port': int(val)}
            elif field == 'user':
                patch['ssh'] = {'user': val}
            elif field == 'key':
                patch['ssh'] = {'auth': 'key', 'key': os.path.expanduser(val)}
            elif field == 'password':
                patch['ssh'] = {'auth': 'password', 'password': val}
            updated = update_server_by_id(sid, patch)
            clear_add_session(chat_id)
            await update.message.reply_text(f'已更新：<b>{safe(updated.get("name"))}</b>', parse_mode=ParseMode.HTML, reply_markup=server_markup(updated))
            return
    pending_sid = PENDING_NEXTTRACE.pop(chat_id, None) if chat_id is not None else None
    if pending_sid:
        s = find_server_by_id(pending_sid)
        if not s:
            await update.message.reply_text('刚才选择的服务器不在当前清单里。')
            return
        target = extract_ipv4(text) or normalize_domain(text)
        if not target:
            await update.message.reply_text('没识别到 IP 或域名，已取消这次 NextTrace。')
            return
        jid, pos = enqueue_job(s, 'nexttrace', run_nexttrace_task, context.bot, chat_id, s, target, target=target)
        await bot_queue_notice(context.bot, chat_id, s, f'NextTrace {safe(target)}', pos)
        return
    # 普通文本不再触发任何功能；只有点击 NextTrace 后的下一条 IP/域名才会被消费。
    return


async def post_init(app: Application):
    commands = [
        BotCommand('start', '打开 GUKO 面板'),
        BotCommand('list', '服务器列表'),
        BotCommand('status', '总览状态'),
        BotCommand('addserver', '添加/批量导入服务器'),
        BotCommand('testssh', '测试服务器 SSH'),
        BotCommand('testall', '批量测试 SSH'),
        BotCommand('exportconfig', '导出脱敏配置'),
        BotCommand('info', '查看单台操作面板：/info 名字/IP/ID'),
        BotCommand('jobs', '查看后台任务'),
        BotCommand('history', '查看测试历史：/history 服务器'),
        BotCommand('ip', 'IP/域名工具：/ip 1.1.1.1'),
        BotCommand('nexttrace', '路由追踪：/nexttrace 服务器 目标'),
        BotCommand('version', '查看 GUKO 版本'),
    ]
    await app.bot.set_my_commands(commands)


async def ip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text('用法：/ip <IPv4 或域名>')
        return
    target = ' '.join(context.args)
    try:
        ip, host = await resolve_target_to_ipv4(target)
        suffix = f'{safe(host)} → <code>{safe(ip)}</code>' if host else f'<code>{safe(ip)}</code>'
        await update.message.reply_text(f'识别到 {suffix}，选一个生成：', parse_mode=ParseMode.HTML, reply_markup=ip_tools_markup(ip))
    except Exception as e:
        await update.message.reply_text(f'解析失败：{safe(e)}', parse_mode=ParseMode.HTML)


async def nexttrace_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text('用法：/nexttrace <服务器名/IP/ID/别名> [目标IP或域名]\n例：/nexttrace 海创 1.1.1.1')
        return
    s = find_server(context.args[0], load_inventory().get('servers', []))
    if not s:
        await update.message.reply_text('没找到这台服务器。')
        return
    target = context.args[1] if len(context.args) > 1 else '1.1.1.1'
    if not (extract_ipv4(target) or normalize_domain(target)):
        await update.message.reply_text('目标需要是 IPv4 或域名。')
        return
    jid, pos = enqueue_job(s, 'nexttrace', run_nexttrace_task, context.bot, update.effective_chat.id, s, target, target=target)
    if pos <= 1:
        await update.message.reply_text(f'🛣 已启动 {safe(s.get("name"))} NextTrace：{safe(target)}', parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f'🕒 已加入队列：{safe(s.get("name"))} NextTrace：{safe(target)}（第 {pos} 个）', parse_mode=ParseMode.HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    q = update.callback_query
    await q.answer()
    data = q.data or ''
    chat_id = q.message.chat_id if q.message else update.effective_chat.id
    if data == 'add:start':
        if not await admin_guard(update): return
        clear_add_session(chat_id)
        await q.edit_message_text(add_help_text(), parse_mode=ParseMode.HTML, reply_markup=add_start_markup())
    elif data == 'add:one':
        if not await admin_guard(update): return
        add_session(chat_id, step='single_basic')
        await q.edit_message_text(
            '➕ 发送服务器信息：\n\n'
            '<code>名称 IP [端口] [用户]</code>\n\n'
            '例：<code>hk-01 1.2.3.4 22 root</code>\n'
            '也支持：<code>hk-01 1.2.3.4:53580 root</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]])
        )
    elif data == 'add:bulk':
        if not await admin_guard(update): return
        add_session(chat_id, step='bulk_choose_port')
        await q.edit_message_text(bulk_help_text() + '\n\n先选择端口策略：', parse_mode=ParseMode.HTML, reply_markup=bulk_mode_markup())
    elif data == 'add:cancel':
        clear_add_session(chat_id)
        await q.edit_message_text('已取消添加服务器。', reply_markup=main_menu_markup())
    elif data == 'addauth:default':
        defaults = inventory_defaults(load_inventory())
        add_session(chat_id, auth_kind='default')
        await q.edit_message_text(
            f'将沿用默认 SSH 配置测试登录：\n<code>{safe(defaults.get("user"))}@服务器:{safe(defaults.get("port"))}</code>\n密钥：<code>{safe(defaults.get("key"))}</code>',
            parse_mode=ParseMode.HTML,
        )
        await finish_single_add(update, context, ADD_SESSIONS[chat_id])
    elif data == 'addauth:keypath':
        add_session(chat_id, step='single_key_path')
        await q.edit_message_text('请发送已有 SSH 私钥路径，例如：\n<code>/data/keys/id_ed25519</code>\n\n适合新服务器继续使用以前同一把密钥。', parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'addauth:key':
        add_session(chat_id, step='single_key_text')
        await q.edit_message_text('请直接发送 SSH 私钥文本，或以文件形式上传私钥。\n\n需要包含 SSH 私钥的 BEGIN/END 头尾标记。', parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'addauth:password':
        add_session(chat_id, step='single_password')
        await q.edit_message_text('请发送这台服务器的 SSH 密码。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'addauth:skip':
        add_session(chat_id, auth_kind=None)
        await finish_single_add(update, context, ADD_SESSIONS[chat_id])
    elif data == 'bulkport:same':
        add_session(chat_id, step='bulk_port')
        await q.edit_message_text('请输入所有服务器共用的 SSH 端口，例如 <code>22</code> 或 <code>53580</code>。', parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'bulkport:per':
        add_session(chat_id, same_port=None, step='bulk_auth')
        await q.edit_message_text('好，每台服务器自己写端口。现在选择认证方式：', reply_markup=bulk_auth_markup())
    elif data == 'bulkauth:key':
        add_session(chat_id, step='bulk_shared_key_text')
        await q.edit_message_text('请发送所有服务器共用的 SSH 私钥文本，或上传私钥文件。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'bulkauth:password':
        add_session(chat_id, step='bulk_shared_password')
        await q.edit_message_text('请发送所有服务器共用的 SSH 密码。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'bulkauth:per':
        add_session(chat_id, auth_mode='per', step='bulk_lines')
        await q.edit_message_text(
            '请发送服务器列表，每行一台：\n\n'
            '<code>名称 IP 端口 用户 key:/data/keys/a</code>\n'
            '<code>名称 IP 端口 用户 password:你的密码</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]])
        )
    elif data == 'bulkauth:skip':
        add_session(chat_id, auth_mode='skip', step='bulk_lines')
        await q.edit_message_text('请发送服务器列表，每行一台：\n\n<code>名称 IP [端口] [用户]</code>', parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data == 'noop':
        await q.answer('该功能未启用', show_alert=True)
    elif data == 'act:list':
        await send_or_edit(update, menu_text(), main_menu_markup())
    elif data.startswith('edit:'):
        if not await admin_guard(update): return
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        cfg = ssh_config(s)
        await q.edit_message_text(
            f'✏️ 编辑 <b>{safe(s.get("name"))}</b>\n<code>{safe(cfg.get("user"))}@{safe(cfg.get("host"))}:{safe(cfg.get("port"))}</code>',
            parse_mode=ParseMode.HTML,
            reply_markup=edit_markup(s),
        )
    elif data.startswith('editfield:'):
        if not await admin_guard(update): return
        _p, sid, field = data.split(':', 2)
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=main_menu_markup())
            return
        add_session(chat_id, step='edit_value', sid=sid, field=field)
        labels = {'name': '新名称', 'host': '新 IP/域名（可带 :端口）', 'port': '新端口', 'user': '新 SSH 用户名', 'key': '新密钥路径', 'password': '新密码'}
        await q.edit_message_text(f'请发送{labels.get(field, "新值")}：', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ 取消', callback_data='add:cancel')]]))
    elif data.startswith('editdefault:'):
        if not await admin_guard(update): return
        sid = data.split(':', 1)[1]
        updated = update_server_by_id(sid, {'ssh': {'auth': 'key', 'key': None}})
        if not updated:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=main_menu_markup())
            return
        await q.edit_message_text(f'已改为沿用默认密钥：<b>{safe(updated.get("name"))}</b>', parse_mode=ParseMode.HTML, reply_markup=server_markup(updated))
    elif data.startswith('jobsrv:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        await q.edit_message_text(
            job_status_text(s),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🔄 刷新任务', callback_data=f'jobsrv:{sid}')],
                [InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')],
            ]),
        )
    elif data.startswith('hist:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        await q.edit_message_text(
            history_text(s),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=history_markup(s),
        )
    elif data.startswith('histd:'):
        parts = data.split(':', 2)
        sid = parts[1] if len(parts) > 1 else ''
        kind = parts[2] if len(parts) > 2 else ''
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        item = history_item_for(s, kind)
        if item and (latest_media_path(item) or kind == 'nq'):
            await send_history_result(context.bot, q.message.chat_id, s, kind)
            await q.edit_message_text(
                f'已重新发送 <b>{safe(KIND_NAME.get(kind, kind))}</b> 最近一次完整结果。',
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('↩️ 返回历史记录', callback_data=f'hist:{sid}')],
                    [InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')],
                ]),
            )
        else:
            await q.edit_message_text(
                history_detail_text(s, kind),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('↩️ 返回历史记录', callback_data=f'hist:{sid}')],
                    [InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')],
                ]),
            )
    elif data.startswith('testssh:'):
        if not await admin_guard(update): return
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        await q.edit_message_text(f'🧪 正在测试 {safe(s.get("name"))} SSH…', parse_mode=ParseMode.HTML)
        ok, out = await test_server_login(s)
        await q.edit_message_text(
            (f'✅ <b>{safe(s.get("name"))}</b> SSH 登录成功：' if ok else f'⚠️ <b>{safe(s.get("name"))}</b> SSH 登录失败：') + '<pre>' + safe(out[-1200:]) + '</pre>',
            parse_mode=ParseMode.HTML,
            reply_markup=server_markup(s),
        )
    elif data.startswith('delask:'):
        if not await admin_guard(update): return
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        cfg = ssh_config(s)
        await q.edit_message_text(
            '⚠️ 确认删除这台服务器？\n\n'
            f'<b>{safe(s.get("name"))}</b>\n'
            f'<code>{safe(cfg.get("user"))}@{safe(cfg.get("host"))}:{safe(cfg.get("port"))}</code>\n\n'
            '只会从 GUKO 配置删除，不会动远端机器。',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ 确认删除', callback_data=f'delconfirm:{sid}')],
                [InlineKeyboardButton('❌ 取消删除', callback_data=f'srv:{sid}')],
            ]),
        )
    elif data.startswith('delconfirm:'):
        if not await admin_guard(update): return
        sid = data.split(':', 1)[1]
        removed = delete_server_by_id(sid)
        if not removed:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=main_menu_markup())
            return
        await q.edit_message_text(f'已从配置删除：<b>{safe(removed.get("name"))}</b>', parse_mode=ParseMode.HTML, reply_markup=main_menu_markup())
    elif data.startswith('ipq:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        jid, pos = enqueue_job(s, 'ipq', run_ip_quality_task, context.bot, q.message.chat_id, s)
        await bot_queue_notice(context.bot, q.message.chat_id, s, 'IP质量任务', pos)
    elif data.startswith('gb5:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        jid, pos = enqueue_job(s, 'gb5', run_gb5_task, context.bot, q.message.chat_id, s)
        await bot_queue_notice(context.bot, q.message.chat_id, s, 'GB5', pos)
    elif data.startswith('stream:') or data.startswith('streamrun:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        region_id, region_label = stream_region_for_server(s)
        jid, pos = enqueue_job(s, 'stream', run_stream_task, context.bot, q.message.chat_id, s, region=region_label)
        await bot_queue_notice(context.bot, q.message.chat_id, s, f'流媒体检测（{safe(region_label)}）', pos)
    elif data.startswith('ntask:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        PENDING_NEXTTRACE[q.message.chat_id] = sid
        await q.edit_message_text(
            nexttrace_prompt_text(s),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回操作面板', callback_data=f'srv:{sid}')]])
        )
    elif data.startswith('ntrun:'):
        parts = data.split(':', 2)
        sid = parts[1]
        target = parts[2] if len(parts) > 2 else '1.1.1.1'
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        jid, pos = enqueue_job(s, 'nexttrace', run_nexttrace_task, context.bot, q.message.chat_id, s, target, target=target)
        await bot_queue_notice(context.bot, q.message.chat_id, s, f'NextTrace {safe(target)}', pos)
    elif data.startswith('bgp:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        jid, pos = enqueue_job(s, 'bgp', run_bgp_task, context.bot, q.message.chat_id, s)
        await bot_queue_notice(context.bot, q.message.chat_id, s, 'BGP 图任务', pos)
    elif data.startswith('ippure:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        jid, pos = enqueue_job(s, 'ippure', run_ippure_task, context.bot, q.message.chat_id, s)
        await bot_queue_notice(context.bot, q.message.chat_id, s, 'IPPure 图任务', pos)
    elif data.startswith('bgpip:'):
        ip = data.split(':', 1)[1]
        if not is_ipv4(ip):
            await q.answer('无效 IPv4', show_alert=True)
            return
        pseudo = {'id': ip, 'name': ip, 'host': ip}
        jid, key = start_job(pseudo, 'bgp')
        if not jid:
            await q.answer('这个 IP 的 BGP 图已经在生成了', show_alert=True)
            return
        await send_running_notice(context.bot, q.message.chat_id, pseudo, 'BGP 图任务')
        asyncio.create_task(run_bgp_task(context.bot, q.message.chat_id, pseudo, jid))
    elif data.startswith('ippureip:'):
        ip = data.split(':', 1)[1]
        if not is_ipv4(ip):
            await q.answer('无效 IPv4', show_alert=True)
            return
        pseudo = {'id': ip, 'name': ip, 'host': ip}
        jid, key = start_job(pseudo, 'ippure')
        if not jid:
            await q.answer('这个 IP 的 IPPure 图已经在生成了', show_alert=True)
            return
        await send_running_notice(context.bot, q.message.chat_id, pseudo, 'IPPure 图任务')
        asyncio.create_task(run_ippure_task(context.bot, q.message.chat_id, pseudo, jid))
    elif data.startswith('nqask:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        mask = NQ_DEFAULT_MASK
        await q.edit_message_text(
            nq_menu_text(s, mask, '4'),
            parse_mode=ParseMode.HTML, reply_markup=confirm_nq_markup(s, mask, '4')
        )
    elif data.startswith('nqtoggle:') or data.startswith('nqsel:') or data.startswith('nqproto:'):
        parts = data.split(':')
        _kind, sid = parts[0], parts[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        try:
            mask = int(parts[2]) & NQ_ALL_MASK
        except Exception:
            mask = NQ_DEFAULT_MASK
        ip_mode = parts[3] if len(parts) > 3 else '4'
        if ip_mode == '46' and not server_has_ipv6(s):
            ip_mode = '4'
        await q.edit_message_text(
            nq_menu_text(s, mask, ip_mode),
            parse_mode=ParseMode.HTML, reply_markup=confirm_nq_markup(s, mask, ip_mode)
        )
    elif data.startswith('nqrun:'):
        parts = data.split(':')
        sid = parts[1]
        try:
            mask = int(parts[2]) & NQ_ALL_MASK if len(parts) > 2 else NQ_DEFAULT_MASK
        except Exception:
            mask = NQ_DEFAULT_MASK
        ip_mode = parts[3] if len(parts) > 3 else '4'
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        if mask == 0:
            await q.answer('至少选一项', show_alert=True)
            await q.edit_message_text(
                nq_menu_text(s, mask, ip_mode) + '\n\n至少选一项才能开始。',
                parse_mode=ParseMode.HTML, reply_markup=confirm_nq_markup(s, mask, ip_mode)
            )
            return
        if ip_mode == '46' and not server_has_ipv6(s):
            ip_mode = '4'
        selected_text = nq_selected_text(mask)
        ip_text = nq_ip_mode_text(ip_mode)
        jid, pos = enqueue_job(s, 'nq', run_nq_task, context.bot, q.message.chat_id, s, mask, ip_mode, selected=selected_text, ip_mode=ip_text)
        await bot_queue_notice(context.bot, q.message.chat_id, s, f'NodeQuality（{safe(selected_text)}；{safe(ip_text)}）', pos)
    elif data.startswith('srv:'):
        sid = data.split(':', 1)[1]
        s = find_server_by_id(sid)
        if not s:
            await q.edit_message_text('这台服务器不在当前清单里。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回列表', callback_data='act:list')]]))
            return
        await q.edit_message_text(server_detail_text(s), parse_mode=ParseMode.HTML, reply_markup=server_markup(s))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f'bot error: {context.error!r}', flush=True)


def main():
    startup_check()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(CommandHandler('version', version_cmd))
    app.add_handler(CommandHandler('list', list_cmd))
    app.add_handler(CommandHandler('status', status_cmd))
    app.add_handler(CommandHandler('addserver', addserver_cmd))
    app.add_handler(CommandHandler('testssh', testssh_cmd))
    app.add_handler(CommandHandler('testall', testall_cmd))
    app.add_handler(CommandHandler('exportconfig', export_config_cmd))
    app.add_handler(CommandHandler('info', info_cmd))
    app.add_handler(CommandHandler('jobs', jobs_cmd))
    app.add_handler(CommandHandler('history', history_cmd))
    app.add_handler(CommandHandler('ip', ip_cmd))
    app.add_handler(CommandHandler('nexttrace', nexttrace_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_panel))
    app.add_error_handler(error_handler)
    print(f'guko telegram bot started v{GUKO_VERSION}', flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
