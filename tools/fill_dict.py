"""Help write a book's dict.txt by reusing glosses from the other books.

  python3 tools/fill_dict.py <slug>          list word forms that still need a gloss
  python3 tools/fill_dict.py <slug> --write  write books-src/<slug>/dict.txt from
                                             books-src/<slug>/new.txt (your glosses, these win)
                                             plus matching lines from other books' dict.txt

Check reused lines still fit the new context and put an override in new.txt if not.
"""
import os, re, runpy, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'books-src')
L = 'A-Za-zÀ-ÖØ-öø-ÿœŒ'
TOK = re.compile(rf"[{L}]+'|[{L}]+(?:-[{L}]+)*")


def load(path):
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.rstrip('\n')
            if line.strip():
                out[line.split('|')[0].lower()] = line
    return out


slug = sys.argv[1]
d = os.path.join(SRC, slug)
chapters = runpy.run_path(os.path.join(d, 'content.py'))['CHAPTERS']
used = sorted({t.lower() for ch in chapters for p in ch['paras'] for fr, _ in p for t in TOK.findall(fr)})
new = load(os.path.join(d, 'new.txt'))
other = {}
for s in sorted(os.listdir(SRC)):
    if s != slug and os.path.isdir(os.path.join(SRC, s)):
        for k, v in load(os.path.join(SRC, s, 'dict.txt')).items():
            other.setdefault(k, v)

missing = [w for w in used if w not in new and w not in other]
words = sum(len(TOK.findall(fr)) for ch in chapters for p in ch['paras'] for fr, _ in p)
print(f'{words} words, {len(used)} forms, {len(missing)} still need a gloss')
if '--write' in sys.argv:
    if missing:
        sys.exit('missing: ' + ' '.join(missing))
    lines = [new.get(w) or other[w] for w in used]
    open(os.path.join(d, 'dict.txt'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    unused = sorted(set(new) - set(used))
    if unused:
        print('unused lines in new.txt:', ' '.join(unused))
    print('wrote dict.txt')
else:
    print(' '.join(missing))
