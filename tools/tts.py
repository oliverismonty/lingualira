"""Record natural French audio for a book with Gemini TTS.

  python3 tools/tts.py book <slug>    record every sentence and every word of books/<slug>.json
  python3 tools/tts.py index <slug>   rebuild the audio index files from what is on disk

Output (served by GitHub Pages, played by reader.html):
  audio/<slug>/<chapter>-<para>-<sentence>.mp3   one file per sentence, "steady audiobook narration"
  audio/<slug>/index.json                         which sentences exist
  audio/words/<name>.mp3                          one file per word form, shared by all books
  audio/words/index.json                          word (lowercase, elisions joined: "l'herbe") -> file

Resumable: files that already exist are skipped, so a stopped run can simply be started again.
Needs GEMINI_API_KEY and ffmpeg (see .github/workflows/book-audio.yml). Standard library only.
"""
import base64, concurrent.futures as cf, json, os, re, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get('GEMINI_API_KEY', '').strip()
MODEL = os.environ.get('TTS_MODEL', 'gemini-3.8-flash-tts').strip()
VOICE = os.environ.get('TTS_VOICE', 'Iapetus').strip()
LANG = os.environ.get('TTS_LANG', 'fr-FR').strip()
WORKERS = int(os.environ.get('TTS_WORKERS', '4'))
LIMIT = int(os.environ.get('TTS_LIMIT') or '0')  # stop after this many new clips (0 = no limit); for test runs

# Gemini 3.8 TTS reads its text verbatim, so delivery notes go in speech_metadata.style
SENTENCE_STYLE = "steady audiobook narration"
WORD_STYLE = "clear and careful, like a dictionary pronunciation of one word, spoken in French"
WORDS_DIR = os.path.join(ROOT, 'audio', 'words')
_lock = threading.Lock()


def request_body(text, kind):
    speech = {'voiceConfig': {'voice': VOICE}}
    if LANG:
        speech['languageCode'] = LANG  # stops short words like "destin" being read as English
    part = {'text': text, 'speech_metadata': {'style': SENTENCE_STYLE if kind == 'sentence' else WORD_STYLE}}
    return {'contents': [{'role': 'user', 'parts': [part]}],
            'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': speech}}


class Fatal(Exception):
    """Errors that will not fix themselves (bad key, no credit): stop the whole run."""


def synth(text, kind):
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
    body = json.dumps(request_body(text, kind)).encode()
    data = None
    for attempt in range(6):
        req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'x-goog-api-key': KEY})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            part = data['candidates'][0]['content']['parts'][0]['inlineData']
            return base64.b64decode(part['data']), part.get('mimeType', '')
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:300].replace('\n', ' ')
            if e.code in (401, 402, 403):
                raise Fatal(f'HTTP {e.code}: {msg}')
            if e.code in (408, 409, 429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f'HTTP {e.code}: {msg}')
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 5:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(str(e))
        except (KeyError, IndexError, TypeError):
            if attempt < 2:
                continue
            raise RuntimeError(f'no audio in response: {str(data)[:200]}')


def to_mp3(audio, mime, out):
    """Gemini 3.8 returns WAV (older models raw 16-bit PCM). Store trimmed mono MP3."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as f:
        f.write(audio)
        src = f.name
    if audio[:4] == b'RIFF' or 'wav' in mime:
        inp = ['-i', src]
    else:
        rate = re.search(r'rate=(\d+)', mime)
        inp = ['-f', 's16le', '-ar', rate.group(1) if rate else '24000', '-ac', '1', '-i', src]
    flt = ('silenceremove=start_periods=1:start_threshold=-50dB,areverse,'
           'silenceremove=start_periods=1:start_threshold=-50dB,areverse,apad=pad_dur=0.08')
    tmp = out[:-4] + '.part.mp3'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', *inp, '-af', flt, '-ac', '1', '-ar', '24000',
                    '-codec:a', 'libmp3lame', '-b:a', '48k', tmp], check=True)
    os.replace(tmp, out)  # only complete files ever appear under their real name
    os.unlink(src)


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout
    try:
        return float(out)
    except ValueError:
        return 0.0


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


def write_indexes(slug):
    sentences, _ = book_items(slug)
    sdir = os.path.join(ROOT, 'audio', slug)
    have = [sid for sid, _ in sentences if os.path.exists(os.path.join(sdir, sid + '.mp3'))]
    if os.path.isdir(sdir) or have:
        os.makedirs(sdir, exist_ok=True)
        json.dump({'voice': VOICE, 'model': MODEL, 'sentences': have, 'total': len(sentences)},
                  open(os.path.join(sdir, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False)
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
    print(f'{slug}: {len(have)}/{len(sentences)} sentences recorded; {len(idx)} words recorded across all books')


def record_book(slug):
    sentences, words = book_items(slug)
    jobs = [('sentence', sid, text, os.path.join(ROOT, 'audio', slug, sid + '.mp3')) for sid, text in sentences]
    jobs += [('word', key, text, os.path.join(WORDS_DIR, word_file(key))) for key, text in words]
    todo = [j for j in jobs if not os.path.exists(j[3])]
    if LIMIT:
        todo = todo[:LIMIT]
    print(f'{slug}: {len(sentences)} sentences, {len(words)} words; {len(jobs) - len(todo)} already recorded, '
          f'{len(todo)} to do with {MODEL} / {VOICE}', flush=True)
    state = {'done': 0, 'seconds': 0.0}
    failed, stop = [], threading.Event()

    def work(job):
        kind, key, text, out = job
        if stop.is_set():
            return
        try:
            audio, mime = synth(text, kind)
            to_mp3(audio, mime, out)
            secs = duration(out)
            long = (kind == 'word' and secs > 4) or secs > len(text) * 0.12 + 4
            with _lock:
                state['done'] += 1
                state['seconds'] += secs
                n = state['done']
                if long or n % 25 == 0 or n == len(todo):
                    print(f'  {n}/{len(todo)}  {secs:5.1f}s  {text[:60]}{"  <-- long, check it" if long else ""}',
                          flush=True)
        except Fatal as e:
            stop.set()
            with _lock:
                failed.append((key, str(e)))
        except Exception as e:
            with _lock:
                failed.append((key, str(e)))
                print(f'  ERR {text[:50]}: {e}', flush=True)

    with cf.ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, todo))
    write_indexes(slug)
    est = state['seconds'] * 25 * 9 / 1e6
    print(f"recorded {state['done']} clips, {state['seconds'] / 60:.1f} min of audio "
          f"(about ${est:.2f} at $9 per million audio tokens); {len(failed)} failed", flush=True)
    for key, err in failed[:10]:
        print(f'  failed: {key}: {err}')
    if stop.is_set():
        sys.exit(f'Stopped early: {failed[-1][1]}')
    if failed:
        print('Run the job again to retry the failed clips (finished ones are skipped).')


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
