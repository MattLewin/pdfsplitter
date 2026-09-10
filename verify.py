#!/usr/bin/env python3
"""Independently verify the split: every link in the output must resolve to the
same physical page of the original manual as it did before the split.

    pdfsplitter-verify manual.pdf out/
"""

import argparse
import sys
from pathlib import Path

import pikepdf

from split_manual import (
    build_name_dests,
    dest_of_annot,
    plan_chapters,
    read_outline,
    resolve_dest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdf", help="the original, unsplit PDF")
    parser.add_argument("outdir", help="directory holding the split output")
    args = parser.parse_args()

    src_path, outdir = args.pdf, Path(args.outdir)

    # ---- ground truth ---------------------------------------------------------
    src = pikepdf.open(src_path)
    page_index = {p.obj.objgen: i for i, p in enumerate(src.pages)}
    name_dests = build_name_dests(src)
    truth = {}
    uri_truth = {}
    for pno, page in enumerate(src.pages):
        if "/Annots" not in page.obj:
            continue
        for ano, annot in enumerate(page.obj.Annots):
            if annot.get("/Subtype") != "/Link":
                continue
            d = dest_of_annot(annot)
            if d is None:
                a = annot.get("/A")
                if a is not None and a.get("/S") == "/URI":
                    uri_truth[(pno, ano)] = str(a.URI)
                continue
            hit = resolve_dest(d, name_dests, page_index)
            if hit:
                truth[(pno, ano)] = hit[0]

    outline = read_outline(src, name_dests, page_index)
    chapters = plan_chapters(outline, len(src.pages))
    by_file = {c["filename"]: c for c in chapters}

    print(f"ground truth: {len(truth)} GoTo links, {len(uri_truth)} URI links")

    # ---- check page counts --------------------------------------------------
    ok = fail = 0
    for c in chapters:
        pdf = pikepdf.open(outdir / c["filename"])
        expected = c["stop"] - c["start"]
        got = len(pdf.pages)
        status = "ok" if got == expected else "MISMATCH"
        if got != expected:
            fail += 1
        print(
            f"  {c['filename']:<44} pages {got}/{expected} {status}"
            f"  outline={'/Outlines' in pdf.Root}"
            f"  labels={'/PageLabels' in pdf.Root}"
        )
        pdf.close()

    # ---- check every link -------------------------------------------------
    print("\nlink-by-link:")
    uri_seen = 0
    for c in chapters:
        pdf = pikepdf.open(outdir / c["filename"])
        local_names = build_name_dests(pdf)
        local_index = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        for local, pno in enumerate(range(c["start"], c["stop"])):
            if "/Annots" not in pdf.pages[local].obj:
                continue
            for ano, annot in enumerate(pdf.pages[local].obj.Annots):
                if annot.get("/Subtype") != "/Link":
                    continue
                action = annot.get("/A")
                if action is not None and action.get("/S") == "/URI":
                    assert str(action.URI) == uri_truth.get((pno, ano)), "URI changed"
                    uri_seen += 1
                    continue
                want = truth.get((pno, ano))
                if want is None:
                    continue
                if action is not None and action.get("/S") == "/GoToR":
                    fname = str(action.F.F)
                    target_chapter = by_file.get(fname)
                    if target_chapter is None:
                        print(f"    FAIL unknown file {fname}")
                        fail += 1
                        continue
                    got = target_chapter["start"] + int(action.D[0])
                else:
                    hit = resolve_dest(dest_of_annot(annot), local_names, local_index)
                    if hit is None:
                        print(f"    FAIL unresolvable link p{pno} a{ano}")
                        fail += 1
                        continue
                    got = c["start"] + hit[0]
                if got == want:
                    ok += 1
                else:
                    fail += 1
                    print(
                        f"    FAIL {c['filename']} p{pno} a{ano}: "
                        f"points to {got + 1}, should be {want + 1}"
                    )
        pdf.close()

    print(
        f"  {ok} links resolve to the correct original page, "
        f"{uri_seen} URI links intact, {fail} failures"
    )

    # ---- check the generated ToC -----------------------------------------
    toc = pikepdf.open(outdir / "00_Contents.pdf")
    tocn = 0
    flat = []

    def flatten(items, d=0):
        for it in items:
            if d < 2:
                flat.append(it)
                flatten(it["children"], d + 1)

    flatten(outline)
    expected_targets = [it["page"] for it in flat if it["page"] is not None]
    got_targets = []
    for page in toc.pages:
        for annot in page.obj.get("/Annots", []):
            a = annot.A
            assert a.S == "/GoToR"
            ch = by_file[str(a.F.F)]
            got_targets.append(ch["start"] + int(a.D[0]))
            tocn += 1
    print(f"\nToC: {tocn} links; targets match outline: {got_targets == expected_targets}")

    passed = fail == 0 and got_targets == expected_targets
    print("\nRESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
