#!/usr/bin/env python3
"""Generate a GitLab-flavored markdown fab summary from a KiCad PCB."""

import argparse
import datetime
import shutil
import sys
import tempfile
from pathlib import Path

import pcbnew


# ---------- helpers --------------------------------------------------------- #

def to_mm(value):
    """Convert KiCad internal units (nm) to mm."""
    return pcbnew.ToMM(value)


def to_mil(value):
    """Convert KiCad internal units (nm) to mil."""
    return pcbnew.ToMils(value)


def fmt_mm_mil(value, mm_places=3, mil_places=1):
    """Format a length in internal units as 'X.XXX mm (Y.Y mil)'."""
    return f"{to_mm(value):.{mm_places}f} mm ({to_mil(value):.{mil_places}f} mil)"


def safe_attr(obj, *names, default=None):
    """Try several attribute/method names on obj, return the first that works."""
    for name in names:
        if hasattr(obj, name):
            attr = getattr(obj, name)
            try:
                return attr() if callable(attr) else attr
            except Exception:
                continue
    return default


def find_board_image(img_dir):
    """Find the first PNG in img_dir. Returns a markdown-friendly relative
    path string, or None if no PNG is found.

    img_dir can be a relative path (resolved from CWD) or absolute.
    """
    if not img_dir:
        return None

    d = Path(img_dir)
    if not d.is_dir():
        print(f"warning: image dir not found: {d}", file=sys.stderr)
        return None

    pngs = sorted(d.glob("*.png"))
    if not pngs:
        print(f"warning: no .png files in {d}", file=sys.stderr)
        return None
    if len(pngs) > 1:
        print(
            f"warning: multiple .png files in {d}, using {pngs[0].name} "
            f"(others: {', '.join(p.name for p in pngs[1:])})",
            file=sys.stderr,
        )

    # Use forward slashes so the markdown link works on every platform.
    return f"{img_dir.replace(chr(92), '/').rstrip('/')}/{pngs[0].name}"


def load_board_safely(board_path):
    """Load a board from a temp copy so a lock on the original doesn't block us.

    Returns (board, tmp_dir). Caller must rmtree tmp_dir when done.
    """
    src = Path(board_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Board file not found: {src}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="board_specs_"))
    tmp_path = tmp_dir / src.name
    shutil.copy2(src, tmp_path)

    board = pcbnew.LoadBoard(str(tmp_path))
    if board is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(
            f"pcbnew.LoadBoard returned None for {src}. "
            "The file may be corrupt, locked, or in an unexpected format."
        )
    return board, tmp_dir


# ---------- extractors ------------------------------------------------------ #

def get_title_block(board):
    tb = board.GetTitleBlock()
    return {
        "title": tb.GetTitle(),
        "company": tb.GetCompany(),
    }


def get_dimensions_mm(board):
    bbox = board.GetBoardEdgesBoundingBox()
    return to_mm(bbox.GetWidth()), to_mm(bbox.GetHeight())


def get_stackup_info(board):
    """Pull copper count, thickness, copper-layer thicknesses, mask/silk color,
    and surface finish out of the stackup. All fields fall back to '—'."""
    ds = board.GetDesignSettings()
    info = {
        "copper_layers": board.GetCopperLayerCount(),
        "total_thickness_mm": to_mm(ds.GetBoardThickness()),
        "copper_thicknesses_mm": [],
        "mask_color": "—",
        "silk_color": "—",
        "finish": "—",
    }

    try:
        stackup = ds.GetStackupDescriptor()
    except Exception as exc:
        print(f"warning: stackup unavailable ({exc})", file=sys.stderr)
        return info

    BS_COPPER = getattr(pcbnew, "BS_ITEM_TYPE_COPPER", None)
    BS_MASK = getattr(pcbnew, "BS_ITEM_TYPE_SOLDERMASK", None)
    BS_SILK = getattr(pcbnew, "BS_ITEM_TYPE_SILKSCREEN", None)

    try:
        items = list(stackup.GetList())
    except Exception:
        items = []

    for layer in items:
        ltype = safe_attr(layer, "GetType")
        if ltype == BS_COPPER and BS_COPPER is not None:
            t = safe_attr(layer, "GetThickness", default=0)
            if t:
                info["copper_thicknesses_mm"].append(to_mm(t))
        elif ltype == BS_MASK and BS_MASK is not None:
            color = safe_attr(layer, "GetColor", default="")
            if color and info["mask_color"] == "—":
                info["mask_color"] = str(color)
        elif ltype == BS_SILK and BS_SILK is not None:
            color = safe_attr(layer, "GetColor", default="")
            if color and info["silk_color"] == "—":
                info["silk_color"] = str(color)

    finish = safe_attr(stackup, "GetFinishType", default=None)
    if not finish:
        finish = getattr(stackup, "m_FinishType", None)
    if finish:
        info["finish"] = str(finish)

    return info


def get_design_rules(board):
    """Configured minimums from Board Setup → Constraints, in internal units."""
    ds = board.GetDesignSettings()
    return {
        "min_track_width": getattr(ds, "m_TrackMinWidth", 0),
        "min_clearance": getattr(ds, "m_MinClearance", 0),
        "min_via_diameter": getattr(ds, "m_ViasMinSize", 0),
        "min_via_drill": getattr(ds, "m_MinThroughDrill", 0),
    }


def count_components(board):
    fps = list(board.GetFootprints())

    smt = 0
    tht = 0
    tht_holes = 0
    for fp in fps:
        attrs = fp.GetAttributes()
        if attrs & pcbnew.FP_SMD:
            smt += 1
        elif attrs & pcbnew.FP_THROUGH_HOLE:
            tht += 1
        for pad in fp.Pads():
            d = pad.GetDrillSize()
            if d.x > 0 or d.y > 0:
                tht_holes += 1

    vias = sum(1 for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T)

    return {
        "total_parts": len(fps),
        "smt_parts": smt,
        "tht_parts": tht,
        "vias": vias,
        "tht_holes": tht_holes,
    }


# ---------- rendering ------------------------------------------------------- #

def render_markdown(board_path, img_link, data):
    tb = data["title_block"]
    project = tb["title"] or Path(board_path).stem
    today = datetime.date.today().isoformat()

    w, h = data["dimensions_mm"]
    s = data["stackup"]
    r = data["rules"]
    c = data["counts"]

    if s["copper_thicknesses_mm"]:
        copper_str = ", ".join(f"{t:.3f} mm" for t in s["copper_thicknesses_mm"])
    else:
        copper_str = "—"

    lines = [
        f"# {project} — Fab Summary",
        "",
        f"**Project:** {project}  ",
        f"**Report generated:** {today}",
        "",
    ]

    if img_link:
        lines += [f"![Board render]({img_link})", ""]

    lines += [
        "## Board",
        "",
        "| Property | Value |",
        "| --- | --- |",
        f"| Outline size | {w:.2f} × {h:.2f} mm |",
        f"| Copper layers | {s['copper_layers']} |",
        f"| Total thickness | {s['total_thickness_mm']:.2f} mm |",
        f"| Copper thickness (per layer) | {copper_str} |",
        f"| Solder mask color | {s['mask_color']} |",
        f"| Silkscreen color | {s['silk_color']} |",
        f"| Surface finish | {s['finish']} |",
        "",
        "## Design rules (configured minimums)",
        "",
        "| Property | Value |",
        "| --- | --- |",
        f"| Min trace width | {fmt_mm_mil(r['min_track_width'])} |",
        f"| Min clearance | {fmt_mm_mil(r['min_clearance'])} |",
        f"| Min via diameter | {fmt_mm_mil(r['min_via_diameter'])} |",
        f"| Min via drill | {fmt_mm_mil(r['min_via_drill'])} |",
        "",
        "## Counts",
        "",
        "| Property | Value |",
        "| --- | --- |",
        f"| Total parts | {c['total_parts']} |",
        f"| SMT parts | {c['smt_parts']} |",
        f"| THT parts | {c['tht_parts']} |",
        f"| THT holes | {c['tht_holes']} |",
        f"| Vias | {c['vias']} |",
        "",
    ]
    return "\n".join(lines)


# ---------- entrypoint ------------------------------------------------------ #

def main():
    p = argparse.ArgumentParser(
        description="Generate a markdown fab summary for a KiCad PCB."
    )
    p.add_argument("board", help="Path to the .kicad_pcb file")
    p.add_argument("--out", required=True, help="Output markdown file")
    p.add_argument(
        "--img-dir",
        default="Outputs/img",
        help="Directory containing a board render PNG (default: Outputs/img). "
             "The first .png found is used. Pass an empty string to omit.",
    )
    args = p.parse_args()

    # KiCad's jobset Execute Command on Windows passes wrapping quotes through
    # literally instead of letting the shell consume them. Strip them defensively.
    args.board = args.board.strip('"').strip("'")
    args.out = args.out.strip('"').strip("'")
    if args.img_dir:
        args.img_dir = args.img_dir.strip('"').strip("'")

    img_link = find_board_image(args.img_dir)

    # Load via temp copy so a lock on the original (e.g. pcbnew open) doesn't
    # silently turn into a None board.
    board, tmp_dir = load_board_safely(args.board)
    try:
        data = {
            "title_block": get_title_block(board),
            "dimensions_mm": get_dimensions_mm(board),
            "stackup": get_stackup_info(board),
            "rules": get_design_rules(board),
            "counts": count_components(board),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    md = render_markdown(args.board, img_link, data)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()