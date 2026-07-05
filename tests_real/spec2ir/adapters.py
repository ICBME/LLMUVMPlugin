"""Adapters from benchmark samples to Spec2IR inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from .datasets import RealDataCase


@dataclass(frozen=True)
class VerilogPort:
    name: str
    direction: str
    width: int = 1
    width_expr: str | None = None

    def manifest_kind(self) -> str:
        return "bool" if self.width == 1 else "uint"


@dataclass(frozen=True)
class MaterializedSpec2IRInput:
    case: RealDataCase
    root: Path
    manifest_path: Path
    spec_path: Path
    target: str
    interface_ports: tuple[VerilogPort, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "case": self.case.to_dict(),
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "spec_path": str(self.spec_path),
            "target": self.target,
            "interface_ports": [
                {
                    "name": port.name,
                    "direction": port.direction,
                    "width": port.width,
                    "width_expr": port.width_expr,
                }
                for port in self.interface_ports
            ],
        }


DECLARATION_RE = re.compile(
    r"\b(input|output|inout)\b\s+([^;\n)]*)",
    flags=re.IGNORECASE,
)
MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)", flags=re.IGNORECASE)
WIDTH_RE = re.compile(r"\[([^\]]+)\]")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
TYPE_TOKENS = {
    "wire",
    "reg",
    "logic",
    "signed",
    "unsigned",
    "tri",
}


def materialize_verilogeval_case(case: RealDataCase, work_root: str | Path) -> MaterializedSpec2IRInput:
    target = sanitize_target_name(case.case_id)
    case_root = Path(work_root) / target
    case_root.mkdir(parents=True, exist_ok=True)
    prompt_text = case.prompt_path.read_text(encoding="utf-8", errors="replace")
    ref_text = (
        case.ref_path.read_text(encoding="utf-8", errors="replace")
        if case.ref_path is not None and case.ref_path.exists()
        else ""
    )
    ports = tuple(parse_verilog_module_ports(ref_text)) if ref_text else ()
    manifest_path = case_root / "manifest.toml"
    spec_path = case_root / "spec.md"
    manifest_path.write_text(render_manifest(target, ports), encoding="utf-8")
    spec_path.write_text(
        render_spec(case=case, prompt_text=prompt_text, ref_text=ref_text, ports=ports),
        encoding="utf-8",
    )
    return MaterializedSpec2IRInput(
        case=case,
        root=case_root,
        manifest_path=manifest_path,
        spec_path=spec_path,
        target=target,
        interface_ports=ports,
    )


def parse_verilog_module_ports(verilog_text: str) -> list[VerilogPort]:
    text = strip_verilog_comments(verilog_text)
    ports: list[VerilogPort] = []
    seen: set[tuple[str, str]] = set()
    for match in DECLARATION_RE.finditer(text):
        direction = match.group(1).lower()
        declaration = match.group(2)
        width_expr = extract_width_expr(declaration)
        width = width_from_expr(width_expr)
        names = extract_declared_names(declaration)
        for name in names:
            key = (direction, name)
            if key in seen:
                continue
            seen.add(key)
            ports.append(
                VerilogPort(
                    name=name,
                    direction=direction,
                    width=width,
                    width_expr=width_expr,
                )
            )
    return ports


def strip_verilog_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def extract_width_expr(declaration: str) -> str | None:
    match = WIDTH_RE.search(declaration)
    if match is None:
        return None
    return match.group(1).strip()


def width_from_expr(width_expr: str | None) -> int:
    if not width_expr:
        return 1
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", width_expr)
    if match is None:
        return 1
    left = int(match.group(1))
    right = int(match.group(2))
    return abs(left - right) + 1


def extract_declared_names(declaration: str) -> list[str]:
    declaration = WIDTH_RE.sub(" ", declaration)
    declaration = declaration.replace(")", " ").replace(";", " ").replace("\n", " ")
    names: list[str] = []
    for raw_part in declaration.split(","):
        tokens = [
            token
            for token in re.split(r"\s+", raw_part.strip())
            if token and token.lower() not in TYPE_TOKENS
        ]
        if not tokens:
            continue
        candidate = tokens[-1].split("=")[0].strip()
        candidate = candidate.strip("()[]{}")
        if IDENTIFIER_RE.match(candidate):
            names.append(candidate)
    return names


def render_manifest(target: str, ports: Iterable[VerilogPort]) -> str:
    lines = [
        f'name = "{target}"',
        "",
    ]
    for port in ports:
        if port.direction != "input":
            continue
        lines.extend(
            [
                "[[field]]",
                f'name = "{port.name}"',
                f'kind = "{port.manifest_kind()}"',
            ]
        )
        if port.width > 1:
            lines.append("min = 0")
            if port.width <= 62:
                lines.append(f"max = {2 ** port.width - 1}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_spec(
    *,
    case: RealDataCase,
    prompt_text: str,
    ref_text: str,
    ports: Iterable[VerilogPort],
) -> str:
    lines = [
        f"# VerilogEval {case.case_id}",
        "",
    ]
    lines.extend([behavior_prompt_text(prompt_text, ports=ports), ""])
    return "\n".join(lines)


def behavior_prompt_text(prompt_text: str, *, ports: Iterable[VerilogPort] = ()) -> str:
    lines: list[str] = []
    skip_continuation = False
    port_names = normalized_port_roles(ports)
    for raw_line in prompt_text.splitlines():
        line = raw_line.strip()
        if not line:
            skip_continuation = False
            continue
        lower = line.lower()
        if lower.startswith("i would like you to implement a module named"):
            skip_continuation = True
            continue
        if skip_continuation and (
            lower.startswith("interface")
            or lower.startswith("specified")
            or lower.startswith("with the following")
        ):
            continue
        skip_continuation = False
        if lower.startswith("interface"):
            continue
        if lower.startswith("all input and output ports are"):
            continue
        if line.startswith("-") and any(
            lower.lstrip("- ").startswith(prefix)
            for prefix in ("input", "output", "inout")
        ):
            continue
        lines.append(normalize_behavior_port_language(raw_line.rstrip(), port_names))
    return "\n".join(lines).strip() or prompt_text.strip()


def normalized_port_roles(ports: Iterable[VerilogPort]) -> dict[str, str]:
    inputs = [port.name for port in ports if port.direction == "input"]
    outputs = [port.name for port in ports if port.direction == "output"]
    return {
        "input": inputs[0] if len(inputs) == 1 else "source signal",
        "output": outputs[0] if len(outputs) == 1 else "destination signal",
    }


def normalize_behavior_port_language(text: str, port_names: dict[str, str]) -> str:
    source = port_names.get("input", "source signal")
    destination = port_names.get("output", "destination signal")
    replacements = [
        (r"\breports\b", "produces"),
        (r"\breport\b", "produce"),
        (r"\binput\s+port\b", source),
        (r"\boutput\s+port\b", destination),
        (r"\binput\s+ports\b", f"{source} signals"),
        (r"\boutput\s+ports\b", f"{destination} signals"),
        (r"\binput\s+vector\b", f"{source} vector"),
        (r"\boutput\s+vector\b", f"{destination} vector"),
        (r"\binput\s+vectors\b", f"{source} vectors"),
        (r"\boutput\s+vectors\b", f"{destination} vectors"),
        (r"\binput\s+bits?\b", f"{source} bits"),
        (r"\boutput\s+bits?\b", f"{destination} bits"),
        (r"\binputs\s+are\b", f"{source} signals are"),
        (r"\boutputs\s+are\b", f"{destination} signals are"),
        (r"\boutputs\s+the\b", "produces the"),
        (r"\boutputs\s+a\b", "produces a"),
        (r"\boutputs\s+an\b", "produces a"),
        (r"\bthe\s+input\b", f"the {source}"),
        (r"\bthe\s+inputs\b", f"the {source} signals"),
        (r"\ban\s+input\b", f"a {source}"),
        (r"\binputs\b", f"{source} signals"),
        (r"\binput\b", source),
        (r"\bthe\s+output\b", f"the {destination}"),
        (r"\bthe\s+outputs\b", f"the {destination} signals"),
        (r"\ban\s+output\b", f"a {destination}"),
        (r"\boutputs\b", f"{destination} signals"),
        (r"\boutput\b", destination),
        (r"\binterfaces\b", "signal groups"),
        (r"\binterface\b", "signal group"),
        (r"\bport\b", "signal"),
        (r"\bports\b", "signals"),
    ]
    normalized = text
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


def extract_module_name(verilog_text: str) -> str:
    match = MODULE_RE.search(strip_verilog_comments(verilog_text))
    return match.group(1) if match is not None else ""


def sanitize_target_name(value: str) -> str:
    target = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    if not target:
        return "verilogeval_case"
    if target[0].isdigit():
        return f"case_{target}"
    return target
