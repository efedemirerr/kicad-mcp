"""KiCad MCP server.

One server for the whole KiCad flow, schematic to fab files:
  - checks and outputs (ERC, DRC, BOM, stats, Gerbers, PDF, 3D render) drive the
    official `kicad-cli` that ships with KiCad;
  - schematic editing, PCB placement, routing and autorouting go through the
    pcbnew-based engine of mixelpixx/KiCAD-MCP-Server (see backend.py).

Run:  python server.py        (stdio transport, used by Claude Desktop)
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from backend import KicadBackend
from schematic import _find_block, build_schematic

mcp = MCPServer(
    name="KiCad",
    instructions=(
        "Tools for KiCad electronics projects. `project` may be a project folder or a "
        ".kicad_pro, .kicad_sch or .kicad_pcb path. Typical flow: new_project -> "
        "add_schematic_parts (or draw_schematic for a readable wired diagram) -> run_erc -> "
        "update_pcb_from_schematic -> set_board_outline -> auto_place or move_component -> "
        "autoroute (or route_pad_to_pad) -> run_drc -> export_manufacturing_files. "
        "Coordinates are in mm, y grows downwards. Use search_symbols / search_footprints / "
        "list_symbol_pins instead of guessing library names. Explain ERC/DRC violations in plain terms."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_kicad_cli() -> str:
    """Locate kicad-cli: env var, PATH, then the usual Windows/macOS/Linux installs."""
    env = os.environ.get("KICAD_CLI")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    candidates: list[Path] = []
    for base in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad",
    ):
        if base.exists():
            # newest version first (10.0, 9.0, ...)
            candidates += sorted(base.glob("*/bin/kicad-cli.exe"), reverse=True)
    candidates += [
        Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
        Path("/usr/bin/kicad-cli"),
        Path("/usr/local/bin/kicad-cli"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise RuntimeError(
        "kicad-cli not found. Install KiCad 8+ or set the KICAD_CLI environment variable."
    )


KICAD_CLI = _find_kicad_cli()
KICAD_BIN = Path(KICAD_CLI).resolve().parent


def _make_backend() -> KicadBackend:
    here = Path(__file__).resolve().parent
    script = Path(os.environ.get("KICAD_MCP_BACKEND")
                  or here.parent / "KiCAD-MCP-Server" / "python" / "kicad_interface.py")
    python_exe = Path(os.environ.get("KICAD_PYTHON")
                      or KICAD_BIN / ("python.exe" if os.name == "nt" else "python3"))
    env = {}
    jre = Path.home() / ".kicad-mcp" / "jre"
    if "JAVA_HOME" not in os.environ and jre.exists():
        env["JAVA_HOME"] = str(jre)  # portable JRE for Freerouting
    return KicadBackend(python_exe, script, env, Path.home() / ".kicad-mcp" / "logs" / "backend-stderr.log")


BACKEND = _make_backend()


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [KICAD_CLI, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc


def _check(proc: subprocess.CompletedProcess, what: str) -> None:
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {msg}")


def _resolve(path: str, ext: str) -> Path:
    """Turn a folder / .kicad_pro / sibling file into the wanted .kicad_sch or .kicad_pcb."""
    p = Path(path).expanduser().resolve()
    if p.is_file() and p.suffix == ext:
        return p
    if p.is_file():
        cand = p.with_suffix(ext)
        if cand.exists():
            return cand
        p = p.parent
    if p.is_dir():
        pros = list(p.glob("*.kicad_pro"))
        if pros:
            cand = pros[0].with_suffix(ext)
            if cand.exists():
                return cand
        files = list(p.glob(f"*{ext}"))
        if len(files) == 1:
            return files[0]
    raise FileNotFoundError(f"No {ext} file found for '{path}'")


def _out_dir(source: Path, output_dir: str | None, sub: str) -> Path:
    d = Path(output_dir).expanduser().resolve() if output_dir else source.parent / "mcp_output" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _violation(v: dict, **extra) -> dict:
    return {
        "severity": v.get("severity"),
        "type": v.get("type"),
        "description": v.get("description"),
        "items": [i.get("description") for i in v.get("items", [])],
        **extra,
    }


def _summarize(items: list[dict]) -> dict:
    """Counts per severity plus the readable list."""
    counts: dict[str, int] = {}
    for i in items:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    return {"total": len(items), "by_severity": counts, "violations": items}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
def kicad_version() -> str:
    """Return the installed KiCad version and the kicad-cli path in use."""
    proc = _run(["version"])
    _check(proc, "kicad-cli version")
    return f"KiCad {proc.stdout.strip()} ({KICAD_CLI})"


@mcp.tool()
def find_projects(directory: str, max_results: int = 50) -> list[dict]:
    """Search a folder (recursively) for KiCad projects (.kicad_pro files)."""
    root = Path(directory).expanduser().resolve()
    results = []
    for pro in root.rglob("*.kicad_pro"):
        results.append({
            "name": pro.stem,
            "project": str(pro),
            "has_schematic": pro.with_suffix(".kicad_sch").exists(),
            "has_pcb": pro.with_suffix(".kicad_pcb").exists(),
        })
        if len(results) >= max_results:
            break
    return results


@mcp.tool()
def run_erc(project: str, include_warnings: bool = True) -> dict:
    """Run the Electrical Rules Check on a schematic and return the violations.

    project: project folder, .kicad_pro or .kicad_sch path.
    """
    sch = _resolve(project, ".kicad_sch")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "erc.json"
        args = ["sch", "erc", "--format", "json", "--units", "mm", "-o", str(out)]
        args += ["--severity-all"] if include_warnings else ["--severity-error"]
        proc = _run(args + [str(sch)])
        _check(proc, "ERC")
        report = json.loads(out.read_text(encoding="utf-8"))
    result = _summarize([
        _violation(v, sheet=sheet.get("path"))
        for sheet in report.get("sheets", [])
        for v in sheet.get("violations", [])
    ])
    result["schematic"] = str(sch)
    return result


@mcp.tool()
def run_drc(project: str, check_schematic_parity: bool = True, refill_zones: bool = False) -> dict:
    """Run the Design Rules Check on a PCB and return violations, unconnected items
    and (optionally) schematic/PCB mismatches.

    project: project folder, .kicad_pro or .kicad_pcb path.
    """
    pcb = _resolve(project, ".kicad_pcb")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "drc.json"
        args = ["pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "-o", str(out)]
        if check_schematic_parity:
            args.append("--schematic-parity")
        if refill_zones:
            args.append("--refill-zones")
        proc = _run(args + [str(pcb)])
        _check(proc, "DRC")
        report = json.loads(out.read_text(encoding="utf-8"))
    result = _summarize([
        _violation(v, category=category)
        for category in ("violations", "unconnected_items", "schematic_parity")
        for v in report.get(category, [])
    ])
    result["pcb"] = str(pcb)
    return result


@mcp.tool()
def list_components(project: str) -> list[dict]:
    """List every component in the schematic (reference, value, footprint, datasheet, DNP)."""
    sch = _resolve(project, ".kicad_sch")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "parts.csv"
        proc = _run([
            "sch", "export", "bom", "-o", str(out),
            "--fields", "Reference,Value,Footprint,Datasheet,${DNP}",
            "--labels", "Reference,Value,Footprint,Datasheet,DNP",
            "--group-by", "", "--ref-range-delimiter", "",
            str(sch),
        ])
        _check(proc, "Component export")
        rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    return rows


@mcp.tool()
def export_bom(project: str, output_file: str | None = None, group_by_value: bool = True) -> dict:
    """Export a Bill of Materials as CSV. Returns the file path and the BOM rows.

    By default the file is written to <project>/mcp_output/bom/<name>_bom.csv.
    """
    sch = _resolve(project, ".kicad_sch")
    out = Path(output_file).resolve() if output_file else _out_dir(sch, None, "bom") / f"{sch.stem}_bom.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["sch", "export", "bom", "-o", str(out),
            "--fields", "Reference,Value,Footprint,${QUANTITY},${DNP}",
            "--labels", "Refs,Value,Footprint,Qty,DNP"]
    if group_by_value:
        args += ["--group-by", "Value,Footprint"]
    proc = _run(args + [str(sch)])
    _check(proc, "BOM export")
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    return {"file": str(out), "line_items": len(rows), "rows": rows}


@mcp.tool()
def board_stats(project: str) -> dict:
    """Board statistics: size, layer count, component/pad/via/hole counts, etc."""
    pcb = _resolve(project, ".kicad_pcb")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "stats.json"
        proc = _run(["pcb", "export", "stats", "--format", "json", "--units", "mm", "-o", str(out), str(pcb)])
        _check(proc, "Board stats")
        return json.loads(out.read_text(encoding="utf-8"))


@mcp.tool()
def export_manufacturing_files(project: str, output_dir: str | None = None) -> dict:
    """Generate Gerbers + drill files and pack them in a zip ready to upload to a PCB fab
    (JLCPCB, PCBWay, ...). Run run_drc first.

    By default writes to <project>/mcp_output/manufacturing/.
    """
    pcb = _resolve(project, ".kicad_pcb")
    out = _out_dir(pcb, output_dir, "manufacturing")
    gerber_dir = out / "gerbers"
    if gerber_dir.exists():
        shutil.rmtree(gerber_dir)
    gerber_dir.mkdir()

    # Explicit fab layer set (stored plot settings are often incomplete); add any inner copper.
    header = pcb.read_text(encoding="utf-8").split("(setup", 1)[0]
    inner = sorted(set(re.findall(r'"(In\d+\.Cu)"', header)), key=lambda n: int(n[2:-3]))
    layers = ["F.Cu", *inner, "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen",
              "F.Mask", "B.Mask", "Edge.Cuts"]
    proc = _run(["pcb", "export", "gerbers", "--layers", ",".join(layers), "--subtract-soldermask",
                 "-o", str(gerber_dir) + os.sep, str(pcb)])
    _check(proc, "Gerber export")
    proc = _run(["pcb", "export", "drill", "--generate-map", "--map-format", "gerberx2",
                 "-o", str(gerber_dir) + os.sep, str(pcb)])
    _check(proc, "Drill export")

    files = sorted(p.name for p in gerber_dir.iterdir() if p.is_file())
    zip_path = out / f"{pcb.stem}_gerbers.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(gerber_dir / name, name)
    return {"zip": str(zip_path), "folder": str(gerber_dir), "files": files}


@mcp.tool()
def export_schematic_pdf(project: str, output_file: str | None = None) -> str:
    """Export the full schematic (all sheets) to a PDF. Returns the PDF path."""
    sch = _resolve(project, ".kicad_sch")
    out = Path(output_file).resolve() if output_file else _out_dir(sch, None, "docs") / f"{sch.stem}_schematic.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["sch", "export", "pdf", "-o", str(out), str(sch)])
    _check(proc, "Schematic PDF export")
    return str(out)


@mcp.tool()
def draw_schematic(
    output_file: str,
    components: list[dict],
    connections: list[list[str]],
    notes: list[dict] | None = None,
    title: str = "",
    overwrite: bool = False,
) -> dict:
    """Draw a NEW, human-readable schematic with real wires between pins, then run ERC on it.
    Best for small teaching circuits (filters, dividers, RLC). To add parts to an existing
    schematic, or for larger designs headed to a PCB, use add_schematic_parts instead.

    output_file: path of the .kicad_sch to create (a matching .kicad_pro is created too).
    components: list of {"ref", "symbol", "value", "x", "y", "rotation"?, "footprint"?, "properties"?}
      - symbol is a KiCad library id, e.g. "Device:R", "Device:C", "Device:L", "Device:LED",
        "Simulation_SPICE:VSIN", "Simulation_SPICE:VDC", "power:GND", "power:PWR_FLAG", "power:+5V".
      - x, y in mm on an A4 sheet (about 20..270 x 20..180), y grows downwards; keep to multiples of 2.54.
      - rotation 0 = as in library (R/C/L vertical, pin 1 on top), 90 = horizontal (pin 1 on the left).
      - power symbols use refs like "#PWR01"; add a power:PWR_FLAG on GND when nothing drives it.
    connections: [["R1.1", "L1.2"], ...] pin refs "<ref>.<pin number or pin name>"; each pair becomes
      a wire (straight if aligned, otherwise an L: horizontal first, then vertical). Place parts so
      pins line up and wires don't run through other parts.
    notes: optional [{"text", "x", "y"}] free text on the sheet.
    """
    sch = Path(output_file).expanduser().resolve()
    if sch.suffix != ".kicad_sch":
        sch = sch.with_suffix(".kicad_sch")
    if sch.exists() and not overwrite:
        raise FileExistsError(f"{sch} already exists (pass overwrite=true to replace it)")
    sch.parent.mkdir(parents=True, exist_ok=True)

    symbol_dir = Path(KICAD_CLI).resolve().parent.parent / "share" / "kicad" / "symbols"
    text, summary = build_schematic(symbol_dir, sch.stem, components, connections, notes, title)
    sch.write_text(text, encoding="utf-8")
    pro = sch.with_suffix(".kicad_pro")
    if not pro.exists():
        pro.write_text(json.dumps({"meta": {"filename": pro.name, "version": 3}}, indent=2), encoding="utf-8")

    erc = run_erc(str(sch))
    return {"schematic": str(sch), "project": str(pro), **summary, "erc": erc}


# ---------------------------------------------------------------------------
# schematic editing and PCB layout (pcbnew engine, see backend.py)
# ---------------------------------------------------------------------------

_INTERNAL_KEYS = ("_backend", "_realtime", "sessionBackend", "success")
_board_state: dict = {"pcb": None, "mtime": None}


def _engine(command: str, timeout_s: float = 300, **params) -> dict:
    """Run a backend command; raise with its message on failure, drop bookkeeping keys."""
    result = BACKEND.call(command, params, timeout_s)
    if result.get("success") is False:
        detail = result.get("errorDetails") or ""
        raise RuntimeError(f"{command}: {result.get('message', 'failed')} {detail}".strip())
    return {k: v for k, v in result.items() if k not in _INTERNAL_KEYS}


def _open_board(project: str) -> Path:
    """Make the engine work on this board, reloading it if the file changed on disk."""
    pcb = _resolve(project, ".kicad_pcb")
    mtime = pcb.stat().st_mtime_ns
    if _board_state["pcb"] != pcb or _board_state["mtime"] != mtime:
        pro = pcb.with_suffix(".kicad_pro")
        _engine("open_project", filename=str(pro if pro.exists() else pcb))
        _board_state.update(pcb=pcb, mtime=mtime)
    return pcb


def _save_board(pcb: Path) -> None:
    """Write the engine's in-memory board to disk (some commands already did)."""
    result = BACKEND.call("save_board", {})
    if result.get("success") is False and not result.get("diskChangedExternally"):
        raise RuntimeError(f"save_board: {result.get('message')}")
    _board_state.update(pcb=pcb, mtime=pcb.stat().st_mtime_ns)
    if result.get("diskChangedExternally"):
        _board_state["mtime"] = None  # the command wrote the file itself: reload next time


def _mm(x: float, y: float) -> dict:
    return {"x": x, "y": y, "unit": "mm"}


@mcp.tool()
def new_project(directory: str, name: str) -> dict:
    """Create an empty KiCad project (.kicad_pro, .kicad_sch, .kicad_pcb) in directory/."""
    return _engine("create_project", path=str(Path(directory).expanduser().resolve()), name=name)


@mcp.tool()
def search_symbols(query: str, limit: int = 20) -> dict:
    """Find schematic symbols in the installed KiCad libraries (e.g. 'LM358', 'USB_C', 'LED').
    Returns 'Library:Symbol' ids to use in add_schematic_parts / draw_schematic."""
    return _engine("search_symbols", query=query, limit=limit)


@mcp.tool()
def list_symbol_pins(symbol: str) -> dict:
    """Pin numbers, names and types of a library symbol, e.g. 'Device:LED' or 'Amplifier_Operational:LM358'.
    Use before add_schematic_parts so the 'nets' map uses real pin numbers."""
    return _engine("list_symbol_pins", symbol=symbol)


@mcp.tool()
def search_footprints(query: str, limit: int = 30) -> dict:
    """Find PCB footprints in the installed KiCad libraries (e.g. 'R_0805', 'SOIC-8', 'PinHeader_1x02').
    Returns 'Library:Footprint' ids for the footprint field of schematic parts."""
    return _engine("search_footprints", pattern=query, limit=limit)


@mcp.tool()
def add_schematic_parts(project: str, components: list[dict], global_labels: bool = False) -> dict:
    """Add parts to a project's schematic and connect them by net name (net labels on the pins).

    components: [{"symbol": "Device:R", "reference": "R1", "value": "10k",
                  "footprint": "Resistor_SMD:R_0805_2012Metric", "position": {"x": 100, "y": 80},
                  "rotation": 0, "nets": {"1": "VCC", "2": "OUT"}}]
      - pins with the same net name are connected; give every part a footprint so it can go on the PCB.
      - positions in mm on the sheet; keep parts ~15 mm apart.
    """
    sch = _resolve(project, ".kicad_sch")
    return _engine("batch_add_and_connect", schematicPath=str(sch), components=components,
                   labelType="global_label" if global_labels else "label")


@mcp.tool()
def edit_schematic_part(project: str, reference: str, value: str | None = None,
                        footprint: str | None = None, new_reference: str | None = None) -> dict:
    """Change a schematic part's value, footprint or reference (e.g. R1 value -> '220')."""
    sch = _resolve(project, ".kicad_sch")
    return _engine("edit_schematic_component", schematicPath=str(sch), reference=reference,
                   value=value, footprint=footprint, newReference=new_reference)


@mcp.tool()
def delete_schematic_part(project: str, reference: str) -> dict:
    """Remove a part (and the net labels left dangling on its pins) from the schematic."""
    sch = _resolve(project, ".kicad_sch")
    return _engine("delete_schematic_component", schematicPath=str(sch), reference=reference,
                   deleteAttachedLabels=True)


@mcp.tool()
def update_pcb_from_schematic(project: str) -> dict:
    """Bring schematic parts and nets onto the PCB (KiCad's F8). Creates the board if it does not
    exist yet. Run after every schematic change, before placing or routing."""
    sch = _resolve(project, ".kicad_sch")
    pcb = sch.with_suffix(".kicad_pcb")
    if not pcb.exists() or pcb.stat().st_size < 200:
        result = _engine("create_board_from_schematic", schematicPath=str(sch), boardPath=str(pcb),
                         overwrite=True)
    else:
        _open_board(str(pcb))
        result = _engine("sync_schematic_to_board", schematicPath=str(sch), boardPath=str(pcb))
        _save_board(pcb)
    _board_state["mtime"] = None
    return result


@mcp.tool()
def set_board_outline(project: str, width_mm: float, height_mm: float, x_mm: float = 100,
                      y_mm: float = 100, corner_radius_mm: float = 0) -> dict:
    """Set the board edge (Edge.Cuts) to a rectangle with its top-left corner at (x_mm, y_mm).
    Replaces any existing outline. Place parts inside this rectangle."""
    pcb = _open_board(project)
    params = {"x": x_mm, "y": y_mm, "width": width_mm, "height": height_mm, "unit": "mm"}
    shape = "rectangle"
    if corner_radius_mm > 0:
        shape, params["cornerRadius"] = "rounded_rectangle", corner_radius_mm
    result = _engine("replace_board_outline", shape=shape, params=params)
    _save_board(pcb)
    return result


@mcp.tool()
def add_mounting_hole(project: str, x_mm: float, y_mm: float, diameter_mm: float = 3.2) -> dict:
    """Add a mounting hole (3.2 mm fits an M3 screw) at (x_mm, y_mm)."""
    pcb = _open_board(project)
    result = _engine("add_mounting_hole", position=_mm(x_mm, y_mm), diameter=diameter_mm)
    _save_board(pcb)
    return result


@mcp.tool()
def list_pcb_components(project: str) -> dict:
    """Footprints on the PCB with position (mm), rotation and side."""
    _open_board(project)
    return _engine("get_component_list", unit="mm")


@mcp.tool()
def move_component(project: str, reference: str, x_mm: float, y_mm: float,
                   rotation_deg: float | None = None, bottom_side: bool = False) -> dict:
    """Move a footprint to (x_mm, y_mm), optionally rotate it (absolute degrees) or flip it to the bottom."""
    pcb = _open_board(project)
    result = _engine("move_component", reference=reference, position=_mm(x_mm, y_mm),
                     rotation=rotation_deg, layer="B.Cu" if bottom_side else None)
    _save_board(pcb)
    return result


def _outline_bbox(pcb: Path) -> tuple[float, float, float, float] | None:
    """Bounding box (x1, y1, x2, y2) of the Edge.Cuts graphics in a saved board file."""
    text = pcb.read_text(encoding="utf-8")
    xs, ys = [], []
    for m in re.finditer(r"\((gr_line|gr_arc|gr_rect|gr_poly|gr_circle)\s", text):
        block = _find_block(text, m.start())
        if '"Edge.Cuts"' not in block:
            continue
        for x, y in re.findall(r"\((?:start|end|mid|center|xy) ([-\d.]+) ([-\d.]+)\)", block):
            xs.append(float(x))
            ys.append(float(y))
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _overlaps(a: tuple, b: tuple, gap: float) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _pack_inside(pcb: Path, fixed: set[str], margin_mm: float, gap_mm: float) -> dict:
    """Shelf-pack the movable footprints into the outline, left to right, top to bottom,
    in their current left-to-right order, never overlapping each other or fixed parts."""
    box = _outline_bbox(pcb)
    if not box:
        raise RuntimeError("The board has no outline yet: call set_board_outline first.")
    x1, y1, x2, y2 = box[0] + margin_mm, box[1] + margin_mm, box[2] - margin_mm, box[3] - margin_mm
    comps = _engine("get_component_list", unit="mm").get("components", [])
    rect = lambda bb: (bb["min_x"], bb["min_y"], bb["max_x"], bb["max_y"])
    taken = [rect(c["boundingBox"]) for c in comps if c["reference"] in fixed]
    movable = sorted((c for c in comps if c["reference"] not in fixed),
                     key=lambda c: (c["position"]["x"], c["position"]["y"]))
    cx, cy, row_h = x1, y1, 0.0
    did_not_fit = []
    for c in movable:
        bb = c["boundingBox"]
        w, h = bb["max_x"] - bb["min_x"], bb["max_y"] - bb["min_y"]
        while True:
            if cx + w > x2:
                cx, cy, row_h = x1, cy + max(row_h, 1.0) + gap_mm, 0.0
            if cy + h > y2:
                did_not_fit.append(c["reference"])
                break
            cand = (cx, cy, cx + w, cy + h)
            hit = next((t for t in taken if _overlaps(cand, t, gap_mm)), None)
            if hit:
                cx = hit[2] + gap_mm
                continue
            pos = c["position"]
            _engine("move_component", reference=c["reference"],
                    position=_mm(round(pos["x"] + cx - bb["min_x"], 3), round(pos["y"] + cy - bb["min_y"], 3)))
            taken.append(cand)
            cx, row_h = cx + w + gap_mm, max(row_h, h)
            break
    return {"placed": len(movable) - len(did_not_fit), "did_not_fit": did_not_fit}


@mcp.tool()
def auto_place(project: str, keep_fixed: list[str] | None = None, edge_margin_mm: float = 1.5,
               gap_mm: float = 1.5) -> dict:
    """Automatically place footprints inside the board outline. Connected parts are first oriented
    toward each other, then packed in rows inside the outline (edge_margin_mm from the edge,
    gap_mm between parts) so nothing overlaps or crosses the edge. keep_fixed: refs that must not
    move (place connectors with move_component first). Set the board outline first; if parts are
    reported in did_not_fit, enlarge the board."""
    pcb = _open_board(project)
    fixed = set(keep_fixed or [])
    oriented = _engine("suggest_placement", locked=sorted(fixed), apply=True)
    result = _pack_inside(pcb, fixed, edge_margin_mm, gap_mm)
    result["rotated"] = oriented.get("summary", {}).get("rotated")
    _save_board(pcb)
    return result


@mcp.tool()
def route_pad_to_pad(project: str, from_ref: str, from_pad: str, to_ref: str, to_pad: str,
                     layer: str = "F.Cu", width_mm: float | None = None) -> dict:
    """Draw one straight track between two pads (e.g. R1 pad 2 -> D1 pad 1). It does NOT avoid
    obstacles, so run run_drc afterwards; prefer autoroute for whole boards."""
    pcb = _open_board(project)
    result = _engine("route_pad_to_pad", fromRef=from_ref, fromPad=from_pad, toRef=to_ref,
                     toPad=to_pad, layer=layer, width=width_mm)
    _save_board(pcb)
    return result


@mcp.tool()
def autoroute(project: str, attempts: int = 1, timeout_s: int = 300) -> dict:
    """Route every unrouted connection with the Freerouting autorouter (needs Java 25 or Docker).
    attempts > 1 tries several pass counts and keeps the most complete result (use 3-5 for dense boards).
    Existing tracks are kept: run delete_tracks first to re-route from scratch."""
    pcb = _open_board(project)
    result = _engine("autoroute", timeout_s=timeout_s * max(attempts, 1) + 60,
                     boardPath=str(pcb), attempts=attempts, timeout=timeout_s)
    result.pop("freerouting_stdout", None)
    _board_state["mtime"] = None  # autoroute writes the board file itself
    result["widened_tracks"] = _widen_thin_tracks(project)
    return result


def _widen_thin_tracks(project: str) -> int:
    """Freerouting necks tracks down in tight spots; bring them back to the board's minimum width."""
    pcb = _open_board(project)
    try:
        rules = json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        min_w = rules["board"]["design_settings"]["rules"]["min_track_width"]
    except (OSError, KeyError, ValueError):
        return 0
    thin = [t for t in _engine("query_traces", unit="mm").get("traces", []) if t["width"] < min_w - 1e-6]
    for t in thin:
        _engine("modify_trace", traceUuid=t["uuid"], width=min_w)
    if thin:
        _save_board(pcb)
    return len(thin)


@mcp.tool()
def delete_tracks(project: str, net: str | None = None) -> dict:
    """Delete all tracks and vias of one net, or of every net when net is omitted."""
    pcb = _open_board(project)
    if net:
        nets = [net]
    else:
        listed = _engine("get_nets_list").get("nets", [])
        nets = [n["name"] if isinstance(n, dict) else n for n in listed]
    deleted = 0
    for name in nets:
        if name:
            deleted += _engine("delete_trace", net=name, includeVias=True).get("deletedCount", 0)
    _save_board(pcb)
    return {"deleted": deleted, "nets": [n for n in nets if n]}


@mcp.tool()
def add_copper_pour(project: str, net: str = "GND", layer: str = "B.Cu", clearance_mm: float = 0.3) -> dict:
    """Fill a copper layer with a net (usually a GND plane on B.Cu) across the whole board outline."""
    pcb = _open_board(project)
    result = _engine("add_copper_pour", net=net, layer=layer, clearance=clearance_mm)
    _engine("refill_zones")
    _save_board(pcb)
    return result


@mcp.tool()
def set_design_rules(project: str, clearance_mm: float | None = None, track_width_mm: float | None = None,
                     via_diameter_mm: float | None = None, via_drill_mm: float | None = None) -> dict:
    """Set board design rules, e.g. a fab's minimums (JLCPCB standard: clearance 0.127, track 0.127,
    via 0.45/0.3) or wider defaults for power tracks."""
    pcb = _open_board(project)
    result = _engine("set_design_rules", clearance=clearance_mm, trackWidth=track_width_mm,
                     viaDiameter=via_diameter_mm, viaDrill=via_drill_mm)
    _save_board(pcb)
    return result


@mcp.tool()
def view_pcb(project: str, copper_only: bool = False) -> Image:
    """2D top view of the PCB (both copper layers, silkscreen, outline) so you can check placement
    and routing. copper_only=True shows just tracks, pads and the outline."""
    pcb = _resolve(project, ".kicad_pcb")
    layers = ["F.Cu", "B.Cu", "Edge.Cuts"] if copper_only else None
    result = _engine("get_board_2d_view", pcbPath=str(pcb), layers=layers, width=1400, height=1000,
                     format="png")
    return Image(data=base64.b64decode(result["imageData"]), format="png")


@mcp.tool()
def render_pcb_3d(project: str, side: str = "top", isometric: bool = False) -> Image:
    """Render a 3D picture of the PCB so you can see the board.

    side: top, bottom, left, right, front or back. isometric=True gives an angled view.
    The PNG is also saved in <project>/mcp_output/renders/.
    """
    pcb = _resolve(project, ".kicad_pcb")
    out = _out_dir(pcb, None, "renders") / f"{pcb.stem}_{side}{'_iso' if isometric else ''}.png"
    args = ["pcb", "render", "-o", str(out), "--side", side, "-w", "1200", "-h", "800",
            "--quality", "basic", "--background", "opaque"]
    if isometric:
        args += ["--rotate", "-45,0,45", "--perspective"]
    proc = _run(args + [str(pcb)], timeout=600)
    _check(proc, "3D render")
    return Image(path=out)


if __name__ == "__main__":
    mcp.run()
