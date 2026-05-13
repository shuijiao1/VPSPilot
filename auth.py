import os
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_KEY = os.environ.get('JIAOOPS_DEFAULT_KEY', '/data/keys/id_ed25519')
DEFAULT_PORT = int(os.environ.get('JIAOOPS_DEFAULT_PORT', '22'))
DEFAULT_USER = os.environ.get('JIAOOPS_DEFAULT_USER', 'root')


def inventory_defaults(inv: dict | None = None) -> dict:
    inv = inv or {}
    defaults = inv.get('defaults') or {}
    ssh = defaults.get('ssh') or defaults
    return {
        'user': ssh.get('user') or defaults.get('user') or DEFAULT_USER,
        'port': ssh.get('port') or defaults.get('port') or DEFAULT_PORT,
        'key': ssh.get('key') or defaults.get('key') or DEFAULT_KEY,
        'password': ssh.get('password') or defaults.get('password'),
        'auth': ssh.get('auth') or defaults.get('auth'),
    }


def resolve_ssh(server: dict, inv: dict | None = None) -> dict:
    """Resolve SSH settings with compatibility for old flat server entries.

    Supported server formats:
      {"host":"1.2.3.4", "port":22, "user":"root", "key":"/path/key"}
      {"host":"1.2.3.4", "ssh":{"port":22, "user":"root", "key":"/path/key"}}
      {"host":"1.2.3.4", "auth":"password", "password":"secret"}
      {"host":"1.2.3.4", "ssh":{"auth":"password", "password":"secret"}}
    """
    defaults = inventory_defaults(inv)
    ssh = server.get('ssh') or {}
    explicit_key = ssh.get('key') or server.get('key')
    cfg = {
        'host': ssh.get('host') or server.get('host'),
        'user': ssh.get('user') or server.get('user') or defaults['user'],
        'port': ssh.get('port') or server.get('port') or defaults['port'],
        'key': explicit_key or defaults.get('key'),
        'password': ssh.get('password') or server.get('password') or defaults.get('password'),
        'auth': ssh.get('auth') or server.get('auth') or defaults.get('auth'),
    }
    cfg['port'] = int(cfg['port'])
    if not cfg['auth']:
        cfg['auth'] = 'password' if cfg.get('password') else 'key'
    if cfg['auth'] == 'password' and not explicit_key:
        cfg['key'] = None
    if cfg.get('key'):
        cfg['key'] = os.path.expanduser(str(cfg['key']))
    return cfg


def _base_ssh_options(cfg: dict, *, tty=False, batch=True) -> list[str]:
    args = [
        '-o', f"BatchMode={'yes' if batch else 'no'}",
        '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=6',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-p', str(cfg['port']),
    ]
    if cfg.get('key'):
        args += ['-i', str(cfg['key'])]
    if tty:
        args.append('-tt')
    return args


def ssh_args(server: dict, remote: str, *, tty=False, inv: dict | None = None) -> list[str]:
    cfg = resolve_ssh(server, inv)
    if not cfg.get('host'):
        raise ValueError('missing ssh host')
    batch = not (cfg.get('auth') == 'password' and cfg.get('password'))
    args = ['ssh'] + _base_ssh_options(cfg, tty=tty, batch=batch)
    args += [f"{cfg['user']}@{cfg['host']}", remote]
    if cfg.get('auth') == 'password' and cfg.get('password'):
        return ['sshpass', '-e'] + args
    return args


def scp_from_args(server: dict, remote_path: str, local_path: str, *, inv: dict | None = None) -> list[str]:
    cfg = resolve_ssh(server, inv)
    if not cfg.get('host'):
        raise ValueError('missing ssh host')
    batch = not (cfg.get('auth') == 'password' and cfg.get('password'))
    args = [
        'scp',
        '-o', f"BatchMode={'yes' if batch else 'no'}",
        '-o', 'ConnectTimeout=10',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-P', str(cfg['port']),
    ]
    if cfg.get('key'):
        args += ['-i', str(cfg['key'])]
    args += [f"{cfg['user']}@{cfg['host']}:{remote_path}", local_path]
    if cfg.get('auth') == 'password' and cfg.get('password'):
        return ['sshpass', '-e'] + args
    return args


def ssh_display(server: dict, inv: dict | None = None) -> str:
    cfg = resolve_ssh(server, inv)
    auth = 'password' if cfg.get('auth') == 'password' else 'key'
    return f"{cfg.get('user')}@{cfg.get('host')}:{cfg.get('port')} ({auth})"


def shell_join(args: list[str]) -> str:
    return ' '.join(shlex.quote(str(x)) for x in args)
