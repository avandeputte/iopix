# iopix.net

The iopix website: a hand-written home page (`home/`, with the screenshots taken from `../docs/screenshots`) and the documentation (`docs/`, MkDocs Material) served at `/docs/`.

```sh
pip install mkdocs==1.6.1 mkdocs-material==9.7.7
./build.sh            # writes dist/
python3 -m http.server -d dist 8000
```

Hosted on Cloudflare Pages (project `iopix`), deployed from `dist/`.
