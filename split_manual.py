#!/usr/bin/env python3
"""
split_manual.py — slice a large, well-bookmarked PDF into one file per chapter
while keeping the internal hyperlinks working across the resulting files.

    uv run --with pikepdf --with reportlab split_manual.py outline  manual.pdf
    uv run --with pikepdf --with reportlab split_manual.py split    manual.pdf -o out/

What it does
------------
1.  Reads the PDF outline (bookmarks) and treats each top-level entry as a
    chapter. Pages before the first chapter become a "front matter" file.
2.  Resolves *every* link annotation in the document to an absolute page index,
    handling all three encodings in the wild: a bare /Dest array, a /Dest that
    is a name looked up in /Names/Dests, and an /A << /S /GoTo /D ... >> action.
3.  Writes one PDF per chapter. Links whose target lands in the same chapter
    stay as ordinary /GoTo links. Links that leave the chapter are rewritten as
    /GoToR (remote go-to) actions pointing at the sibling file and a page index
    inside it. /URI links are left alone.
4.  Rebuilds each chapter's slice of the bookmark tree, sets /PageLabels so the
    viewer shows the original manual's page numbers, and generates a
    00_Contents.pdf whose entries are /GoToR links into the chapter files.

Why the link work is not optional
---------------------------------
A link annotation stores its target as an indirect reference to a page object,
or as a name looked up in the document-wide /Names/Dests tree. When you copy a
page into a new document, qpdf replaces references to pages that did not come
along with null, and the name tree is not copied at all. So a naive page-range
split yields chapters in which *every* internal link is dead - including the
intra-chapter ones - and dead silently: the annotation is still there, still
has a clickable rectangle, and simply does nothing. Worse, the output no longer
contains enough information to work out where those links used to point.

The mapping therefore has to be captured from the intact original before any
page is copied, which is what collect_and_strip_links does.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

MAX_NAME_INDIRECTION = 8


# --------------------------------------------------------------------------
# destination resolution
# --------------------------------------------------------------------------


def build_name_dests(pdf: pikepdf.Pdf) -> dict[str, object]:
    """Collect named destinations from both the modern name tree and the
    legacy /Root/Dests dictionary."""
    out: dict[str, object] = {}
    root = pdf.Root
    try:
        if "/Names" in root and "/Dests" in root.Names:
            for key, value in pikepdf.NameTree(root.Names.Dests).items():
                out[str(key)] = value
    except Exception as exc:  # malformed name tree: keep going
        print(f"  warning: could not read /Names/Dests ({exc})", file=sys.stderr)
    try:
        if "/Dests" in root:
            for key, value in root.Dests.items():
                out[str(key).lstrip("/")] = value
    except Exception as exc:
        print(f"  warning: could not read /Root/Dests ({exc})", file=sys.stderr)
    return out


def sanitize_tail(items) -> list:
    """Copy the zoom/position part of a destination array into plain values that
    are safe to place in a different document."""
    tail = []
    for item in items:
        if isinstance(item, Name):
            tail.append(Name(str(item)))
        elif item is None:
            tail.append(None)
        else:
            try:
                tail.append(float(item) if "." in str(item) else int(item))
            except TypeError, ValueError:
                tail.append(None)
    return tail


def resolve_dest(dest, name_dests, page_index, depth: int = 0):
    """Return (absolute_page_index, tail) for any destination encoding, or None."""
    if dest is None or depth > MAX_NAME_INDIRECTION:
        return None

    if isinstance(dest, Dictionary):
        return resolve_dest(dest.get("/D"), name_dests, page_index, depth + 1)

    if isinstance(dest, (Name, String)):
        key = str(dest).lstrip("/")
        return resolve_dest(name_dests.get(key), name_dests, page_index, depth + 1)

    if isinstance(dest, Array):
        if len(dest) == 0:
            return None
        first = dest[0]
        if isinstance(first, Dictionary):  # indirect page object
            idx = page_index.get(first.objgen)
            return (idx, sanitize_tail(list(dest)[1:])) if idx is not None else None
        try:  # already a page number
            return int(first), sanitize_tail(list(dest)[1:])
        except TypeError, ValueError:
            return None
    return None


def dest_of_annot(annot):
    """Pull the GoTo destination out of a link annotation, whichever way it is
    encoded. Returns None for URI / GoToR / Launch / anything else."""
    if "/Dest" in annot:
        return annot.Dest
    action = annot.get("/A")
    if isinstance(action, Dictionary) and action.get("/S") == "/GoTo":
        return action.get("/D")
    return None


# --------------------------------------------------------------------------
# outline
# --------------------------------------------------------------------------


def read_outline(pdf, name_dests, page_index):
    """Flatten the bookmark tree into nested plain dicts."""
    root = pdf.Root.get("/Outlines")
    if root is None or "/First" not in root:
        return []

    def walk(node, depth=0):
        items, seen = [], set()
        current = node.get("/First")
        while current is not None and depth < 32:
            gen = current.objgen
            if gen in seen:
                break  # cyclic /Next, seen in the wild
            seen.add(gen)
            dest = current.get("/Dest")
            if dest is None:
                action = current.get("/A")
                if isinstance(action, Dictionary) and action.get("/S") == "/GoTo":
                    dest = action.get("/D")
            hit = resolve_dest(dest, name_dests, page_index)
            items.append(
                {
                    "title": str(current.get("/Title", "")).strip(),
                    "page": hit[0] if hit else None,
                    "tail": hit[1] if hit else [Name("/Fit")],
                    "children": walk(current, depth + 1),
                }
            )
            current = current.get("/Next")
        return items

    return walk(root)


def safe_filename(index: int, title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60] or "Chapter"
    return f"{index:02d}_{slug}.pdf"


def plan_chapters(outline, total_pages, front_matter=True):
    """Turn top-level outline entries into (title, start, stop, filename)."""
    tops = [it for it in outline if it["page"] is not None]
    if not tops:
        raise SystemExit(
            "No top-level bookmarks with resolvable destinations; "
            "run the 'outline' command and use --boundaries instead."
        )

    starts = []
    for item in tops:
        if starts and item["page"] <= starts[-1][1]:
            continue  # duplicate or out-of-order entry
        starts.append((item["title"], item["page"]))

    chapters = []
    if front_matter and starts[0][1] > 0:
        chapters.append({"title": "Front Matter", "start": 0, "stop": starts[0][1], "outline": []})
    for i, (title, start) in enumerate(starts):
        stop = starts[i + 1][1] if i + 1 < len(starts) else total_pages
        chapters.append({"title": title, "start": start, "stop": stop, "outline": [tops[i]]})

    for i, chapter in enumerate(chapters):
        chapter["filename"] = safe_filename(i, chapter["title"])
    return chapters


# --------------------------------------------------------------------------
# link rewriting
# --------------------------------------------------------------------------


def collect_and_strip_links(pdf, name_dests, page_index):
    """Record where every GoTo link points, then remove the destination from the
    source document so page copying cannot drag foreign pages along."""
    targets: dict[tuple[int, int], tuple[int, list]] = {}
    unresolved = 0
    for page_no, page in enumerate(pdf.pages):
        obj = page.obj
        if "/Annots" not in obj:
            continue
        for annot_no, annot in enumerate(obj.Annots):
            if not isinstance(annot, Dictionary) or annot.get("/Subtype") != "/Link":
                continue
            dest = dest_of_annot(annot)
            if dest is None:
                continue  # URI and friends: leave untouched
            hit = resolve_dest(dest, name_dests, page_index)
            if hit is None:
                unresolved += 1
            else:
                targets[(page_no, annot_no)] = hit
            if "/Dest" in annot:
                del annot["/Dest"]
            if "/A" in annot:
                del annot["/A"]
    return targets, unresolved


def make_gotor(filename: str, page_in_target: int, tail, new_window: bool):
    spec = Dictionary(Type=Name.Filespec, F=String(filename), UF=String(filename))
    return Dictionary(
        S=Name.GoToR, F=spec, D=Array([page_in_target] + list(tail)), NewWindow=bool(new_window)
    )


def chapter_of_page(chapters, page_no):
    for i, chapter in enumerate(chapters):
        if chapter["start"] <= page_no < chapter["stop"]:
            return i
    return None


# --------------------------------------------------------------------------
# per-chapter output
# --------------------------------------------------------------------------


def attach_outline(dst, items, base, span):
    """Rebuild the slice of the bookmark tree that belongs to this chapter."""

    def build(nodes):
        built = []
        for node in nodes:
            page = node["page"]
            if page is None or not (base <= page < base + span):
                # keep descendants that do land in range
                built.extend(build(node["children"]))
                continue
            entry = Dictionary(Title=String(node["title"]))
            entry.Dest = Array([dst.pages[page - base].obj] + list(node["tail"]))
            kids = build(node["children"])
            built.append((dst.make_indirect(entry), kids))
        return built

    def link(parent, built):
        count = 0
        previous = None
        for obj, kids in built:
            obj.Parent = parent
            if previous is not None:
                previous.Next = obj
                obj.Prev = previous
            previous = obj
            count += 1
            if kids:
                sub = link(obj, kids)
                obj.Count = -sub  # negative = collapsed
        if built:
            parent.First = built[0][0]
            parent.Last = built[-1][0]
        return count

    tree = build(items)
    if not tree:
        return
    root = dst.make_indirect(Dictionary(Type=Name.Outlines))
    root.Count = link(root, tree)
    dst.Root.Outlines = root
    dst.Root.PageMode = Name.UseOutlines


def write_chapter(src, chapters, index, targets, outdir, new_window, page_labels):
    chapter = chapters[index]
    base, stop = chapter["start"], chapter["stop"]
    dst = pikepdf.Pdf.new()
    for page_no in range(base, stop):
        dst.pages.append(src.pages[page_no])

    internal = external = dropped = 0
    for local, page_no in enumerate(range(base, stop)):
        obj = dst.pages[local].obj
        if "/B" in obj:
            del obj["/B"]  # article beads can span chapters
        if "/Annots" not in obj:
            continue
        for annot_no, annot in enumerate(obj.Annots):
            hit = targets.get((page_no, annot_no))
            if hit is None:
                continue
            target_page, tail = hit
            if base <= target_page < stop:
                annot.A = dst.make_indirect(
                    Dictionary(
                        S=Name.GoTo, D=Array([dst.pages[target_page - base].obj] + list(tail))
                    )
                )
                internal += 1
            else:
                other = chapter_of_page(chapters, target_page)
                if other is None:
                    dropped += 1
                    continue
                annot.A = dst.make_indirect(
                    make_gotor(
                        chapters[other]["filename"],
                        target_page - chapters[other]["start"],
                        tail,
                        new_window,
                    )
                )
                external += 1

    attach_outline(dst, chapter["outline"], base, stop - base)

    if page_labels:
        dst.Root.PageLabels = dst.make_indirect(
            Dictionary(Nums=Array([0, Dictionary(S=Name.D, St=base + 1)]))
        )

    with src.open_metadata() as _:
        pass
    dst.Root.PageMode = dst.Root.get("/PageMode", Name.UseOutlines)
    path = outdir / chapter["filename"]
    dst.save(path, linearize=True)
    dst.close()
    return path, internal, external, dropped


# --------------------------------------------------------------------------
# generated table of contents
# --------------------------------------------------------------------------


def write_contents(outline, chapters, outdir, title, new_window, max_depth=2):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    width, height = letter
    left, right, top, bottom = 60, 60, 72, 60
    entries = []  # (pdf_page, rect, chapter_i, local)

    def flatten(items, depth=0):
        for item in items:
            if depth < max_depth:
                yield depth, item
                yield from flatten(item["children"], depth + 1)

    rows = list(flatten(outline))
    path = outdir / "00_Contents.pdf"
    canv = canvas.Canvas(str(path), pagesize=letter)
    canv.setTitle(title)

    y = height - top
    page_no = 0
    canv.setFont("Helvetica-Bold", 20)
    canv.drawString(left, y, title)
    y -= 34

    for depth, item in rows:
        if y < bottom + 40:
            canv.showPage()
            page_no += 1
            y = height - top
        size = 12 if depth == 0 else 10
        canv.setFont("Helvetica-Bold" if depth == 0 else "Helvetica", size)
        x = left + depth * 18
        text = item["title"]
        canv.drawString(x, y, text)

        target = item["page"]
        if target is not None:
            ci = chapter_of_page(chapters, target)
            if ci is not None:
                label = str(target + 1)
                canv.setFont("Helvetica", 9)
                canv.drawRightString(width - right, y, label)
                entries.append(
                    (
                        page_no,
                        (x - 2, y - 3, width - right, y + size),
                        ci,
                        target - chapters[ci]["start"],
                    )
                )
        y -= size + 7 if depth == 0 else size + 4
    canv.save()

    # attach the GoToR annotations
    pdf = pikepdf.open(path, allow_overwriting_input=True)
    for page in pdf.pages:
        page.obj.Annots = pdf.make_indirect(Array([]))
    for pno, rect, ci, local in entries:
        annot = pdf.make_indirect(
            Dictionary(
                Type=Name.Annot,
                Subtype=Name.Link,
                Rect=Array([float(v) for v in rect]),
                Border=Array([0, 0, 0]),
                A=make_gotor(
                    chapters[ci]["filename"], local, [Name.XYZ, None, None, None], new_window
                ),
            )
        )
        pdf.pages[pno].obj.Annots.append(annot)
    pdf.save(outdir / "00_Contents.tmp.pdf")
    pdf.close()
    (outdir / "00_Contents.tmp.pdf").replace(path)
    return path, len(entries)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_outline(args):
    pdf = pikepdf.open(args.pdf)
    page_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
    outline = read_outline(pdf, build_name_dests(pdf), page_index)
    print(f"{len(pdf.pages)} pages, {len(outline)} top-level bookmarks\n")
    for i, item in enumerate(outline):
        page = item["page"]
        print(
            f"{i:3d}  p{'?' if page is None else page + 1:<6} "
            f"{item['title'][:70]}  ({len(item['children'])} sub)"
        )


def cmd_split(args):
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"opening {args.pdf}")
    src = pikepdf.open(args.pdf)
    page_index = {p.obj.objgen: i for i, p in enumerate(src.pages)}
    name_dests = build_name_dests(src)
    print(f"  {len(src.pages)} pages, {len(name_dests)} named destinations")

    outline = read_outline(src, name_dests, page_index)
    chapters = plan_chapters(outline, len(src.pages), front_matter=not args.no_front_matter)
    print(f"  {len(chapters)} output files planned")

    targets, unresolved = collect_and_strip_links(src, name_dests, page_index)
    print(
        f"  {len(targets)} internal links mapped"
        + (f", {unresolved} unresolvable" if unresolved else "")
    )

    totals = [0, 0, 0]
    for i, chapter in enumerate(chapters):
        path, internal, external, dropped = write_chapter(
            src, chapters, i, targets, outdir, args.new_window, not args.no_page_labels
        )
        totals = [totals[0] + internal, totals[1] + external, totals[2] + dropped]
        size = path.stat().st_size / 1e6
        print(
            f"  [{i:2d}] {path.name:<48} "
            f"p{chapter['start'] + 1}-{chapter['stop']}  {size:6.1f} MB  "
            f"in:{internal} out:{external}"
        )

    if not args.no_contents:
        toc, n = write_contents(
            outline, chapters, outdir, args.title, args.new_window, args.toc_depth
        )
        print(f"  [ +] {toc.name:<48} {n} entries")

    print(
        f"\ndone: {totals[0]} intra-file links, {totals[1]} cross-file links"
        + (f", {totals[2]} dropped" if totals[2] else "")
    )
    src.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_out = sub.add_parser("outline", help="list top-level bookmarks and their pages")
    p_out.add_argument("pdf")
    p_out.set_defaults(func=cmd_outline)

    p_split = sub.add_parser("split", help="write one PDF per chapter")
    p_split.add_argument("pdf")
    p_split.add_argument("-o", "--out", default="chapters")
    p_split.add_argument("--title", default="Contents")
    p_split.add_argument("--toc-depth", type=int, default=2)
    p_split.add_argument(
        "--new-window",
        action="store_true",
        help="cross-file links open a new window instead of " "replacing the current document",
    )
    p_split.add_argument("--no-front-matter", action="store_true")
    p_split.add_argument("--no-contents", action="store_true")
    p_split.add_argument(
        "--no-page-labels", action="store_true", help="do not renumber pages to match the original"
    )
    p_split.set_defaults(func=cmd_split)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
