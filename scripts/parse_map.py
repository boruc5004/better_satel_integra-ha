#!/usr/bin/env python3
"""Parse DLOADX printout text exports into a machine-readable JSON map.

Usage:
    pdftotext -layout zones.pdf zones.txt
    pdftotext -layout outputs.pdf outputs.txt
    pdftotext -layout partitions.pdf partitions.txt
    python3 scripts/parse_map.py --zones zones.txt --outputs outputs.txt \
        --partitions partitions.txt -o installation_map.json

The result is handy as ground truth when filling the integration's options
(gate state zones, climate bindings) or writing tests. The integration
itself does NOT need this file — it discovers the panel directly.
"""
import argparse
import json
import re


def parse_numlist(s: str) -> list[int]:
    """Parse '1,4÷6,14' style DLOADX lists into explicit ints."""
    out = []
    s = s.strip()
    if not s or s == "-":
        return out
    for part in s.split(","):
        part = part.strip()
        if not part or part == "-":
            continue
        if "÷" in part:
            a, b = part.split("÷")
            out.extend(range(int(a), int(b) + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out


def parse_zones(path):
    zones = {}
    zre = re.compile(r"^\s*(\d{1,3})\s+(.+?)\s+(\d{1,2})\s+(\d+):\s+(.*)$")
    for line in open(path, encoding="utf-8"):
        m = zre.match(line.rstrip("\n"))
        if not m:
            continue
        nr, name, part, ltype, rest = m.groups()
        nr, part, ltype = int(nr), int(part), int(ltype)
        if not (1 <= part <= 32) or nr < 1 or nr > 256:
            continue
        follow_output = None
        if ltype == 8:  # "Według wyjścia N" (zone follows an output)
            fm = re.match(r"Według wyjścia (\d+)\s+(.*)$", rest)
            if fm:
                follow_output = int(fm.group(1))
                rest = fm.group(2)
        rm2 = re.search(r"\d+\s*ms\s+(\d{1,3}):\s+(.+?)(?:\s{2,}|$)", rest)
        rm = re.search(r"(\d{1,3}):\s+(.+?)\s{2,}", rest + "  ")
        if rm2:
            react_code, react_desc = int(rm2.group(1)), rm2.group(2).strip()
        elif rm:
            react_code, react_desc = int(rm.group(1)), rm.group(2).strip()
        else:
            continue
        zones[nr] = {
            "name": name.strip(),
            "partition": part,
            "line_type": ltype,
            "reaction": react_code,
            "reaction_desc": react_desc,
        }
        if follow_output:
            zones[nr]["follows_output"] = follow_output
    return zones


def parse_outputs(path):
    outputs = {}
    ore = re.compile(
        r"^\s*(\d{1,3})\s+(.+?)\s+(\d{1,3}):\s+(.+?)\s+(\d+)\s*min\.\s*(\d+)\s*sek\.\s*(.*)$"
    )
    for line in open(path, encoding="utf-8"):
        m = ore.match(line.rstrip("\n"))
        if not m:
            continue
        nr, name, otype, otype_desc, mins, secs, rest = m.groups()
        nr, otype = int(nr), int(otype)
        if nr < 1 or nr > 256:
            continue
        entry = {
            "name": name.strip(),
            "function": otype,
            "function_desc": otype_desc.strip(),
            "time_s": int(mins) * 60 + int(secs),
        }
        sm = re.match(r"^(\d{1,3})\b", rest.strip())
        if sm and int(sm.group(1)) != 0:
            entry["state_zone"] = int(sm.group(1))
        tm = re.search(r"wejścia:\s*(.*)$", rest)
        if tm:
            seg = re.split(r"\s{2,}", tm.group(1).strip())[0]
            trig_part, _, timer_part = seg.partition("timery:")
            trig = parse_numlist(trig_part.rstrip(" ,"))
            if trig:
                entry["trigger_zones"] = trig
            tmrs = parse_numlist(timer_part)
            if tmrs:
                entry["timers"] = tmrs
        wm = re.search(r"wyjścia:\s*([\d,÷\s\-]+?)(?:\s{2,}|$)", rest)
        if wm:
            louts = parse_numlist(wm.group(1))
            if louts:
                entry["logic_outputs"] = louts
        outputs[nr] = entry
    return outputs


def parse_partitions(path):
    parts = {}
    pre = re.compile(r"^\s*(\d{1,2})\s+(.+?)\s+(\d+)\s+Załączana hasłem\s+([\d,÷]+)")
    for line in open(path, encoding="utf-8"):
        m = pre.match(line.rstrip("\n"))
        if m:
            nr = int(m.group(1))
            parts[nr] = {
                "name": m.group(2).strip(),
                "object": int(m.group(3)),
                "zones": parse_numlist(m.group(4)),
            }
    return parts


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--zones", help="pdftotext -layout output of the zones printout")
    ap.add_argument("--outputs", help="pdftotext -layout output of the outputs printout")
    ap.add_argument("--partitions", help="pdftotext -layout output of the partitions printout")
    ap.add_argument("-o", "--out", required=True, help="output JSON path")
    args = ap.parse_args()

    doc = {}
    if args.partitions:
        doc["partitions"] = parse_partitions(args.partitions)
    if args.zones:
        doc["zones"] = parse_zones(args.zones)
    if args.outputs:
        doc["outputs"] = parse_outputs(args.outputs)
    if not doc:
        ap.error("give at least one of --zones/--outputs/--partitions")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    print(f"wrote {args.out}: " + ", ".join(f"{len(v)} {k}" for k, v in doc.items()))


if __name__ == "__main__":
    main()
