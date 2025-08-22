"""The accessibility tree, linearised into what a screen reader is handed.

Chromium builds an accessibility tree from the DOM and hands it to whatever
assistive technology is attached; NVDA, VoiceOver and Orca all read *this*, then
phrase it in their own words. So the tree is the part that can be asserted on and
reviewed without an actual screen reader: it holds the role, the name, the
description, the state and — importantly — the order.

What it cannot tell you is whether the result is *comprehensible*. That is why
`scripts/ax_transcript.py` writes it out for a person to read.
"""

from __future__ import annotations

from typing import Any

# Nodes that carry no information a reader would voice on its own.
SKIPPED_ROLES = frozenset(
    {
        "none",
        "generic",
        "GenericContainer",
        "InlineTextBox",
        "LineBreak",
        "ListMarker",
        "IframePresentational",
    }
)

# StaticText is kept: in browse mode a screen reader reads the prose, and leaving
# it out was hiding the readiness blockers -- the very thing that has to be
# comprehensible without seeing the badge beside it.

# Roles worth naming in the transcript even when they have no name of their own.
LANDMARKS = frozenset({"banner", "navigation", "main", "contentinfo", "region", "form"})


def _value(node: dict[str, Any], key: str) -> str:
    entry = node.get(key)
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "")
    return ""


def _states(node: dict[str, Any]) -> list[str]:
    """The properties a reader voices alongside the name."""
    interesting = {
        "required": "required",
        "invalid": "invalid",
        "disabled": "disabled",
        "checked": "checked",
        "pressed": "pressed",
        "expanded": "expanded",
        "level": "level",
    }
    out = []
    for prop in node.get("properties") or []:
        name = prop.get("name")
        value = _value(prop, "value")
        if name not in interesting or value in {"", "false", "none"}:
            continue
        out.append(f"{interesting[name]}={value}" if value != "true" else interesting[name])
    return out


def transcript(page: Any) -> list[str]:
    """One line per node a screen reader would announce, in reading order."""
    session = page.context.new_cdp_session(page)
    try:
        # Without this the tree is a lone RootWebArea: Chromium only builds the
        # full tree once something is listening, which is exactly what happens
        # when a real screen reader attaches.
        session.send("Accessibility.enable")
        nodes = session.send("Accessibility.getFullAXTree").get("nodes", [])
    finally:
        session.detach()

    by_id = {node["nodeId"]: node for node in nodes}
    lines: list[str] = []

    def walk(node_id: str, depth: int) -> None:
        node = by_id.get(node_id)
        if node is None:
            return
        # An ignored node is voiced by nobody, but its children still are -- most
        # of the page hangs under ignored `<div role=none>` wrappers.
        if node.get("ignored"):
            for child in node.get("childIds") or []:
                walk(child, depth)
            return
        role = _value(node, "role")
        name = _value(node, "name").strip()
        description = _value(node, "description").strip()
        value = _value(node, "value").strip()

        if role not in SKIPPED_ROLES and (name or value or role in LANDMARKS):
            parts = [role]
            if name:
                parts.append(f'"{name}"')
            if value:
                parts.append(f"value={value!r}")
            if description:
                parts.append(f"described as {description!r}")
            parts.extend(_states(node))
            lines.append("  " * depth + " ".join(parts))
            depth += 1

        for child in node.get("childIds") or []:
            walk(child, depth)

    roots = [n for n in nodes if not n.get("parentId")]
    for root in roots:
        walk(root["nodeId"], 0)
    return lines


def named_controls(page: Any) -> list[tuple[str, str]]:
    """(role, name) for everything focusable, in the order a reader meets it."""
    interactive = {
        "button",
        "link",
        "textbox",
        "combobox",
        "checkbox",
        "radio",
        "listbox",
        "searchbox",
        "menuitem",
        "disclosure triangle",
    }
    out = []
    for line in transcript(page):
        stripped = line.strip()
        role = stripped.split(" ", 1)[0]
        if role not in interactive:
            continue
        name = ""
        if '"' in stripped:
            name = stripped.split('"', 2)[1]
        out.append((role, name))
    return out
