"""Writes docs/docs/whats-new.md from the version commits in git history ("v1.2.3: Title. Details...")."""
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

OUT = Path(__file__).parent / 'docs' / 'docs' / 'whats-new.md'
HEAD = '# What\'s new\n\nEvery iopix release, newest first. Update from **Maintenance › iopix › Update now**, or see [Updating](install/updating.md).\n'
VERSION = re.compile(r'^v(\d+)\.(\d+)\.(\d+):\s+(.*)$')


def summary(text):
    """The title and the sentence after it, without API or file details."""
    text = text.replace(' > ', ' › ')
    text = re.split(r'\s(?:API|Tests?|Internals?):', text)[0]
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    title = parts[0].rstrip('.')
    rest = parts[1] if len(parts) > 1 else ''
    if len(rest) > 320:  # keep the sentence up to its first clause break, else leave it out
        cut = min((i for i in (rest.find(': '), rest.find('; ')) if 40 < i < 320), default=-1)
        rest = rest[:cut].rstrip(' ,') + '.' if cut > 0 else ''
    if not title.startswith('iopix'):
        title = title[:1].upper() + title[1:]
    return title, rest


def main():
    try:
        log = subprocess.run(['git', 'log', '--format=%ad%x09%s', '--date=short'], capture_output=True,
                             text=True, check=True, cwd=Path(__file__).parent).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        print(f'whatsnew: no git history ({e}); writing a placeholder', file=sys.stderr)
        OUT.write_text(HEAD + '\nSee the [releases on GitHub](https://github.com/avandeputte/iopix/commits/main).\n')
        return
    seen, days = set(), OrderedDict()
    for line in log.splitlines():
        date, _, subject = line.partition('\t')
        m = VERSION.match(subject)
        if not m or m.group(0)[:m.end(3)] in seen:
            continue
        ver = f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
        seen.add(m.group(0)[:m.end(3)])
        if re.search(r'google photos|digikam viewer', subject, re.I):
            continue
        title, rest = summary(m.group(4))
        days.setdefault(date, []).append((ver, title, rest))
    lines = [HEAD]
    for date, items in days.items():
        lines.append(f'\n## {date}\n')
        for ver, title, rest in items:
            lines.append(f'- **{ver}** · {title}.' + (f' {rest}' if rest else ''))
    OUT.write_text('\n'.join(lines) + '\n')
    print(f'whatsnew: {len(seen)} versions')


if __name__ == '__main__':
    main()
