#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'HELP'
Usage: ./release.sh <version> "release notes"

Example:
  ./release.sh 0.1.2 "修复脚本更新检测并补充部署文档。"

The script will:
  1. check that the git working tree is clean;
  2. update version files when they exist;
  3. prepend CHANGELOG.md;
  4. commit, tag and push to GitHub.
HELP
}

version="${1:-}"
notes="${2:-}"
if [[ -z "$version" || -z "$notes" || "$version" == "-h" || "$version" == "--help" ]]; then
  usage
  exit 1
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "version must be semantic version like 0.1.2" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is not clean" >&2
  git status --short
  exit 1
fi

python3 - "$version" "$notes" <<'PYHELP'
import datetime, json, re, sys
from pathlib import Path
version, notes = sys.argv[1], sys.argv[2]
root = Path('.')
if (root / 'version.txt').exists():
    (root / 'version.txt').write_text(version + '
')
for path in root.glob('*.sh'):
    text = path.read_text()
    text = re.sub(r'^(VERSION=")[0-9]+\.[0-9]+\.[0-9]+(".*)$', rf'\g<1>{version}', text, flags=re.M)
    text = re.sub(r'^(SCRIPT_VERSION=")[0-9]+\.[0-9]+\.[0-9]+(".*)$', rf'\g<1>{version}', text, flags=re.M)
    path.write_text(text)
for name in ['package.json', 'package-lock.json']:
    p = root / name
    if p.exists():
        data = json.loads(p.read_text())
        data['version'] = version
        if name == 'package-lock.json' and 'packages' in data and '' in data['packages']:
            data['packages']['']['version'] = version
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '
')
bot = root / 'telegram-bot' / 'bot.py'
if bot.exists():
    text = bot.read_text()
    text = re.sub(r"GUKO_VERSION = os\.environ\.get\('GUKO_VERSION', '[^']+'\)\.strip\(\) or '[^']+'", f"GUKO_VERSION = os.environ.get('GUKO_VERSION', '{version}').strip() or '{version}'", text)
    bot.write_text(text)
env = root / '.env.example'
if env.exists():
    text = env.read_text()
    text = re.sub(r'^GUKO_VERSION=.*$', f'GUKO_VERSION={version}', text, flags=re.M)
    env.write_text(text)
changelog = root / 'CHANGELOG.md'
entry = f"## [{version}] - {datetime.date.today().isoformat()}

- {notes}

"
if changelog.exists():
    text = changelog.read_text()
    if f'## [{version}]' not in text:
        text = re.sub(r'(# Changelog

)', r'' + entry, text, count=1)
    changelog.write_text(text)
else:
    changelog.write_text('# Changelog

' + entry)
PYHELP

git add -A
git commit -m "Release v$version"
git tag -a "v$version" -m "v$version"
git push
git push origin "v$version"
