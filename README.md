# lingualira

Web-based e-reader for foreign language learning, with abridged versions of public-domain books.
Every word can be tapped for a translation.
It's a static site on GitHub Pages and can be added to an iPhone home screen as a full-screen app.

## How it fits together

| Path | What it is |
|---|---|
| `index.html` | Library home page; lists the books in `books/index.json` |
| `reader.html?b=<slug>` | One shared reader for every book |
| `style.css` | Shared styles |
| `books/` | **Generated.** One `<slug>.json` and `<slug>.svg` per book, plus `index.json` |
| `books-src/<slug>/` | **Source** for each book (edit these) |
| `tools/build.py` | Turns `books-src/` into `books/` and refreshes the offline cache list in `sw.js` |

Progress and saved words are kept per book in the browser on your device.

## Adding a book

1. Make a folder `books-src/<slug>/` (lowercase, hyphens, for example `le-petit-prince`) containing:
   - `meta.json`: `title`, `author`, `year`, `blurb`, `level`, `colour` (cover band colour), `added` (YYYY-MM-DD; newest shows first)
   - `content.py`: `CHAPTERS = [{"fr": title, "en": title, "paras": [[(french, english), ...], ...]}, ...]`
   - `dict.txt`: `form|English gloss|grammar note`, one line per word form used in the text
   - `phrases.txt`: `d' abord|first`, with tokens split by spaces and elisions kept as their own token
   - `cover.svg`: cover picture, about 420×200
2. Run `python3 tools/build.py`. It stops and lists any word missing from `dict.txt`.
3. Commit and push. The book appears in the library within a minute or two.

Copy `books-src/micromegas/` as a starting point.
