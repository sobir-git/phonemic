"""Allowlisted phone controls for the local Herdr Unix socket API."""
import asyncio
import json
import os
import pathlib


class Herdr:
    """Expose only the pane controls used by the phone, over a private socket."""
    async def request(self, method, params):
        path = os.environ.get("PM_HERDR_SOCKET", os.environ.get(
            "HERDR_SOCKET_PATH", str(pathlib.Path.home() / ".config/herdr/herdr.sock")))
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(path, limit=2**20)
            try:
                writer.write((json.dumps({"id": "phonemic", "method": method,
                                          "params": params}) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", "Herdr command failed"))
                return response["result"]
            finally:
                writer.close()
                await writer.wait_closed()

    async def control(self, message):
        action = message.get("action")
        if action == "list":
            workspaces, panes, tabs = await asyncio.gather(
                self.request("workspace.list", {}), self.request("pane.list", {}),
                self.request("tab.list", {}))
            labels = {w["workspace_id"]: w["label"] for w in workspaces["workspaces"]}
            states = {w["workspace_id"]: w.get("agent_status", "unknown") for w in workspaces["workspaces"]}
            tab_labels = {t["tab_id"]: t.get("label", "") for t in tabs.get("tabs", [])}
            return {"panes": [{"id": p["pane_id"],
                               "workspace": labels.get(p["workspace_id"], p["workspace_id"]),
                               "workspace_id": p["workspace_id"],
                               "workspace_state": states.get(p["workspace_id"], "unknown"),
                               "state": p.get("agent_status", "unknown"),
                               "agent": p.get("agent", "terminal"),
                               "title": tab_labels.get(p.get("tab_id")) or
                                         p.get("terminal_title_stripped") or p.get("agent") or "Terminal",
                               "focused": p.get("focused", False)} for p in panes["panes"]]}
        if action == "workspace":
            label, cwd = message.get("label"), message.get("cwd", "")
            if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
                raise RuntimeError("Use a space name of up to 100 characters")
            if not isinstance(cwd, str) or len(cwd) > 4096 or any(ord(c) < 32 for c in cwd):
                raise RuntimeError("Invalid directory")
            if cwd and not pathlib.Path(cwd).is_absolute():
                raise RuntimeError("Use an absolute directory path")
            if not cwd and message.get("pane"):
                current = (await self.request("pane.get", {"pane_id": message["pane"]}))["pane"]
                cwd = current.get("foreground_cwd") or current.get("cwd") or ""
            result = await self.request("workspace.create", {"label": label.strip(), "cwd": cwd or None, "focus": True})
            return {"pane": result["root_pane"]["pane_id"]}
        pane = message.get("pane")
        if not isinstance(pane, str) or not pane or len(pane) > 128:
            raise RuntimeError("Select a pane first")
        if action == "read":
            result = await self.request("pane.read", {"pane_id": pane, "source": "visible",
                                                      "format": "ansi", "strip_ansi": False, "lines": 160})
            output = result["read"]
            text = output.get("text", "")
            if not isinstance(text, str):
                raise RuntimeError("Herdr returned invalid terminal output")
            return {"pane": pane, "text": text[:120000],
                    "truncated": bool(output.get("truncated")) or len(text) > 120000}
        if action == "split":
            current = (await self.request("pane.get", {"pane_id": pane}))["pane"]
            result = await self.request("pane.split", {"target_pane_id": pane,
                "workspace_id": current["workspace_id"], "direction": "down",
                "cwd": current.get("foreground_cwd") or current.get("cwd"), "focus": True})
            return {"pane": result["pane"]["pane_id"]}
        elif action == "close":
            await self.request("pane.close", {"pane_id": pane})
        elif action == "text":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 20000 or any(
                    (ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in text):
                raise RuntimeError("Use text of up to 20000 characters without terminal control codes")
            await self.request("pane.send_text", {"pane_id": pane, "text": text})
        elif action == "command":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise RuntimeError("Use a single-line command of up to 2000 characters")
            await self.request("pane.send_text", {"pane_id": pane, "text": text})
        elif action == "focus":
            await self.request("pane.focus", {"pane_id": pane})
        elif action == "scroll":
            direction = message.get("direction")
            if direction not in ("up", "down", "bottom"):
                raise RuntimeError("Unknown scroll direction")
            current = await self.request("pane.get", {"pane_id": pane})
            scroll = current["pane"].get("scroll", {})
            if current["pane"].get("agent") == "claude" and not scroll.get("max_offset_from_bottom", 0):
                layout = (await self.request("pane.layout", {"pane_id": pane}))["layout"]
                rect = next(p["rect"] for p in layout["panes"] if p["pane_id"] == pane)
                x, y = max(1, rect["width"] // 2), max(1, rect["height"] // 2)
                # Herdr 0.9 sends text as raw PTY bytes but rejects wheel/PageUp keys.
                # SGR mouse reports go straight to Claude, independent of desktop focus.
                wheel = f"\x1b[<{64 if direction == 'up' else 65};{x};{y}M"
                if direction == "bottom":
                    async def screen():
                        result = await self.request("pane.read", {"pane_id": pane,
                            "source": "visible", "format": "text", "lines": 160})
                        return result["read"]["text"]
                    previous = await screen()
                    for _ in range(10):
                        await self.request("pane.send_text", {"pane_id": pane, "text": wheel * 40})
                        await asyncio.sleep(0.1)
                        current_screen = await screen()
                        if current_screen == previous:
                            break
                        previous = current_screen
                    else:
                        raise RuntimeError("Moved toward latest output. Tap Latest again to continue.")
                else:
                    await self.request("pane.send_text", {"pane_id": pane, "text": wheel * 3})
                return {"pane": pane}
            offset = scroll.get("offset_from_bottom", 0)
            step = max(1, scroll.get("viewport_rows", 24) // 2)
            offset = 0 if direction == "bottom" else offset + (step if direction == "up" else -step)
            await self.request("pane.scroll", {"pane_id": pane, "offset_from_bottom":
                               max(0, min(scroll.get("max_offset_from_bottom", 0), offset))})
        elif action == "key":
            key, modifiers = message.get("key"), message.get("modifiers", [])
            if not isinstance(modifiers, list) or len(modifiers) > 2 or any(
                    m not in ("ctrl", "alt") for m in modifiers):
                raise RuntimeError("Unknown modifier")
            if key not in ("esc", "tab", "left", "right", "up", "down", "space", "backspace", "enter"):
                if key not in ("c", "u") or "ctrl" not in modifiers:
                    raise RuntimeError("Unknown key")
            keys = "+".join([m for m in ("ctrl", "alt") if m in modifiers] + [key])
            await self.request("pane.send_keys", {"pane_id": pane, "keys": [keys]})
        else:
            raise RuntimeError("Unknown Herdr action")
        return {"pane": pane}
