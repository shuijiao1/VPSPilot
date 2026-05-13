#!/usr/bin/env python3
import argparse, os, subprocess, sys, shlex, json
from pathlib import Path

from auth import resolve_ssh, ssh_args, ssh_display

ROOT = Path(__file__).resolve().parent
INV = Path(os.environ.get('JIAOOPS_INV', ROOT / 'servers.json'))


def load_inventory():
    if not INV.exists():
        return {'servers': []}
    return json.loads(INV.read_text() or '{}')


def ssh_env_for(s, inv):
    cfg = resolve_ssh(s, inv)
    if cfg.get('auth') == 'password' and cfg.get('password'):
        env = os.environ.copy()
        env['SSHPASS'] = str(cfg['password'])
        return env
    return None


def health_cmd():
    return r'''set -e
printf 'host='; hostname
printf 'uptime='; uptime -p || true
printf 'load='; awk '{print $1,$2,$3}' /proc/loadavg
printf 'mem='; free -h | awk '/Mem:/ {print $3 "/" $2}'
printf 'disk='; df -h / | awk 'NR==2 {print $3 "/" $2 " (" $5 ")"}'
printf 'kernel='; uname -r
printf 'ssh='; ss -ltnp 2>/dev/null | grep -E ':(22|53580) ' || true
systemctl is-active nezha-agent >/dev/null 2>&1 && echo 'nezha=active' || echo 'nezha=inactive-or-missing'
'''


def main():
    p = argparse.ArgumentParser(prog='jiaoops', description='饺管家 / JiaoOps server manager')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    sub.add_parser('health')
    runp = sub.add_parser('run'); runp.add_argument('name'); runp.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    inv = load_inventory()
    servers = inv.get('servers') or []
    if args.cmd == 'list':
        if not servers:
            print('no servers in servers.json')
        for s in servers:
            extra = f" nezha_id={s.get('nezha_id')}" if s.get('nezha_id') is not None else ''
            try:
                endpoint = ssh_display(s, inv)
            except Exception:
                endpoint = '<no-host>'
            print(f"{s.get('name','?')} {endpoint} {s.get('role','')}{extra}")
    elif args.cmd == 'health':
        if not servers:
            print('no servers in servers.json')
            return
        for s in servers:
            if not s.get('host') and not (s.get('ssh') or {}).get('host'):
                print(f"\n== {s.get('name','?')} (<no-host>) ==")
                print('skipped: no ssh host in inventory')
                continue
            print(f"\n== {s.get('name',s.get('host'))} ({ssh_display(s, inv)}) ==")
            r = subprocess.run(ssh_args(s, health_cmd(), inv=inv), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, env=ssh_env_for(s, inv))
            print(r.stdout.rstrip())
    elif args.cmd == 'run':
        target = next((s for s in servers if s.get('name') == args.name or s.get('host') == args.name or str(s.get('nezha_id')) == args.name), None)
        if not target:
            sys.exit(f'not found: {args.name}')
        if not target.get('host') and not (target.get('ssh') or {}).get('host'):
            sys.exit(f"no ssh host for: {args.name}")
        if not args.command:
            sys.exit('missing command')
        # Support both styles:
        #   jiaoops.py run host 'systemctl status nginx --no-pager'
        #   jiaoops.py run host systemctl status nginx --no-pager
        remote = args.command[0] if len(args.command) == 1 else ' '.join(shlex.quote(x) for x in args.command)
        subprocess.run(ssh_args(target, remote, inv=inv), check=False, env=ssh_env_for(target, inv))


if __name__ == '__main__':
    main()
