"""Record natural French audio for a book with Gemini TTS.

  python3 tools/tts.py book <slug>    record every sentence and every word of books/<slug>.json
  python3 tools/tts.py index <slug>   rebuild the audio index files from what is on disk

Output (served by GitHub Pages, played by reader.html):
  audio/<slug>/<chapter>-<part>.mp3              narration of a whole chapter (or part of a long one),
                                                  "steady audiobook narration", one request each
  audio/<slug>/<chapter>-<part>.json              where each sentence starts and ends in it
  audio/<slug>/index.json                         sentence id "c-p-s" -> [file, start, end]
  audio/words/<name>.mp3                          one file per word form, shared by all books
  audio/words/index.json                          word (lowercase, elisions joined: "l'herbe") -> file

Narration is recorded a chapter at a time so the intonation flows from sentence to sentence;
sentence timings come from the pauses, matched against each sentence's length. Words are recorded WORD_BATCH at a time as a spoken list,
then cut apart at the pauses; if the number of pieces doesn't match, the batch is split in
half and tried again, down to single words. This keeps a whole book to a few dozen requests.

Resumable: files that already exist are skipped. A daily quota error stops the run at once
(everything finished so far is kept); per-minute limits are waited out.
Needs GEMINI_API_KEY and ffmpeg (see .github/workflows/book-audio.yml). Standard library only.
"""
import base64, concurrent.futures as cf, json, os, re, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get('GEMINI_API_KEY', '').strip()
MODEL = os.environ.get('TTS_MODEL', 'gemini-3.8-flash-tts').strip()
VOICE = os.environ.get('TTS_VOICE', 'Iapetus').strip()
LANG = os.environ.get('TTS_LANG', 'fr-FR').strip()
WORKERS = int(os.environ.get('TTS_WORKERS') or '2')
WORD_BATCH = int(os.environ.get('TTS_WORD_BATCH') or '25')
CHUNK_WORDS = int(os.environ.get('TTS_CHUNK_WORDS') or '450')  # longest stretch of narration per request
LIMIT = int(os.environ.get('TTS_LIMIT') or '0')  # stop after this many requests (0 = no limit); for test runs

# Gemini 3.8 TTS reads its text verbatim, so delivery notes go in speech_metadata.style
STYLES = {
    'sentence': "steady audiobook narration",
    'word': "clear and careful, like a dictionary pronunciation of one word, spoken in French",
    'wordlist': ("a list of separate French words, each one clear and careful like a dictionary "
                 "pronunciation, with a long pause after every word"),
}
WORDS_DIR = os.path.join(ROOT, 'audio', 'words')
_lock = threading.Lock()
_requests = {'n': 0}


class Fatal(Exception):
    """Errors that will not fix themselves today (bad key, no credit, daily quota): stop the run."""


def request_body(text, kind):
    speech = {'voiceConfig': {'voice': VOICE}}
    if LANG:
        speech['languageCode'] = LANG  # stops short words like "destin" being read as English
    part = {'text': text, 'speech_metadata': {'style': STYLES[kind]}}
    return {'contents': [{'role': 'user', 'parts': [part]}],
            'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': speech}}


def describe_error(raw):
    """Google's error as one readable line, including which quota was hit and any suggested wait."""
    try:
        err = json.loads(raw)['error']
    except Exception:
        return raw[:1500], [], None
    quotas, wait = [], None
    for d in err.get('details', []):
        for v in d.get('violations', []):
            q = v.get('quotaId') or v.get('quotaMetric')
            if q:
                quotas.append(f"{q} (limit {v.get('quotaValue', '?')})")
        if 'retryDelay' in d:
            try:
                wait = float(str(d['retryDelay']).rstrip('s'))
            except ValueError:
                pass
    msg = err.get('message', '').split('\n')[0]
    return f"{err.get('code')} {err.get('status', '')}: {msg} {'; '.join(quotas)}".strip(), quotas, wait


def synth(text, kind):
    if LIMIT and _requests['n'] >= LIMIT:
        raise Fatal(f'request limit of {LIMIT} for this run reached')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
    body = json.dumps(request_body(text, kind)).encode()
    data = None
    for attempt in range(6):
        with _lock:
            _requests['n'] += 1
        req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'x-goog-api-key': KEY})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                data = json.load(r)
            part = data['candidates'][0]['content']['parts'][0]['inlineData']
            return base64.b64decode(part['data']), part.get('mimeType', '')
        except urllib.error.HTTPError as e:
            line, quotas, wait = describe_error(e.read().decode(errors='replace'))
            if e.code in (401, 402, 403):
                raise Fatal(line)
            if e.code == 429:
                if any(re.search(r'day|daily', q, re.I) for q in quotas):
                    raise Fatal('daily limit reached: ' + line)
                if attempt >= 3:
                    raise Fatal('still over quota after waiting: ' + line)
                print(f'  waiting {int(wait or 60)}s: {line}', flush=True)
                time.sleep((wait or 60) + 2)
                continue
            if e.code in (408, 409, 500, 502, 503, 504) and attempt < 5:
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(line)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(str(e))
        except (KeyError, IndexError, TypeError):
            if attempt < 2:
                continue
            raise RuntimeError(f'no audio in response: {str(data)[:200]}')


TRIM = ('silenceremove=start_periods=1:start_threshold=-50dB,areverse,'
        'silenceremove=start_periods=1:start_threshold=-50dB,areverse,apad=pad_dur=0.08')


def to_wav(audio, mime):
    """Gemini 3.8 returns WAV (older models raw 16-bit PCM). Returns a path to a 24 kHz mono WAV."""
    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as f:
        f.write(audio)
        src = f.name
    if audio[:4] == b'RIFF' or 'wav' in mime:
        inp = ['-i', src]
    else:
        rate = re.search(r'rate=(\d+)', mime)
        inp = ['-f', 's16le', '-ar', rate.group(1) if rate else '24000', '-ac', '1', '-i', src]
    out = src[:-4] + '.wav'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', *inp, '-ac', '1', '-ar', '24000', out], check=True)
    os.unlink(src)
    return out


def encode(wav, out, start=None, end=None, trim=True):
    """Cut [start, end] from a WAV (if given), trim silence, write mono MP3 atomically."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # cut as an input option, so the silence trim below only sees this piece (cutting after the
    # filter shifts every timestamp by however much silence was removed)
    cut = ['-ss', f'{start:.3f}', '-to', f'{end:.3f}'] if start is not None else []
    tmp = out[:-4] + '.part.mp3'
    flt = ['-af', TRIM] if trim else []
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', *cut, '-i', wav, *flt, '-ac', '1', '-ar', '24000',
                    '-codec:a', 'libmp3lame', '-b:a', '48k', tmp], check=True)
    os.replace(tmp, out)


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout
    try:
        return float(out)
    except ValueError:
        return 0.0


def speech_spans(wav, noise_db, min_gap):
    """Stretches of speech between silences of at least min_gap seconds."""
    log = subprocess.run(['ffmpeg', '-hide_banner', '-i', wav, '-af', f'silencedetect=noise={noise_db}dB:d={min_gap}',
                          '-f', 'null', '-'], capture_output=True, text=True).stderr
    total = duration(wav)
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', log)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', log)]
    spans, pos = [], 0.0
    for s, e in zip(starts, ends + [total] * (len(starts) - len(ends))):
        if s - pos > 0.12:
            spans.append((pos, s))
        pos = e
    if total - pos > 0.12:
        spans.append((pos, total))
    return spans


def split_words(wav, n):
    """Find settings that cut the list into exactly n plausible pieces, or None."""
    for noise in (-40, -35, -45, -30):
        for gap in (0.35, 0.28, 0.45, 0.22, 0.6):
            spans = speech_spans(wav, noise, gap)
            if len(spans) == n and all(0.15 <= e - s <= 3.5 for s, e in spans):
                return spans
    return None


def word_file(key):
    """Safe, readable file name for a word: accents kept, apostrophes and odd characters replaced."""
    name = re.sub(r"[^0-9a-zàâäçéèêëîïôöùûüÿœæ-]+", '_', key.lower()).strip('_') or 'w'
    return name[:80] + '.mp3'


def book_items(slug):
    """Sentences as (id, text) and words as (key, text) for one book, from the built books/<slug>.json."""
    data = json.load(open(os.path.join(ROOT, 'books', slug + '.json'), encoding='utf-8'))
    sentences, words = [], {}
    for c, ch in enumerate(data['book']):
        for pi, para in enumerate(ch['p']):
            for si, (segs, _en) in enumerate(para):
                text = ''.join(x[0] if isinstance(x, list) else x for x in segs).strip()
                sentences.append((f'{c}-{pi}-{si}', text))
                toks = [x[0] for x in segs if isinstance(x, list)]
                for i, t in enumerate(toks):
                    if t.endswith("'"):  # elided: say it joined to the next word, as the reader does
                        t = t + (toks[i + 1] if i + 1 < len(toks) else '')
                    words.setdefault(t.lower(), t)
    return sentences, sorted(words.items())


def narration_chunks(slug):
    """Each chapter as one chunk, or several if it is long: whole paragraphs, up to CHUNK_WORDS words."""
    data = json.load(open(os.path.join(ROOT, 'books', slug + '.json'), encoding='utf-8'))
    chunks = []
    for c, ch in enumerate(data['book']):
        cur, words, k = [], 0, 0
        for pi, para in enumerate(ch['p']):
            sents = [(f'{c}-{pi}-{si}', ''.join(x[0] if isinstance(x, list) else x for x in segs).strip())
                     for si, (segs, _en) in enumerate(para)]
            n = sum(len(t.split()) for _, t in sents)
            if cur and words + n > CHUNK_WORDS:
                chunks.append({'name': f'{c}-{k}', 'paras': cur})
                cur, words, k = [], 0, k + 1
            cur.append(sents)
            words += n
        if cur:
            chunks.append({'name': f'{c}-{k}', 'paras': cur})
    for ch in chunks:
        ch['sentences'] = [s for p in ch['paras'] for s in p]
        ch['text'] = '\n\n'.join(' '.join(t for _, t in p) for p in ch['paras'])
    return chunks


def trimmed(wav):
    """Copy of a WAV with leading and trailing silence removed (timings are measured on this)."""
    out = wav[:-4] + '.trim.wav'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', wav, '-af', TRIM, out], check=True)
    return out


def silences(wav, noise_db=-38, min_gap=0.12):
    log = subprocess.run(['ffmpeg', '-hide_banner', '-i', wav, '-af', f'silencedetect=noise={noise_db}dB:d={min_gap}',
                          '-f', 'null', '-'], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', log)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', log)]
    return [(st, en) for st, en in zip(starts, ends) if en - st >= min_gap]


def align(wav, texts):
    """Start and end of each sentence in a narration. Picks, in order, the pauses that best match
    where each sentence should end given its length, preferring longer pauses (full stops pause
    longer than commas). Falls back to proportional timing if there aren't enough pauses."""
    total, n = duration(wav), len(texts)
    if n == 1:
        return [(0.0, total)]
    lens = [max(len(t), 1) for t in texts]
    cum = [sum(lens[:i + 1]) / sum(lens) * total for i in range(n - 1)]  # proportional fallback
    cand = silences(wav)
    m = len(cand)
    if m < n - 1:
        cuts = [(e, e) for e in cum]
    else:
        # choose n-1 pauses in order so that each sentence's duration matches its length
        # (chars x average speed), preferring long pauses; dynamic programming over the pauses
        rate = total / sum(lens)
        mids = [(st + en) / 2 for st, en in cand]
        gap = [min(en - st, 1.2) for st, en in cand]

        def fit(dur, i):
            exp = lens[i] * rate
            return 4 * ((dur - exp) / max(exp, 1.0)) ** 2

        INF = float('inf')
        best = [[INF] * m for _ in range(n - 1)]
        back = [[-1] * m for _ in range(n - 1)]
        for j in range(m):
            best[0][j] = fit(mids[j], 0) - 1.5 * gap[j]
        for i in range(1, n - 1):
            for j in range(i, m):
                bj, arg = INF, -1
                for k in range(i - 1, j):
                    if best[i - 1][k] < INF:
                        v = best[i - 1][k] + fit(mids[j] - mids[k], i)
                        if v < bj:
                            bj, arg = v, k
                if arg >= 0:
                    best[i][j] = bj - 1.5 * gap[j]
                    back[i][j] = arg
        j = min(range(m), key=lambda x: best[n - 2][x] + fit(total - mids[x], n - 1))
        picked = []
        for i in range(n - 2, -1, -1):
            picked.append(j)
            j = back[i][j]
        cuts = [cand[j] for j in reversed(picked)]
    spans, start = [], 0.0
    for st, en in cuts:
        spans.append((round(start, 3), round(min(en, st + 0.05), 3)))
        start = max(st, en - 0.05)
    spans.append((round(start, 3), round(total, 3)))
    return spans


def write_indexes(slug):
    sentences, _ = book_items(slug)
    sdir = os.path.join(ROOT, 'audio', slug)
    timing = {}
    if os.path.isdir(sdir):
        for f in sorted(os.listdir(sdir)):
            if re.fullmatch(r'\d+-\d+\.json', f) and os.path.exists(os.path.join(sdir, f[:-5] + '.mp3')):
                for sid, st, en in json.load(open(os.path.join(sdir, f), encoding='utf-8')):
                    timing[sid] = [f[:-5] + '.mp3', st, en]
        json.dump({'voice': VOICE, 'model': MODEL, 'sentences': timing, 'total': len(sentences)},
                  open(os.path.join(sdir, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False,
                  separators=(',', ':'))
    have = len(timing)
    # the words index covers every book, so rebuild it from all of them
    idx = {}
    for s in sorted(os.listdir(os.path.join(ROOT, 'books'))):
        if s.endswith('.json') and s != 'index.json':
            for key, _ in book_items(s[:-5])[1]:
                f = word_file(key)
                if os.path.exists(os.path.join(WORDS_DIR, f)):
                    idx[key] = f
    if idx:
        os.makedirs(WORDS_DIR, exist_ok=True)
        json.dump(idx, open(os.path.join(WORDS_DIR, 'index.json'), 'w', encoding='utf-8'),
                  ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    print(f'{slug}: {have}/{len(sentences)} sentences narrated; {len(idx)} words recorded across all books')


def record_book(slug):
    sentences, words = book_items(slug)
    sdir = os.path.join(ROOT, 'audio', slug)
    chunks = narration_chunks(slug)
    s_todo = [ch for ch in chunks if not (os.path.exists(os.path.join(sdir, ch['name'] + '.mp3')) and
                                         os.path.exists(os.path.join(sdir, ch['name'] + '.json')))]
    w_todo = [(k, t) for k, t in words if not os.path.exists(os.path.join(WORDS_DIR, word_file(k)))]
    batches = [w_todo[i:i + WORD_BATCH] for i in range(0, len(w_todo), WORD_BATCH)]
    print(f'{slug}: {len(chunks)} narration parts ({len(s_todo)} to do), {len(words)} words ({len(w_todo)} to do, '
          f'{len(batches)} batches of up to {WORD_BATCH}) with {MODEL} / {VOICE}, {WORKERS} at a time', flush=True)
    st = {'sent': 0, 'words': 0, 'seconds': 0.0, 'splits': 0}
    failed, stop = [], threading.Event()

    def guard(fn, label):
        def run(item):
            if stop.is_set():
                return
            try:
                fn(item)
            except Fatal as e:
                if not stop.is_set():
                    print(f'STOPPING: {e}', flush=True)
                stop.set()
                with _lock:
                    failed.append((label(item), str(e)))
            except Exception as e:
                with _lock:
                    failed.append((label(item), str(e)))
                    print(f'  ERR {label(item)[:50]}: {e}', flush=True)
        return run

    def do_chunk(ch):
        audio, mime = synth(ch['text'], 'sentence')
        raw = to_wav(audio, mime)
        wav = trimmed(raw)
        spans = align(wav, [t for _, t in ch['sentences']])
        base = os.path.join(sdir, ch['name'])
        encode(wav, base + '.mp3', trim=False)
        tmp = base + '.json.part'
        json.dump([[sid, a, b] for (sid, _), (a, b) in zip(ch['sentences'], spans)], open(tmp, 'w'))
        os.replace(tmp, base + '.json')
        secs = duration(base + '.mp3')
        os.unlink(raw)
        os.unlink(wav)
        words_in = len(ch['text'].split())
        long = secs > words_in * 0.75 + 10
        with _lock:
            st['sent'] += 1
            st['seconds'] += secs
            print(f"  narration {st['sent']}/{len(s_todo)}  part {ch['name']}: {len(ch['sentences'])} sentences, "
                  f"{words_in} words, {secs:5.1f}s{'  <-- long, check it' if long else ''}", flush=True)

    def do_words(batch):
        if stop.is_set():
            return
        if len(batch) == 1:
            key, text = batch[0]
            audio, mime = synth(text, 'word')
            wav = to_wav(audio, mime)
            spans = [None]
        else:
            audio, mime = synth('. <short pause> '.join(t for _, t in batch) + '.', 'wordlist')
            wav = to_wav(audio, mime)
            spans = split_words(wav, len(batch))
            if spans is None:  # pieces didn't line up with the words: halve and retry
                os.unlink(wav)
                with _lock:
                    st['splits'] += 1
                half = len(batch) // 2
                do_words(batch[:half])
                do_words(batch[half:])
                return
        for (key, _), span in zip(batch, spans):
            out = os.path.join(WORDS_DIR, word_file(key))
            if span:
                encode(wav, out, max(0, span[0] - 0.05), span[1] + 0.05)
            else:
                encode(wav, out)
            with _lock:
                st['seconds'] += duration(out)
        os.unlink(wav)
        with _lock:
            st['words'] += len(batch)
            print(f"  words {st['words']}/{len(w_todo)}  (…{batch[-1][1]})", flush=True)

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(guard(do_chunk, lambda ch: 'part ' + ch['name']), s_todo))
        list(ex.map(guard(do_words, lambda b: f'{len(b)} words from {b[0][1]}'), batches))
    write_indexes(slug)
    est = st['seconds'] * 1.2 * 25 * 9 / 1e6
    print(f"made {st['sent']} narration parts and {st['words']} words with {_requests['n']} requests "
          f"({st['splits']} word batches had to be split); {st['seconds'] / 60:.1f} min of audio, "
          f"about ${est:.2f}; {len(failed)} problems", flush=True)
    for what, err in failed[:10]:
        print(f'  problem: {what}: {err}')
    if stop.is_set():
        sys.exit('Stopped early. Everything finished so far is saved; run the job again later to continue.')
    if failed:
        print('Run the job again to retry what failed (finished items are skipped).')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('book', 'index'):
        sys.exit(__doc__)
    slug = sys.argv[2]
    if not os.path.exists(os.path.join(ROOT, 'books', slug + '.json')):
        sys.exit(f'No book called {slug}. Choose one of: ' + ', '.join(
            f[:-5] for f in sorted(os.listdir(os.path.join(ROOT, 'books'))) if f.endswith('.json') and f != 'index.json'))
    if sys.argv[1] == 'index':
        write_indexes(slug)
    else:
        if not KEY:
            sys.exit('Set GEMINI_API_KEY first.')
        record_book(slug)
