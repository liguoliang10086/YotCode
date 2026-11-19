"""模态窗口：权限确认、模型/会话选择、帮助、提问和命令面板。

所有模态都通过 push_screen_wait() 返回结果，调用方 await 即可，
这样 Agent 的异步流程可以在等待用户操作时自然暂停。
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Markdown,
    OptionList,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from .. import models
from ..agent import PermissionRequest
from ..permissions import PermissionDecision
from ..session import SessionMeta
from .render import ACCENT, DIM, FAINT, GREEN, RED, TEXT, YELLOW, diff_renderable


# --------------------------------------------------------------------------
# permission
# --------------------------------------------------------------------------
class PermissionScreen(ModalScreen[PermissionDecision]):
    """Ask the user to allow or deny one tool invocation (or a plan)."""

    BINDINGS = [
        Binding("y", "allow", "Allow", show=False),
        Binding("a", "always", "Always allow", show=False),
        Binding("n", "deny", "Deny", show=False),
        Binding("escape", "deny", "Deny", show=False),
        Binding("tab", "focus_reason", "Why not", show=False),
    ]

    def __init__(self, request: PermissionRequest, permission_mode: str = "") -> None:
        super().__init__()
        self.request = request
        self.permission_mode = permission_mode
        self._allow_button: Button | None = None
        self._always_button: Button | None = None
        self._deny_button: Button | None = None

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        request = self.request
        is_plan = request.kind == "plan"
        title = "Plan ready for approval" if is_plan else "Permission required"
        subtitle = request.summary if not is_plan else (
            "Review the plan below. Approving switches to accept-edits mode "
            "so the work can be implemented."
        )
        subtitle_text = Text()
        if not is_plan:
            subtitle_text.append(f"{request.tool.name}  ", style=f"bold {ACCENT}")
        subtitle_text.append(subtitle, style=TEXT)
        with Vertical(id="dialog"):
            yield Static(Text(title, style=f"bold {TEXT}"), classes="dialog-title")
            yield Static(subtitle_text, classes="dialog-subtitle")
            if request.warning:
                yield Static(Text("! " + request.warning, style=f"bold {RED}"),
                             classes="dialog-warning")
            # acceptEdits 只自动放行"改文件"，执行命令仍然要确认。
            # 说清楚，免得用户以为模式没生效、白白重复确认。
            if (not is_plan and self.permission_mode == "acceptEdits"
                    and request.tool.name == "Bash"):
                yield Static(
                    Text("acceptEdits 只自动放行改文件（Write/Edit/MultiEdit）；"
                         "执行命令仍需确认 —— 按 a 可记住这条命令。", style=FAINT),
                    classes="dialog-hint",
                )
            preview = self._preview()
            if preview is not None:
                with VerticalScroll(id="dialog-preview"):
                    yield preview
            if request.suggestion and not is_plan:
                rule = Text()
                rule.append("Remember with: ", style=FAINT)
                rule.append(request.suggestion, style=f"{GREEN}")
                rule.append("   (press a)", style=FAINT)
                yield Static(rule, id="dialog-rule")
            with Horizontal(classes="dialog-buttons"):
                if is_plan:
                    self._allow_button = Button(
                        "Approve plan", variant="success", id="btn-allow"
                    )
                    self._deny_button = Button("Keep planning", id="btn-deny")
                else:
                    self._allow_button = Button(
                        "Yes, allow once", variant="success", id="btn-allow"
                    )
                    if request.suggestion:
                        self._always_button = Button(
                            "Yes, and don't ask again", id="btn-always"
                        )
                    self._deny_button = Button("No", variant="error", id="btn-deny")
                yield self._allow_button
                if self._always_button is not None:
                    yield self._always_button
                yield self._deny_button
            yield Static(
                Text(
                    "y allow · a always · n / esc deny · tab add a reason",
                    style=FAINT,
                ),
                classes="dialog-hint",
            )
            if not is_plan:
                yield Input(placeholder="Optional: tell YotCode why (n)", id="deny-reason")

    def _preview(self) -> Any:
        request = self.request
        if request.kind == "plan":
            plan = str(request.data.get("plan") or "")
            return Markdown(plan or "(no plan text provided)")
        if request.preview:
            path, before, after = request.preview
            return Static(
                diff_renderable(path, before, after, max_lines=400),
                markup=False,
            )
        data = request.data or {}
        if not data:
            return None
        if request.tool.name == "Bash" and set(data) <= {
            "command", "description", "timeout", "run_in_background",
        }:
            # The command is already the subtitle; a JSON echo adds no value.
            return None
        try:
            rendered = json.dumps(data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(data)
        if len(rendered) > 12_000:
            rendered = rendered[:12_000] + "\n… (truncated)"
        return Static(Text(rendered, style=DIM), markup=False)

    def on_mount(self) -> None:
        if self._deny_button is not None:
            self._deny_button.focus()

    # -- actions -----------------------------------------------------------
    def action_allow(self) -> None:
        self.dismiss(PermissionDecision(True))

    def action_always(self) -> None:
        request = self.request
        if not request.suggestion:
            self.action_allow()
            return
        self.dismiss(PermissionDecision(
            True, remember=True, persist=True, rule=request.suggestion
        ))

    def action_deny(self) -> None:
        reason = ""
        try:
            reason = self.query_one("#deny-reason", Input).value.strip()
        except Exception:
            pass
        self.dismiss(PermissionDecision(False, reason=reason or "the user denied this action"))

    def action_focus_reason(self) -> None:
        try:
            self.query_one("#deny-reason", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "deny-reason":
            self.action_deny()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-allow":
            self.action_allow()
        elif event.button.id == "btn-always":
            self.action_always()
        elif event.button.id == "btn-deny":
            self.action_deny()


# --------------------------------------------------------------------------
# model picker
# --------------------------------------------------------------------------
class ModelPickerScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, current: str, *, mock: bool = False) -> None:
        super().__init__()
        self.current = current
        self.mock = mock
        self._specs: list[models.ModelSpec] = models.list_models()

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(Text("Select a model", style=f"bold {TEXT}"), classes="dialog-title")
            yield Static(
                Text(
                    "Prices are estimates. Switching to another provider requires "
                    "that provider's API key.",
                    style=FAINT,
                ),
                classes="dialog-subtitle",
            )
            yield Input(placeholder="Filter models…", id="picker-input")
            yield OptionList(id="picker-list")
            yield Static(
                Text("enter select · esc cancel", style=FAINT), classes="dialog-hint"
            )

    def on_mount(self) -> None:
        self._populate("")
        try:
            self.query_one("#picker-input", Input).focus()
        except Exception:
            pass

    def _populate(self, needle: str) -> None:
        needle = needle.strip().lower()
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        for spec in self._specs:
            haystack = f"{spec.id} {spec.label} {spec.provider}".lower()
            if needle and needle not in haystack:
                continue
            marker = "●" if spec.id == self.current else " "
            prompt = Text()
            prompt.append(f"{marker} ", style=ACCENT)
            prompt.append(f"{spec.label}", style=TEXT)
            prompt.append(f"  {spec.id}", style=DIM)
            prompt.append(f"  · {spec.context_label}", style=FAINT)
            prompt.append(f"  · {spec.provider}", style=FAINT)
            options.add_option(Option(prompt, id=spec.id))
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-input":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker-input":
            return
        options = self.query_one("#picker-list", OptionList)
        option = options.highlighted_option or (
            options.get_option_at_index(0) if options.option_count else None
        )
        if option is not None and option.id:
            self.dismiss(str(option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# session picker
# --------------------------------------------------------------------------
class SessionPickerScreen(ModalScreen[SessionMeta | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, metas: Iterable[SessionMeta], current_id: str = "") -> None:
        super().__init__()
        self.metas = list(metas)
        self.current_id = current_id

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(Text("Resume a session", style=f"bold {TEXT}"),
                         classes="dialog-title")
            yield Static(
                Text(f"{len(self.metas)} session(s) in this directory", style=FAINT),
                classes="dialog-subtitle",
            )
            yield Input(placeholder="Filter sessions…", id="picker-input")
            yield OptionList(id="picker-list")
            yield Static(
                Text("enter resume · esc cancel", style=FAINT), classes="dialog-hint"
            )

    def on_mount(self) -> None:
        self._populate("")
        try:
            self.query_one("#picker-input", Input).focus()
        except Exception:
            pass

    def _populate(self, needle: str) -> None:
        needle = needle.strip().lower()
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        for meta in self.metas:
            title = meta.title or "(untitled)"
            haystack = f"{meta.id} {title} {meta.model}".lower()
            if needle and needle not in haystack:
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated))
            prompt = Text()
            marker = "●" if meta.id == self.current_id else " "
            prompt.append(f"{marker} ", style=ACCENT)
            prompt.append(f"{when}  ", style=DIM)
            prompt.append(title, style=TEXT)
            prompt.append(f"\n   {meta.id}  · {meta.turns} turns", style=FAINT)
            options.add_option(Option(prompt, id=meta.id))
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-input":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker-input":
            return
        options = self.query_one("#picker-list", OptionList)
        option = options.highlighted_option or (
            options.get_option_at_index(0) if options.option_count else None
        )
        if option is not None and option.id:
            self._resolve(str(option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self._resolve(str(event.option.id))

    def _resolve(self, session_id: str) -> None:
        for meta in self.metas:
            if meta.id == session_id:
                self.dismiss(meta)
                return
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# help
# --------------------------------------------------------------------------
class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, markdown: str) -> None:
        super().__init__()
        self.markdown_text = markdown

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static(Text("YotCode help", style=f"bold {TEXT}"), classes="dialog-title")
            with VerticalScroll(id="help-scroll"):
                yield Markdown(self.markdown_text)
            yield Static(
                Text("esc or q to close · scroll with ↑ ↓ / pgup / pgdn", style=FAINT),
                classes="dialog-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# question
# --------------------------------------------------------------------------
class QuestionScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, question: str, header: str, options: list[str],
                 descriptions: list[str], multi_select: bool = False) -> None:
        super().__init__()
        self.question = question
        self.header = header or "Question"
        self.option_labels = options
        self.descriptions = descriptions
        self.multi_select = multi_select
        self._selection: SelectionList[int] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.header, style=f"bold {TEXT}"), classes="dialog-title")
            yield Static(Text(self.question, style=TEXT), classes="dialog-subtitle")
            if self.multi_select:
                selections = []
                for index, label in enumerate(self.option_labels):
                    description = (
                        self.descriptions[index]
                        if index < len(self.descriptions) else ""
                    )
                    text = label + (f"  — {description}" if description else "")
                    selections.append(Selection(text, index, False))
                self._selection = SelectionList(*selections, id="question-options")
                yield self._selection
                with Horizontal(classes="dialog-buttons"):
                    yield Button("Submit", variant="success", id="btn-submit")
                    yield Button("Cancel", id="btn-cancel")
                yield Static(
                    Text(
                        "space toggles · tab to buttons · enter submits · esc cancels",
                        style=FAINT,
                    ),
                    classes="dialog-hint",
                )
            else:
                options = OptionList(id="question-options")
                for index, label in enumerate(self.option_labels):
                    description = (
                        self.descriptions[index]
                        if index < len(self.descriptions) else ""
                    )
                    prompt = Text()
                    prompt.append(f"{index + 1}. ", style=FAINT)
                    prompt.append(label, style=TEXT)
                    if description:
                        prompt.append(f"  — {description}", style=DIM)
                    options.add_option(Option(prompt, id=str(index)))
                yield options
                yield Input(
                    placeholder="Or type your own answer…", id="question-input"
                )
                yield Static(
                    Text(
                        "enter select · type a custom answer · esc cancel",
                        style=FAINT,
                    ),
                    classes="dialog-hint",
                )

    def on_mount(self) -> None:
        if self.multi_select:
            if self._selection is not None:
                self._selection.focus()
            return
        try:
            options = self.query_one("#question-options", OptionList)
            if options.option_count:
                options.highlighted = 0
            options.focus()
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            index = int(str(event.option.id))
            if 0 <= index < len(self.option_labels):
                self.dismiss(self.option_labels[index])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "question-input":
            value = event.value.strip()
            if value:
                self.dismiss(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return
        if event.button.id == "btn-submit" and self._selection is not None:
            chosen = sorted(self._selection.selected)
            if not chosen:
                self.dismiss(None)
                return
            labels = [self.option_labels[i] for i in chosen]
            self.dismiss(", ".join(labels))

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# command palette
# --------------------------------------------------------------------------
class CommandPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, items: Iterable[tuple[str, str, str]]) -> None:
        """items: (name, args, description)."""
        super().__init__()
        self.items = list(items)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(Text("Commands", style=f"bold {TEXT}"), classes="dialog-title")
            yield Input(placeholder="Type to filter…", id="picker-input")
            yield OptionList(id="picker-list")
            yield Static(
                Text("enter run · esc cancel", style=FAINT), classes="dialog-hint"
            )

    def on_mount(self) -> None:
        self._populate("")
        try:
            self.query_one("#picker-input", Input).focus()
        except Exception:
            pass

    def _populate(self, needle: str) -> None:
        needle = needle.strip().lower()
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        for name, args, description in self.items:
            haystack = f"{name} {description}".lower()
            if needle and needle not in haystack and not _fuzzy(needle, name):
                continue
            prompt = Text()
            prompt.append("/" + name, style=ACCENT)
            if args:
                prompt.append(" " + args, style=DIM)
            if description:
                prompt.append("  " + description, style=FAINT)
            options.add_option(Option(prompt, id=name))
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "picker-input":
            self._populate(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker-input":
            return
        options = self.query_one("#picker-list", OptionList)
        option = options.highlighted_option or (
            options.get_option_at_index(0) if options.option_count else None
        )
        if option is not None and option.id:
            self.dismiss(str(option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _fuzzy(needle: str, haystack: str) -> bool:
    """Tiny subsequence match so `cmp` finds `compact`."""
    needle = needle.replace(" ", "")
    haystack = haystack.lower().replace(" ", "")
    position = 0
    for char in needle:
        position = haystack.find(char, position)
        if position < 0:
            return False
        position += 1
    return True
