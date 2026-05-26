"""Render a representative TUI state to SVG, for README/PR previews.

Scripts a realistic agent constellation (CEO + parallel managers + a few
workers in varied states) by feeding fake events straight into a TUI
instance, then snapshots screen 1 (focus + hierarchy) and screen 2 (event
log) to SVG files via Rich's Console.record / save_svg.

Run with: python -m scripts.preview_tui [out_dir]

If out_dir is omitted, writes to ./preview/.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from taskgenie.events import Event
from taskgenie.tui import SCREEN_LOG, SCREEN_MAIN, TUI


def _spawn(tui: TUI, aid: str, name: str, role: str, depth: int, parent_id: str | None) -> None:
    tui._handle_event(
        Event(
            "spawn",
            aid,
            name,
            data={"role_type": role, "depth": depth, "parent_id": parent_id},
        )
    )


def _build_scenario() -> TUI:
    """Populate a TUI as if a Game Boy DMG emulator build were mid-flight."""
    tui = TUI(task="Build a Game Boy DMG emulator in Python with a working CPU, PPU and minimal MBC1 cart loader")

    # Hierarchy: CEO -> three parallel managers -> a few workers each.
    _spawn(tui, "ceo", "CEO", "ceo", 0, None)
    tui._handle_event(Event("state", "ceo", "CEO", data={"status": "working", "task": tui.task}))
    tui._handle_event(
        Event("narrate", "ceo", "CEO", data={"text": "Three workstreams in parallel: CPU, PPU, cartridge loader. Hiring department managers now."})
    )
    tui._handle_event(Event("tool_call", "ceo", "CEO", data={"tool": "hire_manager", "args": {"name": "CPUMgr", "role": "manager"}}))
    tui._handle_event(Event("hire", "ceo", "CEO", data={"child_name": "CPUMgr", "role_type": "manager"}))
    tui._handle_event(Event("tool_call", "ceo", "CEO", data={"tool": "hire_manager", "args": {"name": "PPUMgr"}}))
    tui._handle_event(Event("hire", "ceo", "CEO", data={"child_name": "PPUMgr", "role_type": "manager"}))
    tui._handle_event(Event("tool_call", "ceo", "CEO", data={"tool": "hire_manager", "args": {"name": "CartMgr"}}))
    tui._handle_event(Event("hire", "ceo", "CEO", data={"child_name": "CartMgr", "role_type": "manager"}))
    tui._handle_event(Event("tool_call", "ceo", "CEO", data={"tool": "hire_manager", "args": {"name": "AudioMgr"}}))
    tui._handle_event(Event("hire", "ceo", "CEO", data={"child_name": "AudioMgr", "role_type": "manager"}))
    tui._handle_event(Event("tool_call", "ceo", "CEO", data={"tool": "hire_manager", "args": {"name": "InputMgr"}}))
    tui._handle_event(Event("hire", "ceo", "CEO", data={"child_name": "InputMgr", "role_type": "manager"}))

    _spawn(tui, "cpu_mgr", "CPUMgr", "manager", 1, "ceo")
    _spawn(tui, "ppu_mgr", "PPUMgr", "manager", 1, "ceo")
    _spawn(tui, "cart_mgr", "CartMgr", "manager", 1, "ceo")
    _spawn(tui, "audio_mgr", "AudioMgr", "manager", 1, "ceo")
    _spawn(tui, "input_mgr", "InputMgr", "manager", 1, "ceo")

    # CPU department: large team to force tree overflow into a 2nd column.
    _spawn(tui, "cpu_w1", "OpcodeDecoder", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w2", "InterruptCtrl", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w3", "RegisterFile", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w4", "ALU", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w5", "MemoryBus", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w6", "CycleTimer", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w7", "InstructionTests", "worker", 2, "cpu_mgr")
    _spawn(tui, "cpu_w8", "DebugTracer", "worker", 2, "cpu_mgr")
    tui._handle_event(Event("state", "cpu_mgr", "CPUMgr", data={"status": "working", "task": "Implement the SM83 CPU core: registers, fetch-decode-execute, interrupts, timing."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "OpcodeDecoder", "task": "Implement all 0x00-0xFF opcodes plus the 0xCB prefix table."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "InterruptCtrl", "task": "Implement VBLANK/LCD/Timer/Serial/Joypad interrupt vectors and IME flag."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "RegisterFile", "task": "Implement A/F/B/C/D/E/H/L paired registers, flags, SP, PC."}))

    tui._handle_event(Event("state", "cpu_w3", "RegisterFile", data={"status": "working"}))
    tui._handle_event(Event("narrate", "cpu_w3", "RegisterFile", data={"text": "Modeling the 16-bit pair access (BC, DE, HL) as a property pair over two u8 fields. Flag register Z N H C will share a single u8."}))
    tui._handle_event(Event("tool_call", "cpu_w3", "RegisterFile", data={"tool": "write_workspace", "args": {"path": "cpu/registers.py", "mode": "create"}}))
    tui._handle_event(Event("complete", "cpu_w3", "RegisterFile", data={"report": "Registers + flags implemented with property pairs; unit tests pass."}))

    tui._handle_event(Event("state", "cpu_w2", "InterruptCtrl", data={"status": "working"}))
    tui._handle_event(Event("narrate", "cpu_w2", "InterruptCtrl", data={"text": "IME flag, IE/IF registers at 0xFFFF/0xFF0F, vector dispatch at 0x40/0x48/0x50/0x58/0x60."}))
    tui._handle_event(Event("tool_call", "cpu_w2", "InterruptCtrl", data={"tool": "read_workspace", "args": {"path": "cpu/registers.py"}}))
    tui._handle_event(Event("tool_call", "cpu_w2", "InterruptCtrl", data={"tool": "write_workspace", "args": {"path": "cpu/interrupts.py", "mode": "create"}}))
    tui._handle_event(
        Event("blocked", "cpu_w2", "InterruptCtrl", data={
            "reason": "The CPU clock is needed to determine the 5-cycle interrupt latency, but no clock module exists yet.",
            "needs": "A central clock/cycle counter, or permission to model latency as a constant for now.",
            "partial_progress": "IE/IF registers + IME flag wired; vector dispatch table written.",
        })
    )

    # Spotlight: opcode decoder is actively generating (this is what the user sees in focus).
    tui._handle_event(Event("state", "cpu_w1", "OpcodeDecoder", data={"status": "working"}))
    tui._handle_event(Event("narrate", "cpu_w1", "OpcodeDecoder", data={"text": "Generating the LD r,r' family (0x40-0x7F minus 0x76 HALT). 63 opcodes, all 1-cycle, dest=bits 5-3 src=bits 2-0."}))
    tui._handle_event(Event("tool_call", "cpu_w1", "OpcodeDecoder", data={"tool": "write_workspace", "args": {"path": "cpu/decoder.py", "mode": "create"}}))
    tui._handle_event(Event("narrate", "cpu_w1", "OpcodeDecoder", data={"text": "Moving on to ALU ops 0x80-0xBF: ADD/ADC/SUB/SBC/AND/XOR/OR/CP against A. Flag updates need care for half-carry."}))
    tui._handle_event(Event("tool_call", "cpu_w1", "OpcodeDecoder", data={"tool": "edit_workspace", "args": {"path": "cpu/decoder.py", "mode": "append"}}))
    tui._handle_event(Event("narrate", "cpu_w1", "OpcodeDecoder", data={"text": "Half-carry on ADD: ((a & 0x0F) + (b & 0x0F)) & 0x10 != 0. Same shape for ADC with carry-in added."}))
    tui._handle_event(Event("tool_call", "cpu_w1", "OpcodeDecoder", data={"tool": "edit_workspace", "args": {"path": "cpu/decoder.py", "mode": "append"}}))
    tui._handle_event(Event("narrate", "cpu_w1", "OpcodeDecoder", data={"text": "Now the 0xCB prefix table: BIT/RES/SET/RL/RR/SLA/SRA/SWAP/SRL across all 8 targets. Generating via a small DSL."}))

    # Mark several CPU workers active / done / blocked to make the column
    # feel like a real run.
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "ALU", "task": "Implement add/sub/and/or/xor/cp with half-carry and zero-flag updates."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "MemoryBus", "task": "8/16-bit reads + writes against the bus map, mirroring rules included."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "CycleTimer", "task": "Per-opcode T-cycle accounting and M-cycle stepping."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "InstructionTests", "task": "Generate the Blargg-style instr_timing fixture."}))
    tui._handle_event(Event("assign", "cpu_mgr", "CPUMgr", data={"agent_name": "DebugTracer", "task": "Cycle-by-cycle trace dump for diff against reference traces."}))
    tui._handle_event(Event("state", "cpu_w4", "ALU", data={"status": "working"}))
    tui._handle_event(Event("tool_call", "cpu_w4", "ALU", data={"tool": "write_workspace", "args": {"path": "cpu/alu.py"}}))
    tui._handle_event(Event("state", "cpu_w5", "MemoryBus", data={"status": "working"}))
    tui._handle_event(Event("state", "cpu_w6", "CycleTimer", data={"status": "working"}))
    tui._handle_event(Event("complete", "cpu_w7", "InstructionTests", data={"report": "instr_timing.gb-derived fixtures generated."}))
    tui._handle_event(Event("state", "cpu_w8", "DebugTracer", data={"status": "idle"}))

    # PPU department: manager working, several workers, varied states.
    _spawn(tui, "ppu_w1", "Renderer", "worker", 2, "ppu_mgr")
    _spawn(tui, "ppu_w2", "Tiles", "worker", 2, "ppu_mgr")
    _spawn(tui, "ppu_w3", "Sprites", "worker", 2, "ppu_mgr")
    _spawn(tui, "ppu_w4", "ScanlineFetcher", "worker", 2, "ppu_mgr")
    _spawn(tui, "ppu_w5", "PaletteDecoder", "worker", 2, "ppu_mgr")
    tui._handle_event(Event("state", "ppu_mgr", "PPUMgr", data={"status": "working", "task": "Implement the PPU: tile fetching, sprite layer, scanline timing, VBLANK."}))
    tui._handle_event(Event("assign", "ppu_mgr", "PPUMgr", data={"agent_name": "Renderer", "task": "Implement scanline-accurate rendering pipeline with mode 0/1/2/3 timing."}))
    tui._handle_event(Event("assign", "ppu_mgr", "PPUMgr", data={"agent_name": "Tiles", "task": "Tile data decode from VRAM 0x8000-0x97FF, background map 0x9800-0x9FFF."}))
    tui._handle_event(Event("assign", "ppu_mgr", "PPUMgr", data={"agent_name": "Sprites", "task": "OAM scan, 10-sprite-per-line limit, X-priority sort."}))
    tui._handle_event(Event("assign", "ppu_mgr", "PPUMgr", data={"agent_name": "ScanlineFetcher", "task": "Background + window fetcher state machine."}))
    tui._handle_event(Event("assign", "ppu_mgr", "PPUMgr", data={"agent_name": "PaletteDecoder", "task": "BGP/OBP0/OBP1 register handling for the DMG 4-shade palette."}))
    tui._handle_event(Event("state", "ppu_w1", "Renderer", data={"status": "working"}))
    tui._handle_event(Event("tool_call", "ppu_w1", "Renderer", data={"tool": "write_workspace", "args": {"path": "ppu/renderer.py"}}))
    tui._handle_event(Event("state", "ppu_w2", "Tiles", data={"status": "working"}))
    tui._handle_event(Event("state", "ppu_w3", "Sprites", data={"status": "working"}))
    tui._handle_event(Event("state", "ppu_w4", "ScanlineFetcher", data={"status": "blocked"}))
    tui._handle_event(Event("blocked", "ppu_w4", "ScanlineFetcher", data={"reason": "VRAM layout disagrees with Pan Docs by 4 bytes", "needs": "clarification on tile addressing mode", "partial_progress": "fetcher state machine drafted"}))
    tui._handle_event(Event("complete", "ppu_w5", "PaletteDecoder", data={"report": "BGP/OBP0/OBP1 working, palette swaps mid-frame."}))

    # Cart department: done.
    _spawn(tui, "cart_w1", "MBC1", "worker", 2, "cart_mgr")
    tui._handle_event(Event("state", "cart_mgr", "CartMgr", data={"status": "working", "task": "Cartridge loader with MBC1 support."}))
    tui._handle_event(Event("assign", "cart_mgr", "CartMgr", data={"agent_name": "MBC1", "task": "Parse the cart header + implement MBC1 ROM/RAM banking."}))
    tui._handle_event(Event("state", "cart_w1", "MBC1", data={"status": "working"}))
    tui._handle_event(Event("tool_call", "cart_w1", "MBC1", data={"tool": "write_workspace", "args": {"path": "cart/mbc1.py"}}))
    tui._handle_event(Event("complete", "cart_w1", "MBC1", data={"report": "MBC1 cart loader done; passes tetris.gb and dr_mario.gb header checks."}))
    tui._handle_event(Event("review", "cart_mgr", "CartMgr", data={"accepted": True, "feedback": "Solid. Header masks correct, banking model matches Pan Docs."}))

    # Audio department: square channels, wave, noise, mixer.
    _spawn(tui, "audio_w1", "Square1", "worker", 2, "audio_mgr")
    _spawn(tui, "audio_w2", "Square2", "worker", 2, "audio_mgr")
    _spawn(tui, "audio_w3", "WaveChannel", "worker", 2, "audio_mgr")
    _spawn(tui, "audio_w4", "NoiseChannel", "worker", 2, "audio_mgr")
    _spawn(tui, "audio_w5", "AudioMixer", "worker", 2, "audio_mgr")
    _spawn(tui, "audio_w6", "FrameSequencer", "worker", 2, "audio_mgr")
    tui._handle_event(Event("state", "audio_mgr", "AudioMgr", data={"status": "working", "task": "APU: four channels + mixer + frame sequencer at 512Hz."}))
    tui._handle_event(Event("state", "audio_w1", "Square1", data={"status": "working"}))
    tui._handle_event(Event("state", "audio_w2", "Square2", data={"status": "working"}))
    tui._handle_event(Event("state", "audio_w3", "WaveChannel", data={"status": "idle"}))
    tui._handle_event(Event("state", "audio_w4", "NoiseChannel", data={"status": "idle"}))
    tui._handle_event(Event("state", "audio_w5", "AudioMixer", data={"status": "idle"}))
    tui._handle_event(Event("state", "audio_w6", "FrameSequencer", data={"status": "working"}))

    # Input department: joypad + serial stub.
    _spawn(tui, "input_w1", "JoypadMatrix", "worker", 2, "input_mgr")
    _spawn(tui, "input_w2", "SerialStub", "worker", 2, "input_mgr")
    _spawn(tui, "input_w3", "KeyMap", "worker", 2, "input_mgr")
    tui._handle_event(Event("state", "input_mgr", "InputMgr", data={"status": "working", "task": "Joypad register + serial link cable stub."}))
    tui._handle_event(Event("state", "input_w1", "JoypadMatrix", data={"status": "working"}))
    tui._handle_event(Event("state", "input_w2", "SerialStub", data={"status": "idle"}))
    tui._handle_event(Event("complete", "input_w3", "KeyMap", data={"report": "SDL2 keymap done."}))

    # Some LLM bookkeeping so the header strip looks alive.
    for _ in range(47):
        tui._handle_event(Event("llm_call", "ceo", "CEO", data={"prompt_tokens": 3120, "completion_tokens": 580}))

    # Focus the CEO's view of the most recent worker generating right now.
    tui.focus_id = "cpu_w1"
    # Backdate spawn time so the [NEW] flash isn't showing on every agent.
    for v in tui.agents.values():
        v.spawn_time = time.time() - 30.0
    # Backdate elapsed timer so the header shows a non-trivial wall-clock.
    tui.start_time = time.time() - (7 * 60 + 23)
    # Backdate the focused worker's last_activity so we can show the
    # "thinking Ns…" indicator -- they're mid-LLM-call.
    tui.agents["cpu_w1"].last_activity = time.time() - 8.0
    return tui


def _render_screen(tui: TUI, screen: int, out_path: Path, title: str) -> None:
    tui._screen = screen
    # Height pulled down to a realistic laptop-terminal size so the
    # bigger scenario actually overflows the hierarchy panel and the
    # column-flow is visible in the preview.
    console = Console(record=True, width=180, height=34, force_terminal=True, color_system="truecolor")
    console.print(tui._render())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(out_path), title=title)
    print(f"wrote {out_path}")


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview")
    tui = _build_scenario()
    _render_screen(tui, SCREEN_MAIN, out_dir / "tui_screen1_focus_hierarchy.svg", "TaskGenie TUI — Screen 1: focus + hierarchy")
    tui2 = _build_scenario()  # fresh, so the focus screen render didn't leave state.
    _render_screen(tui2, SCREEN_LOG, out_dir / "tui_screen2_event_log.svg", "TaskGenie TUI — Screen 2: event log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
