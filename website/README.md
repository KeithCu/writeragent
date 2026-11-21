# WriterAgent.org website

Flask + HTML + CSS site for [writeragent.org](https://writeragent.org).

## Run locally

```bash
cd website
pip install -r requirements.txt
flask --app app run
```

Open http://127.0.0.1:5000/

## Deploy

Use any WSGI server and point the domain to it. Example with gunicorn:

```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8000 "app:app"
```

Then point **writeragent.org** at your host (e.g. nginx reverse proxy to port 8000, or a PaaS like Render/Fly.io/Railway).

## Optional images

If you have `Sonnet46Spreadsheet.png` and `Sonnet46ArchDiagram.jpg` in the repo root (as referenced in the main README), copy them into `static/images/` so the home page screenshots and architecture diagram display:

```bash
cp ../Sonnet46Spreadsheet.png ../Sonnet46ArchDiagram.jpg static/images/
```

## Logo & favicons

`static/images/writeragent_logo.svg` is copied from `extension/assets/writeragent_logo.svg`; `favicon-32.png`, `favicon-48.png`, and `apple-touch-icon.png` are rendered from it via `rsvg-convert` and `magick`.
