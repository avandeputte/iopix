#!/bin/sh
# Builds iopix.net into dist/: the hand-written home page plus the MkDocs docs at /docs/.
# Needs: pip install mkdocs==1.6.1 mkdocs-material==9.7.7
set -e
cd "$(dirname "$0")"
rm -rf dist && mkdir -p dist
cp -R home/. dist/
# the screenshots are the README's, from docs/screenshots in this repository
mkdir -p dist/img && cp ../docs/screenshots/*.jpg dist/img/
cp 404.html robots.txt _headers dist/
python3 whatsnew.py
(cd docs && mkdocs build --quiet)
# docs pages show the home page's screenshots: make /img/ links relative so any host or subfolder works
python3 - <<'PY'
import pathlib
for f in pathlib.Path('dist/docs').rglob('*.html'):
    depth = len(f.relative_to('dist').parts) - 1
    t = f.read_text()
    f.write_text(t.replace('src="/img/', 'src="' + '../' * depth + 'img/'))
PY
cat > dist/sitemap.xml <<XML
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://iopix.net/</loc></url>
$(grep -o '<loc>[^<]*</loc>' dist/docs/sitemap.xml | sed 's/^/  <url>/; s/$/<\/url>/')
</urlset>
XML
echo "Built $(find dist -type f | wc -l | tr -d ' ') files into dist/"
