"""Generate natural French audio with Gemini TTS.

  python3 tools/tts.py taster     same 5 sentences and 10 words on each model in MODELS,
                                  written to audio/taster/ with an index.json for taster.html

Needs the GEMINI_API_KEY environment variable and ffmpeg (both are present in the
GitHub Actions workflow .github/workflows/tts-taster.yml). Standard library only.
"""
import base64, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get('GEMINI_API_KEY', '').strip()
VOICE = os.environ.get('TTS_VOICE', 'Iapetus')
MODELS = [m for m in os.environ.get('TTS_MODELS', 'gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview').split(',') if m]
PAUSE = float(os.environ.get('TTS_PAUSE', '6'))  # seconds between calls, to stay under free-tier rate limits

SENTENCE_PROMPT = ("Read the following French text aloud in a clear, natural voice with a standard "
                   "France French accent, at a slightly slow pace for a language learner. "
                   "Read only the text:\n\n{t}")
# Gemini 3.8 TTS reads its text verbatim, so delivery notes go in speech_metadata.style instead
SENTENCE_STYLE = "clear, natural and warm, slightly slow, for a language learner"
WORD_STYLE = "clear and careful, like a dictionary pronunciation of one word"
WORD_PROMPT = ("Say this single French word clearly, with a standard France French accent, "
               "as a dictionary would. Say only the word:\n\n{t}")

TASTER_SENTENCES = [
    "C'était une de ces jolies jeunes femmes qui semblent être nées, par une erreur du destin, dans une famille modeste.",
    "Quel étrange voyage ! Il avait pourtant si bien commencé.",
    "J'ai pris la carafe : elle était vide ! Elle était complètement vide !",
    "« Tu dis que tu as acheté une rivière de diamants pour remplacer la mienne ? »",
    "Si elle me l'avait demandé, je lui aurais tout avoué.",
]
TASTER_WORDS = ["destin", "rivière", "écureuil", "grenouille", "feuille",
                "surveillait", "aujourd'hui", "œuf", "heureux", "brûlé"]


def is_38(model):
    return '3.8' in model or model.startswith('gemini-4')


def request_body(model, text, kind):
    """3.8+: verbatim text plus speech_metadata.style. Older: one prompt with the directions inline."""
    if is_38(model):
        part = {'text': text, 'speech_metadata': {'style': SENTENCE_STYLE if kind == 'sentence' else WORD_STYLE}}
        voice = {'voice': VOICE}
    else:
        part = {'text': (SENTENCE_PROMPT if kind == 'sentence' else WORD_PROMPT).format(t=text)}
        voice = {'prebuiltVoiceConfig': {'voiceName': VOICE}}
    return {'contents': [{'role': 'user', 'parts': [part]}],
            'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': {'voiceConfig': voice}}}


def synth(model, text, kind):
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
    body = json.dumps(request_body(model, text, kind)).encode()
    for attempt in range(5):
        req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'x-goog-api-key': KEY})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            part = data['candidates'][0]['content']['parts'][0]['inlineData']
            return base64.b64decode(part['data']), part.get('mimeType', '')
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:300]
            if e.code in (429, 500, 503) and attempt < 4:
                wait = 20 * (attempt + 1)
                print(f'  {e.code}, retrying in {wait}s: {msg}', flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f'HTTP {e.code}: {msg}')
        except (KeyError, IndexError) as e:
            raise RuntimeError(f'unexpected response: {str(data)[:300]}')


def to_mp3(audio, mime, out):
    """Gemini returns raw 16-bit PCM (audio/L16;rate=24000) or a WAV file; store small mono MP3."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as f:
        f.write(audio)
        src = f.name
    if audio[:4] == b'RIFF' or 'wav' in mime:
        inp = ['-i', src]
    else:
        rate = re.search(r'rate=(\d+)', mime)
        inp = ['-f', 's16le', '-ar', rate.group(1) if rate else '24000', '-ac', '1', '-i', src]
    # trim leading/trailing silence so taps feel instant, then encode speech-quality MP3
    flt = 'silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse,apad=pad_dur=0.08'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', *inp, '-af', flt, '-ac', '1', '-ar', '24000',
                    '-codec:a', 'libmp3lame', '-b:a', '48k', out], check=True)
    os.unlink(src)


def slug(text):
    s = re.sub(r"[^a-z0-9àâäçéèêëîïôöùûüœ']+", '-', text.lower()).strip('-')
    return s.replace("'", '_')[:60]


def taster():
    outdir = os.path.join(ROOT, 'audio', 'taster')
    items = [{'id': f's{i+1}', 'kind': 'sentence', 'text': t} for i, t in enumerate(TASTER_SENTENCES)]
    items += [{'id': 'w-' + slug(w), 'kind': 'word', 'text': w} for w in TASTER_WORDS]
    for it in items:
        it['files'] = {}
    errors = {}
    for model in MODELS:
        print(f'== {model} ({VOICE})', flush=True)
        for it in items:
            rel = f"audio/taster/{model}/{it['id']}.mp3"
            try:
                audio, mime = synth(model, it['text'], it['kind'])
                to_mp3(audio, mime, os.path.join(ROOT, rel))
                it['files'][model] = rel
                secs = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                             os.path.join(ROOT, rel)], capture_output=True, text=True).stdout or 0)
                flag = '  <-- long, check it' if (it['kind'] == 'word' and secs > 3) or secs > len(it['text']) * 0.12 + 3 else ''
                print(f"  ok  {secs:5.1f}s  {it['text'][:50]}{flag}", flush=True)
            except Exception as e:
                errors.setdefault(model, str(e))
                print(f"  ERR {it['text'][:50]}: {e}", flush=True)
            time.sleep(PAUSE)
    os.makedirs(outdir, exist_ok=True)
    json.dump({'voice': VOICE, 'models': MODELS, 'items': items, 'errors': errors,
               'made': time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())},
              open(os.path.join(outdir, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    made = sum(len(it['files']) for it in items)
    print(f'{made} clips made, {len(errors)} model(s) with errors')
    if made == 0:
        sys.exit('No audio was made. Check the GEMINI_API_KEY secret and the errors above.')


if __name__ == '__main__':
    if not KEY:
        sys.exit('Set GEMINI_API_KEY first.')
    if sys.argv[1:] == ['taster']:
        taster()
    else:
        sys.exit(__doc__)
