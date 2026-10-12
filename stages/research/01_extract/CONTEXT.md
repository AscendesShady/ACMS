# research / 01_extract

Turn the source PDFs into searchable text, 25 pages a file.

## Inputs

| Layer | File | Read |
|---|---|---|
| 3 | `references/transportation_engineering_books/*.pdf`, `references/transportation_engineering_papers/*.pdf` | the sources (this PC only) |
| 4 | `research/extract.sh` | nothing: just run it |

## Process

1. In Git Bash, from the repository root: `sh research/extract.sh`. It needs `pdftotext` and `pdfinfo`, and skips
   chunks already extracted.

## Outputs

- `references/pdf_text/<source>/pNNNN-NNNN.txt`. It becomes reference material (Layer 3) for every later stage
  and for citations; it stays on this PC, out of git.

## Done when

- Each new PDF has its folder of chunks. Next: `../02_read/CONTEXT.md`.
