# pdfsplitter

Slice a large, well-bookmarked PDF into one file per chapter **without breaking
the internal hyperlinks**. Cross-references that used to jump within the single
big file are rewritten to jump into the correct sibling file instead; links that
stay inside a chapter keep working as ordinary in-document jumps.

```console
$ pdfsplitter split manual.pdf -o out/
opening manual.pdf
  1240 pages, 3811 named destinations
  57 output files planned
  6032 internal links mapped
  [ 0] 00_Front_Matter.pdf                              p1-18     0.9 MB  in:0 out:12
  [ 1] 01_Introduction.pdf                              p19-44    2.1 MB  in:88 out:41
  ...
  [ +] 00_Contents.pdf                                  312 entries

done: 4901 intra-file links, 1131 cross-file links
```

## Why the link work is not optional

A link annotation stores its target as an indirect reference to a page object,
or as a name looked up in the document-wide `/Names/Dests` tree. When a page is
copied into a new document, qpdf replaces references to pages that did not come
along with `null`, and the name tree is not copied at all. A naive page-range
split therefore yields chapters in which _every_ internal link is dead — and
dead silently: the annotation is still there, still clickable, and simply does
nothing.

`pdfsplitter` captures every link target from the intact original **before any
page is copied**, resolving all three destination encodings found in the wild
(a bare `/Dest` array, a named `/Dest` looked up in `/Names/Dests`, and an
`/A << /S /GoTo /D … >>` action). It then:

1. Treats each top-level bookmark as a chapter; pages before the first chapter
   become a "Front Matter" file.
2. Writes one PDF per chapter. In-chapter links stay `/GoTo`; links that leave
   the chapter become `/GoToR` (remote go-to) actions pointing at the sibling
   file and a page index inside it. `/URI` links are left alone.
3. Rebuilds each chapter's slice of the bookmark tree, sets `/PageLabels` so the
   viewer shows the original manual's page numbers, and generates a
   `00_Contents.pdf` whose entries are `/GoToR` links into the chapter files.

## Install

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/). `uv tool install`
puts `pdfsplitter` and `pdfsplitter-verify` on your PATH in an isolated
environment.

```sh
git clone https://github.com/MattLewin/pdfsplitter.git
cd pdfsplitter
uv tool install .
uv tool update-shell        # once, if uv says your PATH needs it
```

Install straight from the Git URL, without cloning:

```sh
uv tool install git+https://github.com/MattLewin/pdfsplitter.git
```

### Run without installing

From inside the repo:

```sh
uvx --from . pdfsplitter outline manual.pdf
```

## Usage

### Inspect the outline first

```sh
pdfsplitter outline manual.pdf
```

Lists the top-level bookmarks and the page each resolves to — this is the
chapter plan `split` will use. If entries show `p?`, their destinations could
not be resolved and those chapters will not be cut.

### Split

```sh
pdfsplitter split manual.pdf -o out/
```

| Flag                | Meaning                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `-o`, `--out DIR`   | output directory (default `chapters/`)                               |
| `--title TEXT`      | heading + PDF title for the generated table of contents              |
| `--toc-depth N`     | bookmark depth to include in `00_Contents.pdf` (default 2)           |
| `--new-window`      | cross-file links open a new window instead of replacing the document |
| `--no-front-matter` | do not emit the pre-first-chapter "Front Matter" file                |
| `--no-contents`     | do not generate `00_Contents.pdf`                                    |
| `--no-page-labels`  | do not renumber pages to match the original                          |

### Verify

`pdfsplitter-verify` independently re-derives every link target from the
original and checks that each link in the output still resolves to the same
physical page of the original manual. Exits non-zero on any mismatch.

```sh
pdfsplitter-verify manual.pdf out/
```

## Development

```sh
mise install                 # Python 3.14 + uv, pinned in .mise.toml
uv sync                      # create .venv with dev tools (black, ruff, mdformat, pre-commit)
uvx pre-commit install       # enable the git hook
uvx pre-commit run --all-files
```

On every commit, pre-commit runs `ruff --fix`, then `black`, then `prettier`,
then `mdformat` on Markdown (config in `.pre-commit-config.yaml` and
`.mdformat.toml`). `CLAUDE.md` is excluded from `mdformat`.

## License

MIT — see [LICENSE](LICENSE).
