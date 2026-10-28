# iopix.net

The iopix website: a hand-written home page (`home/`, with the screenshots taken from `../docs/screenshots`) and the documentation (`docs/`, MkDocs Material) served at `/docs/`.

```sh
pip install mkdocs==1.6.1 mkdocs-material==9.7.7
./build.sh            # writes dist/
python3 -m http.server -d dist 8000
```

Hosted on Cloudflare Pages (project `iopix`). `.github/workflows/site.yml` builds and publishes it on every push to main that touches `site/`, the screenshots or the version; the Cloudflare key is `CLOUDFLARE_API_TOKEN` (with `CLOUDFLARE_ACCOUNT_ID`) in the GitHub environment `iopix.net`, which only main may use. `functions/_middleware.js` redirects www.iopix.net to iopix.net. The What's new page is generated from the version commits by `whatsnew.py`.
