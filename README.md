# lingualira

Web-based e-reader for foreign language learning, with abridged versions of public-domain books.
Every word can be tapped for a translation.
It's a static site on GitHub Pages and can be added to an iPhone home screen as a full-screen app.

## How it fits together

| Path | What it is |
|---|---|
| `index.html` | Library home page; lists the books in `books/index.json` |
| `reader.html?b=<slug>` | One shared reader for every book |
| `style.css` | Shared styles (paper `#fbfaf6` / charcoal `#1d1d1f`, vermilion accent, Literata) |
| `tools/icon.svg`, `tools/render_icon.py` | App icon source and renderer |
| `books/` | **Generated.** One `<slug>.json` and `<slug>.svg` per book, plus `index.json` |
| `books-src/<slug>/` | **Source** for each book (edit these) |
| `tools/build.py` | Turns `books-src/` into `books/` and refreshes the offline cache list in `sw.js` |

Progress (by sentence) and saved words are kept per book in the browser on your device. Pages are measured on the reader's actual screen and text size, so they never scroll and the page count changes with the device.

## Adding a book

1. Make a folder `books-src/<slug>/` (lowercase, hyphens, for example `le-petit-prince`) containing:
   - `meta.json`: `title`, `author`, `year`, `blurb`, `level`, `added` (YYYY-MM-DD; newest shows first)
   - `content.py`: `CHAPTERS = [{"fr": title, "en": title, "paras": [[(french, english), ...], ...]}, ...]`
   - `dict.txt`: `form|English gloss|grammar note`, one line per word form used in the text
   - `phrases.txt`: `d' abord|first`, with tokens split by spaces and elisions kept as their own token
   - `cover.svg`: cover picture, 420×200, line art in ivory `#f2f0eb` on charcoal `#1d1d1f` with one `#ff6a52` accent
2. Run `python3 tools/build.py`. It stops and lists any word missing from `dict.txt`, and warns about phrases that never appear in the text.
3. Commit and push. The book appears in the library within a minute or two.

Copy `books-src/micromegas/` as a starting point.

## Audio

Recorded with Gemini 3.8 Flash TTS (voice Iapetus) by the **Book audio** GitHub Action
(Actions tab → Book audio → Run workflow → choose the book). It needs the repository secret
`GEMINI_API_KEY`. `tools/tts.py` records each chapter as one narration (so the voice flows from
sentence to sentence) and finds every sentence's start and end in it; single words are recorded
25 at a time as a spoken list and cut apart at the pauses. Runs are resumable and stop cleanly
at the daily quota (resets 08:00 UK time).

In the reader, ▶ plays the book from the current page, highlighting the sentence and the word
being read and turning pages; tapping a word pauses it. Anything not recorded uses the
phone's own French voice.
