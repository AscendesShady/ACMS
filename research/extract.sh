#!/bin/sh
# Split every PDF into 25-page text chunks: research/pdf_text/<file stem>/p0001-0025.txt (pdftotext -layout).
cd "$(dirname "$0")/.."
for f in references/transportation_engineering_books/*.pdf references/transportation_engineering_papers/*.pdf; do
  stem=$(basename "$f" .pdf); out="research/pdf_text/$stem"; mkdir -p "$out"
  pages=$(pdfinfo "$f" | awk '/^Pages:/{print $2}')
  first=1
  while [ "$first" -le "$pages" ]; do
    last=$((first + 24)); [ "$last" -gt "$pages" ] && last=$pages
    name=$(printf "p%04d-%04d.txt" "$first" "$last")
    [ -s "$out/$name" ] || pdftotext -layout -enc UTF-8 -f "$first" -l "$last" "$f" "$out/$name" 2>/dev/null
    first=$((last + 1))
  done
done
echo extracted
