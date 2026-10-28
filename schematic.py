"""Write KiCad schematics (.kicad_sch) from a simple component/connection description.

Symbols are copied from KiCad's own libraries (share/kicad/symbols), so the result
opens in Eeschema like a hand-drawn schematic and can be checked with ERC.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from pathlib import Path

GRID = 1.27


def _uid() -> str:
    return str(uuid.uuid4())


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def _find_block(text: str, start: int) -> str:
    """Return the balanced (...) expression starting at text[start]."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise ValueError("unbalanced expression")


class SymbolLibrary:
    """Loads symbols from KiCad's installed .kicad_sym libraries."""

    def __init__(self, symbol_dir: Path):
        self.dir = symbol_dir
        self._files: dict[str, str] = {}

    def _lib_text(self, lib: str) -> str:
        if lib not in self._files:
            f = self.dir / f"{lib}.kicad_sym"
            if not f.exists():
                raise FileNotFoundError(f"Symbol library '{lib}' not found in {self.dir}")
            self._files[lib] = f.read_text(encoding="utf-8")
        return self._files[lib]

    def _raw(self, lib: str, name: str) -> str:
        text = self._lib_text(lib)
        m = re.search(rf'\n\t\(symbol "{re.escape(name)}"\s', text)
        if not m:
            raise KeyError(f"Symbol '{lib}:{name}' not found")
        return _find_block(text, m.start() + 2)

    def get(self, lib_id: str) -> tuple[str, dict]:
        """Return (lib_symbols entry, info) for 'Lib:Name'. info has pins and default properties."""
        lib, name = lib_id.split(":", 1)
        raw = self._raw(lib, name)
        parent = re.search(r'\(extends "([^"]+)"\)', raw)
        if parent:
            # Flatten a derived symbol: parent's graphics/pins + child's properties.
            base = self._raw(lib, parent.group(1))
            child_props = [_find_block(raw, m.start()) for m in re.finditer(r'\(property "', raw)]
            for blk in [_find_block(base, m.start()) for m in re.finditer(r'\(property "', base)]:
                base = base.replace(blk, "", 1)
            insert_at = base.index("\n", base.index("(symbol")) + 1
            base = base[:insert_at] + "\n".join(child_props) + "\n" + base[insert_at:]
            base = base.replace(f'(symbol "{parent.group(1)}_', f'(symbol "{name}_')
            raw = base.replace(f'(symbol "{parent.group(1)}"', f'(symbol "{name}"', 1)

        entry = raw.replace(f'(symbol "{name}"', f'(symbol "{lib_id}"', 1)

        pins = {}
        for sub in re.finditer(rf'\(symbol "{re.escape(name)}_(\d+)_(\d+)"', raw):
            unit, style = int(sub.group(1)), int(sub.group(2))
            if unit > 1 or style > 1:
                continue
            sub_text = _find_block(raw, sub.start())
            for pm in re.finditer(r"\(pin \w+ \w+", sub_text):
                pin = _find_block(sub_text, pm.start())
                at = re.search(r"\(at ([-\d.]+) ([-\d.]+)", pin)
                num = re.search(r'\(number "([^"]*)"', pin)
                pname = re.search(r'\(name "([^"]*)"', pin)
                pins[num.group(1)] = {
                    "x": float(at.group(1)), "y": float(at.group(2)),
                    "name": pname.group(1) if pname else "",
                }
        props = dict(re.findall(r'\(property "([^"]+)" "([^"]*)"', raw))
        is_power = "(power)" in raw or re.search(r"\(power\s", raw) is not None
        return entry, {"pins": pins, "props": props, "power": is_power}


def _pin_position(x: float, y: float, rot: int, px: float, py: float, mirror: str = "") -> tuple[float, float]:
    """Library pin coords (Y up) -> schematic coords (Y down) for a symbol placed at (x, y)."""
    if mirror == "x":
        py = -py
    elif mirror == "y":
        px = -px
    a = math.radians(rot)
    rx = px * math.cos(a) - py * math.sin(a)
    ry = px * math.sin(a) + py * math.cos(a)
    return round(x + rx, 4), round(y - ry, 4)


def _effects(hide: bool = False, justify: str | None = None) -> str:
    j = f" (justify {justify})" if justify else ""
    h = " (hide yes)" if hide else ""
    return f"{h} (effects (font (size 1.27 1.27)){j})"


def build_schematic(
    symbol_dir: Path,
    project_name: str,
    components: list[dict],
    connections: list[list[str]],
    notes: list[dict] | None = None,
    title: str = "",
) -> tuple[str, dict]:
    """Return (.kicad_sch text, summary).

    components: [{"ref": "R1", "symbol": "Device:R", "value": "1k", "x": 100, "y": 50,
                  "rotation": 0, "footprint": "", "properties": {...}}]
      Power symbols (e.g. "power:GND") use refs like "#PWR01".
    connections: [["R1.1", "C1.2"], ...] — pin references "<ref>.<pin number or name>".
      Each pair becomes a wire (straight, or an L-shape: horizontal first, then vertical).
    notes: [{"text": "...", "x": 50, "y": 20}]
    """
    lib = SymbolLibrary(symbol_dir)
    root = _uid()
    lib_entries: dict[str, str] = {}
    placed: dict[str, dict] = {}
    out_symbols = []

    for c in components:
        ref, lib_id = c["ref"], c["symbol"]
        x, y = _snap(c["x"]), _snap(c["y"])
        rot = int(c.get("rotation", 0)) % 360
        if lib_id not in lib_entries:
            lib_entries[lib_id] = lib.get(lib_id)
        entry, info = lib_entries[lib_id]
        pin_xy = {}
        for num, p in info["pins"].items():
            pos = _pin_position(x, y, rot, p["x"], p["y"])
            pin_xy[num] = pos
            if p["name"] and p["name"] != "~":
                pin_xy.setdefault(p["name"], pos)
        placed[ref] = {"pins": pin_xy, "info": info}

        props = dict(info["props"])
        props.update({"Reference": ref, "Value": str(c.get("value", props.get("Value", "")))})
        if "footprint" in c:
            props["Footprint"] = c["footprint"]
        props.update(c.get("properties", {}))

        vertical = rot in (0, 180)
        prop_lines = []
        for k, v in props.items():
            if k.startswith("ki_"):
                continue
            if k == "Reference":
                hide = info["power"]
                at = (x + 2.54, y - 1.27, 0) if vertical else (x, y - 3.81, 0)
                prop_lines.append(f'(property "Reference" {json.dumps(v)} (at {at[0]} {at[1]} 0){_effects(hide, "left" if vertical else None)})')
            elif k == "Value":
                if info["power"]:
                    at = (x, y + 3.81 if lib_id.endswith("GND") else y - 3.81, 0)
                    just = None
                else:
                    at = (x + 2.54, y + 1.27, 0) if vertical else (x, y + 3.81, 0)
                    just = "left" if vertical else None
                prop_lines.append(f'(property "Value" {json.dumps(v)} (at {at[0]} {at[1]} 0){_effects(False, just)})')
            else:
                prop_lines.append(f'(property {json.dumps(k)} {json.dumps(v)} (at {x} {y} 0){_effects(True)})')

        pin_lines = [f'(pin {json.dumps(n)} (uuid "{_uid()}"))' for n in info["pins"]]
        out_symbols.append(
            f'\t(symbol (lib_id {json.dumps(lib_id)}) (at {x} {y} {rot}) (unit 1)\n'
            f'\t\t(exclude_from_sim no) (in_bom {"no" if info["power"] else "yes"}) '
            f'(on_board {"no" if info["power"] else "yes"}) (dnp no)\n'
            f'\t\t(uuid "{_uid()}")\n\t\t' + "\n\t\t".join(prop_lines + pin_lines) + "\n"
            f'\t\t(instances (project {json.dumps(project_name)} (path "/{root}" (reference {json.dumps(ref)}) (unit 1))))\n'
            f"\t)"
        )

    def resolve(pin_ref: str) -> tuple[float, float]:
        ref, _, pin = pin_ref.rpartition(".")
        if ref not in placed:
            raise KeyError(f"Unknown component '{ref}' in connection '{pin_ref}'")
        pins = placed[ref]["pins"]
        if pin not in pins:
            if len(pins) == 1 and pin in ("", "1"):
                return next(iter(pins.values()))
            raise KeyError(f"'{ref}' has no pin '{pin}'. Pins: {sorted(pins)}")
        return pins[pin]

    wires = []
    for a_ref, b_ref in connections:
        a, b = resolve(a_ref), resolve(b_ref)
        if a == b:
            continue
        if a[0] == b[0] or a[1] == b[1]:
            wires.append((a, b))
        else:
            corner = (b[0], a[1])
            wires.append((a, corner))
            wires.append((corner, b))

    # junction dots where three or more wire ends / pins meet
    ends = Counter()
    for a, b in wires:
        ends[a] += 1
        ends[b] += 1
    pin_points = {p for comp in placed.values() for p in set(comp["pins"].values())}
    junctions = [pt for pt, n in ends.items() if n + (1 if pt in pin_points else 0) >= 3]

    body = []
    for a, b in wires:
        body.append(f'\t(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (stroke (width 0) (type default)) (uuid "{_uid()}"))')
    for pt in junctions:
        body.append(f'\t(junction (at {pt[0]} {pt[1]}) (diameter 0) (color 0 0 0 0) (uuid "{_uid()}"))')
    for n in notes or []:
        body.append(f'\t(text {json.dumps(n["text"])} (exclude_from_sim yes) (at {_snap(n["x"])} {_snap(n["y"])} 0)'
                    f'{_effects(False, "left")} (uuid "{_uid()}"))')

    title_block = f'\t(title_block (title {json.dumps(title)}))\n' if title else ""
    text = (
        "(kicad_sch\n\t(version 20250114)\n\t(generator \"kicad-mcp\")\n\t(generator_version \"10.0\")\n"
        f'\t(uuid "{root}")\n\t(paper "A4")\n{title_block}'
        "\t(lib_symbols\n" + "\n".join(e for e, _ in lib_entries.values()) + "\n\t)\n"
        + "\n".join(body) + "\n" + "\n".join(out_symbols) + "\n"
        '\t(sheet_instances (path "/" (page "1")))\n\t(embedded_fonts no)\n)\n'
    )
    summary = {
        "components": len(components),
        "wires": len(wires),
        "junctions": len(junctions),
        "pins": {ref: {k: v for k, v in c["pins"].items()} for ref, c in placed.items()},
    }
    return text, summary
