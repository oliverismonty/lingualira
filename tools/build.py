"""Build every book in books-src/ into books/<slug>.json, books/<slug>.svg and books/index.json.

Each book folder needs:
  meta.json    title, author, year, blurb, level, colour (cover band), added (YYYY-MM-DD)
  content.py   CHAPTERS = [{"fr", "en", "paras": [[(french sentence, english sentence), ...], ...]}, ...]
  dict.txt     one line per word form: form|English gloss|grammar note (note may be empty)
  phrases.txt  one line per phrase: tokens separated by spaces, elisions kept as  d' abord|gloss
  cover.svg    picture for the library card and the book's cover band (420x200 works well)

Run:  python3 tools/build.py      (fails and lists any word missing from dict.txt)
"""
import hashlib, json, os, re, runpy, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC, OUT = os.path.join(ROOT, 'books-src'), os.path.join(ROOT, 'books')
L = 'A-Za-zÀ-ÖØ-öø-ÿœŒ'
TOK = re.compile(rf"[{L}]+'|[{L}]+(?:-[{L}]+)*")


def segs(s):
    out, last = [], 0
    for m in TOK.finditer(s):
        if m.start() > last:
            out.append(s[last:m.start()])
        out.append([m.group(0)])
        last = m.end()
    if last < len(s):
        out.append(s[last:])
    return out


def build(slug):
    d = os.path.join(SRC, slug)
    meta = json.load(open(os.path.join(d, 'meta.json'), encoding='utf-8'))
    chapters = runpy.run_path(os.path.join(d, 'content.py'))['CHAPTERS']
    D = {}
    for line in open(os.path.join(d, 'dict.txt'), encoding='utf-8'):
        line = line.rstrip('\n')
        if line.strip():
            f, g, n = line.split('|')
            D[f.lower()] = [g, n]
    phr = []
    for line in open(os.path.join(d, 'phrases.txt'), encoding='utf-8'):
        line = line.rstrip('\n')
        if line.strip():
            p, g = line.split('|')
            phr.append([p.lower().split(' '), g])

    missing, used, words, book = set(), set(), 0, []
    for ch in chapters:
        paras = []
        for p in ch['paras']:
            sents = []
            for fr, en in p:
                sg = segs(fr)
                for x in sg:
                    if isinstance(x, list):
                        k = x[0].lower(); words += 1; used.add(k)
                        if k not in D:
                            missing.add(k)
                sents.append([sg, en])
            paras.append(sents)
        book.append({'fr': ch['fr'], 'en': ch['en'], 'p': paras})
    if missing:
        sys.exit(f'{slug}: missing from dict.txt: {" ".join(sorted(missing))}')
    toks = [[x[0].lower() for x in se[0] if isinstance(x, list)] for ch in book for p in ch['p'] for se in p]
    for p, _ in phr:
        if not any(t[i:i + len(p)] == p for t in toks for i in range(len(t))):
            print(f'{slug}: warning, phrase never appears in the text: {" ".join(p)}')

    data = {'slug': slug, **meta, 'words': words, 'chapters': len(book),
            'book': book, 'dict': {k: D[k] for k in sorted(used)}, 'phrases': phr}
    json.dump(data, open(os.path.join(OUT, slug + '.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    shutil.copy(os.path.join(d, 'cover.svg'), os.path.join(OUT, slug + '.svg'))
    return {'slug': slug, **meta, 'words': words, 'chapters': len(book)}


def main():
    os.makedirs(OUT, exist_ok=True)
    books = [build(s) for s in sorted(os.listdir(SRC)) if os.path.isdir(os.path.join(SRC, s))]
    books.sort(key=lambda b: b.get('added', ''), reverse=True)
    json.dump(books, open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # bump the offline cache whenever anything changes, and list every book file in it
    h = hashlib.sha1()
    files = ['index.html', 'reader.html', 'style.css', 'manifest.webmanifest', 'icon-180.png?v=2', 'icon-192.png?v=2', 'icon-512.png?v=2', 'books/index.json']
    for b in books:
        files += [f"books/{b['slug']}.json", f"books/{b['slug']}.svg"]
    for f in files:
        h.update(open(os.path.join(ROOT, f.split('?')[0]), 'rb').read())
    sw = open(os.path.join(ROOT, 'tools', 'sw.template.js'), encoding='utf-8').read()
    sw = sw.replace('__VERSION__', h.hexdigest()[:10]).replace('__FILES__', json.dumps(['./'] + ['./' + f for f in files]))
    open(os.path.join(ROOT, 'sw.js'), 'w', encoding='utf-8').write(sw)
    for b in books:
        print(f"{b['slug']}: {b['words']} words, {b['chapters']} chapters")


if __name__ == '__main__':
    main()
