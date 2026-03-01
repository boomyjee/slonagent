from __future__ import annotations

import logging
from datetime import datetime

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Footer, Header, RichLog, Static, TabbedContent, TabPane

_LEVEL_COLORS = {
    "DEBUG": "dim",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold red reverse",
}


class Dashboard(App):
    CSS = """
    RichLog { scrollbar-gutter: stable; }
    TabPane { padding: 0; }
    VerticalScroll { scrollbar-gutter: stable; }
    Collapsible { margin: 0; border: none; padding: 0 1; }
    Collapsible > CollapsibleTitle { padding: 0; }
    Static.chat-msg { padding: 0 1 1 1; }
    """

    BINDINGS = [
        ("1", "switch_tab('chat')", "Chat"),
        ("2", "switch_tab('agent')", "Agent"),
        ("3", "switch_tab('memory')", "Memory"),
        ("4", "switch_tab('transport')", "Transport"),
        ("q,й", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="chat"):
            with TabPane("Chat", id="chat"):
                yield VerticalScroll(id="chat-scroll")
            with TabPane("Agent", id="agent"):
                yield RichLog(id="log-agent", wrap=True, highlight=False, markup=True)
            with TabPane("Memory", id="memory"):
                yield RichLog(id="log-memory", wrap=True, highlight=False, markup=True)
            with TabPane("Transport", id="transport"):
                yield RichLog(id="log-transport", wrap=True, highlight=False, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "SlonAgent"
        self.sub_title = "dashboard"

    def action_switch_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    # --- Chat helpers ---

    def _chat_scroll(self) -> VerticalScroll:
        return self.query_one("#chat-scroll", VerticalScroll)

    def _mount_chat(self, widget) -> None:
        scroll = self._chat_scroll()
        scroll.mount(widget)
        scroll.scroll_end(animate=False)

    def add_chat(self, role: str, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        if role == "user":
            header = f"[bold cyan]{ts} Вы[/bold cyan]"
        else:
            header = f"[bold green]{ts} Агент[/bold green]"
        self._mount_chat(Static(f"{header}\n{escape(text)}", markup=True, classes="chat-msg"))

    def add_collapsible(self, title: str, text: str) -> None:
        self._mount_chat(Collapsible(
            Static(escape(text)),
            title=title,
            collapsed=True,
        ))

    # --- Log tab ---

    def add_log(self, category: str, level: str, text: str) -> None:
        widget_id = f"log-{category}"
        try:
            log = self.query_one(f"#{widget_id}", RichLog)
        except Exception:
            return
        color = _LEVEL_COLORS.get(level, "white")
        ts = datetime.now().strftime("%H:%M:%S")
        log.write(f"[dim]{ts}[/dim] [{color}]{level}[/{color}] {text}")


class UILogHandler(logging.Handler):
    """Routes log records to the appropriate Dashboard tab."""

    def __init__(self, dashboard: Dashboard, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._dashboard = dashboard

    def _category(self, name: str) -> str:
        if name.startswith("src.memory") or name.startswith("memory"):
            return "memory"
        if name.startswith("aiogram") or name.startswith("src.transport") or name.startswith("httpx"):
            return "transport"
        return "agent"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            category = self._category(record.name)
            text = self.format(record)
            if " - " in text:
                text = text.split(" - ", 3)[-1]
            self._dashboard.call_later(self._dashboard.add_log, category, record.levelname, text)
        except Exception:
            self.handleError(record)
