"""TUI do UltraBackup — interface terminal-style (curses), SPEC.md v1.1.

Arquitetura model/view ESTRITA:

* **Camada de modelo** (deste comentário até "ENGINE GLUE"): classes e funções
  PURAS — estado de listas com checkbox, filtro incremental, transições de
  tela, formatação de linhas de checkbox/plano/log. ZERO curses: o módulo
  ``curses`` só é importado tardiamente dentro de :func:`run_tui`, então
  ``import ultrabackup.tui`` nunca toca curses e a camada de modelo é
  testável por unittest puro (``tests/test_tui_model.py``).
* **Camada de visão** (a partir de "CAMADA DE VISÃO"): desenha o estado e
  traduz teclas. curses NÃO é thread-safe: somente a thread principal toca
  curses; workers (``threading.Thread``) postam eventos numa ``queue.Queue``
  que o loop de UI drena e aplica ao modelo.

Invariantes:

* A TUI nunca muta o sistema fora das chamadas de engine
  (``backup.do_backup`` / ``restore.apply_restore``), e ambas ficam atrás de
  telas de confirmação explícita (backup: enter + confirmação extra se o app
  estiver rodando; restore: y/N default N + re-checagem de app rodando +
  confirmação extra se houver divergência de versão — o análogo do bloqueio
  sem ``--force`` da CLI).
* Workers mutantes (do_backup/apply_restore) são threads NÃO-daemon:
  ``run_tui`` espera por eles antes de retornar, e Ctrl-C em qualquer ponto
  do loop de UI vira um 'q' — nunca mata uma cópia/apply no meio.
* Estimativas de tamanho nunca seguem symlinks (``os.lstat`` +
  ``fsutil.tree_size``, que usa ``lstat_walk``).
"""

from __future__ import annotations

import io
import os
import queue
import stat as stat_module
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__
from . import backup as backup_module
from . import discovery
from . import fsutil
from . import preflight
from . import report
from . import restore as restore_module

# curses é carregado tardiamente por _load_curses() (dentro de run_tui).
# A camada de modelo NUNCA usa este nome; importar ultrabackup.tui não
# importa curses.
curses = None


def _load_curses():
    """Importa curses sob demanda — mantém a camada de modelo livre de curses."""
    global curses
    if curses is None:
        import curses as _curses

        globals()["curses"] = _curses
    return curses


# ===========================================================================
# CAMADA DE MODELO — puro, sem curses, sem I/O
# ===========================================================================

# ---------------------------------------------------------------------------
# Telas e menu
# ---------------------------------------------------------------------------

SCREEN_HOME = "home"
SCREEN_APPS = "apps"          # multi-select de apps p/ backup
SCREEN_CONFIRM = "confirm"    # resumo + preflight + destino
SCREEN_EXECUTE = "execute"    # log de execução do backup
SCREEN_BACKUPS = "backups"    # lista de backups (restore/verify)
SCREEN_PLAN = "plan"          # tabela do plano de restore
SCREEN_RESULT = "result"      # resultado do restore --apply
SCREEN_OUTPUT = "output"      # saída rolável (verify/doctor)

MENU_ITEMS = [
    ("backup", "Backup de apps"),
    ("restore", "Restaurar backup"),
    ("verify", "Verificar backup"),
    ("doctor", "Doctor (preflights)"),
    ("quit", "Sair"),
]

# Alvo de tela de cada entrada do menu ("quit" encerra: alvo None).
MENU_TARGETS = {
    "backup": SCREEN_APPS,
    "restore": SCREEN_BACKUPS,
    "verify": SCREEN_BACKUPS,
    "doctor": SCREEN_OUTPUT,
    "quit": None,
}

# Banner ASCII desenhado à mão (estilo figlet "small").
BANNER = [
    r" _   _  _     _____  ___    _    ___    _     ___  _  __ _   _  ___  ",
    r"| | | || |   |_   _|| _ \  /_\  | _ )  /_\   / __|| |/ /| | | || _ \ ",
    r"| |_| || |__   | |  |   / / _ \ | _ \ / _ \ | (__ | ' < | |_| ||  _/ ",
    r" \___/ |____|  |_|  |_|_\/_/ \_\|___//_/ \_\ \___||_|\_\ \___/ |_|   ",
]

SUBTITLE = "backup file-level por app para macOS — v{}".format(__version__)

FOOTER_DEFAULT = (
    "↑↓/jk navegar · espaço marcar · enter confirmar · / filtrar · q voltar/sair"
)

# Resumo condensado do relatório de capacidade (o que o macOS proíbe).
NOT_CAPTURABLE_RESUMO = (
    "Keychain/safeStorage · TCC (privacidade) · BTM (itens de login) · "
    "recibo MAS · iCloud · SIP · hardlinks/sparse"
)

AVISO_SEGREDOS = (
    "AVISO: o backup pode conter segredos (token OAuth em ~/.claude.json, "
    "cookies, HTTPStorages). Diretório criado com chmod 700 — trate como "
    "material sensível."
)

MSG_SEM_TTY = (
    "erro: a TUI do UltraBackup precisa de um terminal interativo (TTY). "
    "Abra o Terminal e rode 'python3 -m ultrabackup tui', ou use os "
    "subcomandos da CLI (ex.: 'ultrabackup backup claude --yes')."
)


# ---------------------------------------------------------------------------
# Formatação (pura)
# ---------------------------------------------------------------------------

def human_size(num_bytes: int) -> str:
    """Formata bytes de forma legível (base 1024)."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return "{:d} B".format(int(value))
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{:d} B".format(int(num_bytes))


def format_checkbox_line(checked: bool, name: str, version: Optional[str] = None,
                         bundle_id: Optional[str] = None,
                         name_width: int = 28, version_width: int = 12,
                         size_text: Optional[str] = None) -> str:
    """Linha de checkbox: ``[x] Nome  versão  tamanho  bundle-id``."""
    mark = "x" if checked else " "
    line = "[{}] {:<{nw}}  {:<{vw}}".format(
        mark, name or "?", version or "?", nw=name_width, vw=version_width,
    )
    if size_text is not None:
        line += "  {:>9}".format(size_text)
    line += "  {}".format(bundle_id or "-")
    return line.rstrip()


def format_progress_line(entry: dict) -> str:
    """Evento de progresso do backup -> linha de log.

    ``[0001] app_bundle       … copied  (1.2 MiB)``
    """
    status = entry.get("status", "?")
    line = "[{}] {:<16} … {}".format(
        entry.get("id", "?"), entry.get("category", "?"), status
    )
    size = entry.get("size_bytes") or 0
    if status == "copied" and size:
        line += "  ({})".format(human_size(size))
    return line


def format_plan_line(entry: dict) -> str:
    """Linha da tabela do plano de restore (ação, categoria, alvo, flags)."""
    item = entry.get("item") or {}
    line = "{:<8} {:<18} {}".format(
        entry.get("action", "?"), item.get("category", "?"),
        entry.get("target", "?"),
    )
    extras = []
    if entry.get("conflict"):
        extras.append("conflito")
    if entry.get("live_newer"):
        extras.append("alvo mais novo")
    if entry.get("reason"):
        extras.append(str(entry.get("reason")))
    if extras:
        line += "  [{}]".format("; ".join(extras))
    return line


def format_backup_line(row: dict) -> str:
    """Linha da lista de backups existentes."""
    if not row.get("valid"):
        return "{:<20} {:<20} {:>9}  {:<8}  {}".format(
            "?", "?", "?", "INVÁLIDO", row.get("dir", "?")
        )
    return "{:<20} {:<20} {:>9}  {:<8}  {}".format(
        str(row.get("app", "?"))[:20],
        str(row.get("created_at", "?"))[:20],
        human_size(row.get("size_bytes") or 0),
        row.get("completeness", "?"),
        row.get("dir", "?"),
    )


def summarize_found(items: List[dict]) -> Tuple[int, Dict[str, int]]:
    """(total encontrados, contagem por categoria) de itens de discovery."""
    counts: Dict[str, int] = {}
    found = 0
    for item in items or []:
        if item.get("status") == "found":
            found += 1
            cat = item.get("category", "?")
            counts[cat] = counts.get(cat, 0) + 1
    return found, counts


def format_category_summary(counts: Dict[str, int]) -> str:
    if not counts:
        return "nenhum item encontrado"
    return " · ".join(
        "{}: {}".format(cat, n) for cat, n in sorted(counts.items())
    )


def scroll_window(cursor: int, offset: int, height: int) -> int:
    """Offset de rolagem que mantém ``cursor`` visível numa janela ``height``."""
    if height <= 0:
        return 0
    if cursor < offset:
        return cursor
    if cursor >= offset + height:
        return cursor - height + 1
    return max(0, offset)


def clamp_offset(offset: int, total: int, height: int) -> int:
    """Confina um offset de rolagem a [0, total-height]."""
    if height <= 0:
        return 0
    return max(0, min(offset, max(0, total - height)))


# ---------------------------------------------------------------------------
# Listas (puras)
# ---------------------------------------------------------------------------

class ScrollList:
    """Lista single-select com cursor e filtro incremental — pura."""

    def __init__(self, rows, text_fn: Optional[Callable] = None):
        self.rows = list(rows)
        self._text_fn = text_fn or (lambda row: str(row))
        self.filter_text = ""
        self.cursor = 0

    def visible_indices(self) -> List[int]:
        needle = self.filter_text.strip().lower()
        if not needle:
            return list(range(len(self.rows)))
        return [
            index for index, row in enumerate(self.rows)
            if needle in self._text_fn(row).lower()
        ]

    def visible(self) -> list:
        return [self.rows[index] for index in self.visible_indices()]

    def move(self, delta: int) -> None:
        count = len(self.visible_indices())
        if count == 0:
            self.cursor = 0
            return
        self.cursor = max(0, min(count - 1, self.cursor + delta))

    def set_filter(self, text: str) -> None:
        self.filter_text = text or ""
        self._clamp_cursor()

    def _clamp_cursor(self) -> None:
        count = len(self.visible_indices())
        self.cursor = 0 if count == 0 else max(0, min(self.cursor, count - 1))

    def current_index(self) -> Optional[int]:
        """Índice em ``rows`` da linha sob o cursor (None se lista vazia)."""
        indices = self.visible_indices()
        if not indices:
            return None
        return indices[min(self.cursor, len(indices) - 1)]

    def current_row(self):
        index = self.current_index()
        return None if index is None else self.rows[index]


class CheckboxList(ScrollList):
    """Lista multi-select: marcação sobrevive a mudanças de filtro."""

    def __init__(self, rows, text_fn: Optional[Callable] = None):
        super().__init__(rows, text_fn)
        self.checked = set()  # índices em self.rows

    def is_checked(self, row_index: int) -> bool:
        return row_index in self.checked

    def toggle_current(self) -> None:
        index = self.current_index()
        if index is None:
            return
        if index in self.checked:
            self.checked.discard(index)
        else:
            self.checked.add(index)

    def toggle_all_visible(self) -> None:
        """Marca todos os visíveis; se todos já marcados, desmarca-os."""
        indices = self.visible_indices()
        if not indices:
            return
        if all(index in self.checked for index in indices):
            for index in indices:
                self.checked.discard(index)
        else:
            self.checked.update(indices)

    def checked_rows(self) -> list:
        return [self.rows[index] for index in sorted(self.checked)]

    def checked_count(self) -> int:
        return len(self.checked)


# ---------------------------------------------------------------------------
# Estado global de navegação (puro)
# ---------------------------------------------------------------------------

class AppState:
    """Tela atual + pilha de navegação. ``go`` empilha; ``back`` desempilha."""

    def __init__(self):
        self.screen = SCREEN_HOME
        self.stack: List[str] = []
        self.quit = False
        self.menu = ScrollList(MENU_ITEMS, text_fn=lambda row: row[1])

    def go(self, screen: str) -> None:
        self.stack.append(self.screen)
        self.screen = screen

    def back(self) -> bool:
        if not self.stack:
            return False
        self.screen = self.stack.pop()
        return True

    def home(self) -> None:
        self.stack = []
        self.screen = SCREEN_HOME


# ---------------------------------------------------------------------------
# Fluxo de backup (modelo puro; eventos vêm dos workers via queue)
# ---------------------------------------------------------------------------

class ConfirmEntry:
    """Estado por app marcado na tela de confirmação."""

    def __init__(self, app):
        self.app = app
        self.name = getattr(app, "name", "?")
        self.version = getattr(app, "version", None)
        self.bundle_id = getattr(app, "bundle_id", None)
        self.items: Optional[List[dict]] = None  # None = discovery em andamento
        self.found = 0
        self.counts: Dict[str, int] = {}
        self.size_bytes: Optional[int] = None    # None = calculando ("…")
        self.running: List[str] = []
        self.running_checked = False  # False = checagem de processos pendente
        self.error: Optional[str] = None


class BackupFlow:
    """Estado do fluxo de backup: seleção -> confirmação -> execução."""

    def __init__(self, dest):
        self.dest = str(dest)
        self.selection: Optional[CheckboxList] = None  # None = carregando apps
        self.apps_error: Optional[str] = None
        self.apps_scroll = 0
        self.filter_active = False
        self.entries: List[ConfirmEntry] = []
        self.fda: Optional[str] = None
        self.fda_host: Optional[str] = None  # app do terminal a autorizar
        self.free_bytes: Optional[int] = None
        self.dest_warnings: List[str] = []
        self.confirm_scroll = 0
        self.editing_dest: Optional[str] = None  # buffer de edição ou None
        self.force_prompt = False   # confirmação extra: app em execução
        self.log: List[Tuple[str, str]] = []
        self.log_scroll: Optional[int] = None    # None = segue o fim do log
        self.log_view_offset = 0
        self.started = False
        self.done = False

    # -- consultas ---------------------------------------------------------

    def analyzing(self) -> bool:
        """True enquanto discovery OU a checagem de processos estiver pendente.

        A checagem de app em execução (evento ``running``) chega DEPOIS do
        evento ``analysis`` de cada app; sem exigi-la aqui, um enter entre os
        dois eventos iniciaria o backup sem a confirmação extra que a spec
        exige para app rodando.
        """
        return any(
            (entry.items is None and entry.error is None)
            or not entry.running_checked
            for entry in self.entries
        )

    def any_running(self) -> bool:
        return any(entry.running for entry in self.entries)

    def total_size(self) -> Tuple[bool, int]:
        """(todos calculados?, soma dos tamanhos conhecidos)."""
        total = 0
        complete = True
        for entry in self.entries:
            if entry.size_bytes is None:
                complete = False
            else:
                total += entry.size_bytes
        return complete, total

    # -- eventos dos workers ----------------------------------------------

    def apply_event(self, event: dict) -> None:
        """Aplica um evento postado por um worker (puro: modelo -> modelo)."""
        etype = event.get("type")
        if etype == "apps":
            rows = event.get("rows") or []
            self.selection = CheckboxList(
                rows,
                text_fn=lambda row: "{} {}".format(
                    row.get("name", ""), row.get("bundle_id") or ""
                ),
            )
            self.apps_error = event.get("error")
        elif etype == "analysis":
            entry = self._entry(event.get("index"))
            if entry is None:
                return
            if event.get("error"):
                entry.error = event["error"]
                entry.items = []
            else:
                entry.items = event.get("items") or []
                entry.found, entry.counts = summarize_found(entry.items)
        elif etype == "running":
            entry = self._entry(event.get("index"))
            if entry is not None:
                entry.running = event.get("procs") or []
                entry.running_checked = True
        elif etype == "size":
            entry = self._entry(event.get("index"))
            if entry is not None:
                entry.size_bytes = int(event.get("bytes") or 0)
        elif etype == "app_size":
            if self.selection is not None:
                index = event.get("row_index")
                if index is not None and 0 <= index < len(self.selection.rows):
                    self.selection.rows[index]["size_bytes"] = int(
                        event.get("bytes") or 0
                    )
        elif etype == "app_start":
            self.log.append(("info", ""))
            self.log.append(
                ("info", "=== {} — iniciando backup ===".format(event.get("name", "?")))
            )
        elif etype == "item":
            entry = event.get("entry") or {}
            status = entry.get("status")
            tag = "ok" if status == "copied" else (
                "warn" if status == "missing" else "err"
            )
            self.log.append((tag, format_progress_line(entry)))
        elif etype == "app_done":
            comp = event.get("completeness", "?")
            tag = "ok" if comp == "COMPLETE" else "warn"
            self.log.append((tag, "{}: {} — {}".format(
                event.get("name", "?"), comp, event.get("backup_dir", "?"))))
            self.log.append(("info", "  {}/{} itens capturados".format(
                event.get("copied", 0), event.get("total", 0))))
            self.log.append(
                ("info", "  NÃO capturável (macOS): " + NOT_CAPTURABLE_RESUMO)
            )
        elif etype == "app_error":
            self.log.append(("err", "ERRO em {}: {}".format(
                event.get("name", "?"), event.get("error", "?"))))
        elif etype == "log":
            self.log.append((event.get("tag", "info"), event.get("text", "")))
        elif etype == "all_done":
            self.done = True
            self.log.append(("info", ""))
            self.log.append(("warn", AVISO_SEGREDOS))
            self.log.append(
                ("ok", "Concluído. Pressione q ou enter para voltar ao início.")
            )

    def _entry(self, index) -> Optional[ConfirmEntry]:
        if index is None or not (0 <= index < len(self.entries)):
            return None
        return self.entries[index]


def build_confirm_lines(flow: BackupFlow) -> List[Tuple[str, str]]:
    """Linhas (tag, texto) da tela de confirmação — puro e testável."""
    lines: List[Tuple[str, str]] = []
    complete, total = flow.total_size()
    lines.append(("info", "Destino: {}   (d para editar)".format(flow.dest)))
    if flow.free_bytes is not None:
        lines.append(("info", "Espaço livre no destino: {}".format(
            human_size(flow.free_bytes))))
    if flow.fda == "denied":
        host = flow.fda_host or "seu app de terminal"
        lines.append(("err", "ACESSO TOTAL AO DISCO NEGADO — sem ele, "
                             "containers/cookies/HTTPStorages sairão vazios."))
        lines.append(("err", "  O que ligar: Ajustes > Privacidade e Segurança "
                             "> Acesso Total ao Disco > ATIVE '{}'.".format(host)))
        lines.append(("err", "  Pressione f para abrir os Ajustes já na tela "
                             "certa. Depois FECHE e reabra o {} e rode o "
                             "backup de novo.".format(host)))
    elif flow.fda == "unknown":
        lines.append(("warn", "FDA: status desconhecido (canário ausente)."))
    elif flow.fda == "ok":
        lines.append(("ok", "FDA: ok (Acesso Total ao Disco concedido)."))
    for warning in flow.dest_warnings:
        lines.append(("warn", "aviso: {}".format(warning)))
    if complete and flow.free_bytes is not None and total > flow.free_bytes:
        lines.append(("err", "ESPAÇO INSUFICIENTE: estimados {} > livres {}.".format(
            human_size(total), human_size(flow.free_bytes))))
    lines.append(("info", ""))
    lines.append(("info", "Apps marcados ({}):".format(len(flow.entries))))
    for entry in flow.entries:
        lines.append(("info", "  {}  {}  {}".format(
            entry.name, entry.version or "?", entry.bundle_id or "-")))
        if entry.error:
            lines.append(("err", "      erro na análise: {}".format(entry.error)))
        elif entry.items is None:
            lines.append(("warn", "      analisando… (discovery em andamento)"))
        else:
            lines.append(("info", "      itens encontrados: {} — {}".format(
                entry.found, format_category_summary(entry.counts))))
        size_txt = "…" if entry.size_bytes is None else human_size(entry.size_bytes)
        lines.append(("info", "      tamanho estimado: {}".format(size_txt)))
        if not entry.running_checked:
            lines.append(("warn", "      checando processos em execução…"))
        for proc in entry.running[:3]:
            lines.append(("err", "      [EM EXECUÇÃO] {}".format(proc)))
        if len(entry.running) > 3:
            lines.append(("err", "      [EM EXECUÇÃO] … e mais {} processo(s)".format(
                len(entry.running) - 3)))
    lines.append(("info", ""))
    lines.append(("info", "Total estimado: {}".format(
        human_size(total) if complete else "…")))
    if flow.any_running():
        lines.append(("err", "Há app(s) em execução — feche-os (Cmd+Q) e "
                             "pressione r para re-checar."))
        lines.append(("err", "Backup com app rodando pode corromper SQLite "
                             "WAL/LevelDB e exige confirmação extra."))
    return lines


# ---------------------------------------------------------------------------
# Fluxo de restore / telas de saída (modelo puro)
# ---------------------------------------------------------------------------

class RestoreFlow:
    """Estado do fluxo de restore/verify: lista -> plano -> apply -> resultado."""

    def __init__(self, dest, mode: str = "restore"):
        self.dest = str(dest)
        self.mode = mode  # "restore" | "verify"
        self.selection: Optional[ScrollList] = None  # None = carregando lista
        self.list_error: Optional[str] = None
        self.list_scroll = 0
        self.backup_dir: Optional[str] = None
        self.manifest: Optional[dict] = None
        self.plan: List[dict] = []
        self.plan_lines: List[Tuple[str, str]] = []
        self.warnings: List[Tuple[str, str]] = []
        self.skew: List[str] = []   # version_skew_check: exige confirmação extra
        self.running: List[str] = []
        self.plan_loading = False   # worker de plano em andamento
        self.checking = False       # re-checagem de processos em andamento
        self.scroll = 0
        # 0 nenhum; 1 y/N; 2 y/N com app rodando; 3 y/N divergência de versão
        self.confirm_stage = 0
        self.applying = False
        self.result_lines: Optional[List[Tuple[str, str]]] = None
        self.result_scroll = 0

    def apply_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "backups":
            self.list_error = event.get("error")
            self.selection = ScrollList(
                event.get("rows") or [],
                text_fn=lambda row: "{} {}".format(
                    row.get("app", ""), row.get("dir", "")
                ),
            )
        elif etype == "plan":
            self.plan_loading = False
            self.manifest = event.get("manifest")
            self.backup_dir = event.get("backup_dir")
            self.plan = event.get("plan") or []
            self.plan_lines = build_plan_lines(self.plan)
            self.warnings = event.get("warnings") or []
            self.skew = event.get("skew") or []
            self.running = event.get("running") or []
            self.scroll = 0
            self.confirm_stage = 0
        elif etype == "plan_error":
            self.plan_loading = False
        elif etype == "plan_running":
            self.checking = False
            self.running = event.get("procs") or []
        elif etype == "restore_done":
            self.applying = False
            self.result_lines = build_restore_result_lines(
                event.get("result"), self.backup_dir
            )
        elif etype == "restore_error":
            self.applying = False
            self.result_lines = [
                ("err", "erro durante o restore: {}".format(event.get("error", "?"))),
                ("warn", "Use 'ultrabackup rollback {}' para desfazer as "
                         "mutações registradas no journal.".format(self.backup_dir)),
            ]


class OutputScreen:
    """Tela simples de saída rolável (verify/doctor)."""

    def __init__(self, title: str):
        self.title = title
        self.lines: List[Tuple[str, str]] = []
        self.scroll = 0
        self.done = False

    def apply_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "output":
            self.lines.extend(event.get("lines") or [])
        elif etype == "output_done":
            self.done = True


def build_plan_lines(plan: List[dict]) -> List[Tuple[str, str]]:
    """Tabela do plano de restore como linhas (tag, texto) — puro."""
    restores = sum(1 for entry in plan if entry.get("action") == "restore")
    lines: List[Tuple[str, str]] = [
        ("info", "{} itens no plano — {} a restaurar, {} pulados".format(
            len(plan), restores, len(plan) - restores)),
        ("info", ""),
    ]
    for entry in plan:
        tag = "ok" if entry.get("action") == "restore" else "warn"
        lines.append((tag, format_plan_line(entry)))
    return lines


def build_restore_result_lines(result, backup_dir) -> List[Tuple[str, str]]:
    """Resultado de apply_restore -> linhas (tag, texto) — puro."""
    lines: List[Tuple[str, str]] = []
    if not isinstance(result, dict):
        lines.append(("ok", "Restore aplicado."))
        return lines
    failed = bool(result.get("rolled_back")) or not result.get("ok", True)
    if result.get("rolled_back"):
        if result.get("rollback_complete") is False:
            lines.append(("err", "Restore FALHOU; rollback automático "
                                 "INCOMPLETO — verifique o estado."))
        else:
            lines.append(("err", "Restore FALHOU e foi revertido "
                                 "(rollback automático)."))
        if result.get("error"):
            lines.append(("err", "  motivo: {}".format(result["error"])))
    elif not result.get("ok", True):
        lines.append(("err", "Restore FALHOU: {}".format(
            result.get("error") or "erro desconhecido")))
    else:
        lines.append(("ok", "Restore aplicado com sucesso."))
    for key, label in (("restored", "restaurados"), ("skipped", "pulados"),
                       ("moved_aside", "movidos de lado")):
        value = result.get(key)
        if isinstance(value, list):
            lines.append(("info", "  {}: {}".format(label, len(value))))
    for warning in result.get("warnings") or []:
        lines.append(("warn", "  aviso: {}".format(warning)))
    lines.append(("info", ""))
    if failed:
        lines.append(("warn", "Se necessário, desfaça com: "
                              "ultrabackup rollback {}".format(backup_dir)))
    else:
        lines.append(("info", "Para desfazer este restore: "
                              "ultrabackup rollback {}".format(backup_dir)))
    return lines


def build_verify_lines(result: dict) -> List[Tuple[str, str]]:
    """Resultado de restore.verify -> linhas (tag, texto) — puro."""
    lines: List[Tuple[str, str]] = []
    if result.get("ok"):
        lines.append(("ok", "OK: payload confere com o manifest."))
    else:
        lines.append(("err", "FALHA: divergências entre payload e manifest:"))
        for mismatch in result.get("mismatches", []):
            lines.append(("err", "  - item {} {}: {}".format(
                mismatch.get("item_id", "?"), mismatch.get("relpath", "?"),
                mismatch.get("reason", "?"))))
    return lines


def build_doctor_lines(result: dict) -> List[Tuple[str, str]]:
    """Resultado de preflight.doctor -> linhas (tag, texto) — puro."""
    lines: List[Tuple[str, str]] = []
    for problem in result.get("problems", []):
        lines.append(("err", "problema: {}".format(problem)))
    for warning in result.get("warnings", []):
        lines.append(("warn", "aviso: {}".format(warning)))
    if result.get("ok"):
        lines.append(("ok", "doctor: tudo ok."))
    else:
        lines.append(("err", "doctor: problemas encontrados (ver acima)."))
    return lines


# ===========================================================================
# ENGINE GLUE — I/O e workers; sem curses (rodam fora da thread de UI)
# ===========================================================================

def _estimate_item_size(item: dict) -> int:
    """Tamanho de um item de discovery. NUNCA segue symlinks (lstat/tree_size)."""
    path = item.get("path")
    if path is None or item.get("status") != "found":
        return 0
    try:
        st = os.lstat(str(path))
    except OSError:
        return 0
    if stat_module.S_ISDIR(st.st_mode):
        try:
            return fsutil.tree_size(Path(str(path)))
        except Exception:  # noqa: BLE001 - estimativa é cosmética
            return 0
    return st.st_size


def build_app_rows(home: Optional[Path] = None) -> List[dict]:
    """Apps instalados + entradas CLI-only do known_apps (sem .app instalado)."""
    rows: List[dict] = []
    seen_bids = set()
    seen_paths = set()
    for app in discovery.list_installed():
        rows.append({
            "app": app,
            "name": app.name,
            "version": app.version,
            "bundle_id": app.bundle_id,
            "cli_only": False,
            "size_bytes": None,   # None = ainda calculando ("…")
        })
        if app.bundle_id:
            seen_bids.add(app.bundle_id)
        if app.path:
            seen_paths.add(str(app.path))
    for key, entry in sorted(discovery._load_known_apps().items()):
        if not isinstance(entry, dict):
            continue
        if any(bid in seen_bids for bid in entry.get("match_bundle_ids", [])):
            continue
        if any((Path("/Applications") / (name + ".app")).is_dir()
               for name in entry.get("match_names", [])):
            continue
        try:
            app = discovery.find_app(key, home=home)
        except Exception:  # noqa: BLE001 - entrada irresolvível é só omitida
            continue
        if app.bundle_id and app.bundle_id in seen_bids:
            continue
        if app.path is not None and str(app.path) in seen_paths:
            continue
        rows.append({
            "app": app,
            "name": app.name,
            "version": app.version,
            "bundle_id": app.bundle_id,
            "cli_only": app.path is None,
            "size_bytes": None,
        })
    return rows


def app_cache_key(app) -> str:
    """Chave estável de cache de tamanhos por app."""
    return getattr(app, "bundle_id", None) or getattr(app, "name", "") or "?"


def list_backup_rows(dest: Path) -> List[dict]:
    """Backups (válidos ou não) diretamente sob ``dest`` — via load_backup."""
    rows: List[dict] = []
    dest = Path(dest)
    if not dest.is_dir():
        return rows
    try:
        entries = sorted(dest.iterdir())
    except OSError:
        return rows
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        try:
            manifest = restore_module.load_backup(entry)
        except Exception as exc:  # noqa: BLE001 - diretório alheio é ignorado
            if (entry / "payload").is_dir() or (entry / "manifest.json").is_file():
                rows.append({"dir": str(entry), "valid": False, "error": str(exc)})
            continue
        items = manifest.get("items", [])
        rows.append({
            "dir": str(entry),
            "valid": True,
            "app": (manifest.get("app") or {}).get("name", "?"),
            "created_at": manifest.get("created_at", "?"),
            "size_bytes": sum(int(i.get("size_bytes") or 0) for i in items),
            "completeness": manifest.get("completeness", "?"),
        })
    return rows


def _appinfo_from_manifest(manifest: dict):
    """Reconstrói um AppInfo do bloco ``app`` do manifest (p/ preflight)."""
    app = manifest.get("app", {}) or {}
    path = app.get("path")
    return discovery.AppInfo(
        name=app.get("name", ""),
        path=Path(path) if path else None,
        bundle_id=app.get("bundle_id"),
        version=app.get("version"),
        helpers=app.get("helpers", []) or [],
        mas_receipt=bool(app.get("mas_receipt", False)),
    )


# -- workers (threads de fundo; NUNCA tocam curses, só postam na fila) ------

def _apps_worker(home, out_queue, gen):
    try:
        rows = build_app_rows(home=home)
        out_queue.put({"type": "apps", "rows": rows, "error": None, "gen": gen})
    except Exception as exc:  # noqa: BLE001
        out_queue.put({"type": "apps", "rows": [], "error": str(exc), "gen": gen})


def _app_sizes_worker(apps, home, out_queue, gen, cache):
    """Tamanho estimado por app da lista de seleção, em ordem da lista.

    ``cache`` (dict chave->bytes) persiste entre telas/gerações: re-entrar na
    seleção mostra tamanhos já computados na hora. Escritas de dict são
    atômicas sob o GIL; o pior caso de corrida é recomputar um app.
    """
    for index, app in enumerate(apps):
        key = app_cache_key(app)
        cached = cache.get(key)
        if cached is None:
            try:
                items = discovery.discover(app, home=home)
                cached = sum(_estimate_item_size(item) for item in items)
            except Exception:  # noqa: BLE001 - app irresolvível: tamanho 0
                cached = 0
            cache[key] = cached
        out_queue.put({"type": "app_size", "row_index": index,
                       "bytes": cached, "gen": gen})


def _analysis_worker(apps, home, out_queue, gen, size_cache=None):
    """Discovery + processos vivos + tamanhos estimados, por app marcado."""
    all_items: List[List[dict]] = []
    for index, app in enumerate(apps):
        try:
            items = discovery.discover(app, home=home)
            out_queue.put({"type": "analysis", "index": index, "items": items,
                           "error": None, "gen": gen})
        except Exception as exc:  # noqa: BLE001
            items = []
            out_queue.put({"type": "analysis", "index": index, "items": [],
                           "error": str(exc), "gen": gen})
        all_items.append(items)
        try:
            procs = preflight.running_processes(app, home=home)
        except Exception:  # noqa: BLE001
            procs = []
        out_queue.put({"type": "running", "index": index, "procs": procs,
                       "gen": gen})
    # Tamanhos por último: são a parte lenta (tree_size é lstat-walk puro).
    # O cache da tela de seleção evita re-walk de árvores já medidas.
    for index, items in enumerate(all_items):
        key = app_cache_key(apps[index])
        total = size_cache.get(key) if size_cache is not None else None
        if total is None:
            total = sum(_estimate_item_size(item) for item in items)
            if size_cache is not None:
                size_cache[key] = total
        out_queue.put({"type": "size", "index": index, "bytes": total, "gen": gen})


def _recheck_worker(apps, home, out_queue, gen):
    """Re-checa processos vivos por app (tecla ``r`` na confirmação)."""
    for index, app in enumerate(apps):
        try:
            procs = preflight.running_processes(app, home=home)
        except Exception:  # noqa: BLE001
            procs = []
        out_queue.put({"type": "running", "index": index, "procs": procs,
                       "gen": gen})
    out_queue.put({"type": "recheck_done", "gen": gen})


def _backups_worker(dest, out_queue, gen):
    """Lista backups do destino (parse de manifests pode ser lento)."""
    try:
        rows = list_backup_rows(Path(dest).expanduser())
        out_queue.put({"type": "backups", "rows": rows, "error": None,
                       "gen": gen})
    except Exception as exc:  # noqa: BLE001
        out_queue.put({"type": "backups", "rows": [], "error": str(exc),
                       "gen": gen})


def _plan_worker(backup_dir, home, out_queue, gen):
    """load_backup + plan_restore + skew + processos — tudo fora da UI."""
    backup_dir = Path(backup_dir)
    try:
        manifest = restore_module.load_backup(backup_dir)
    except Exception as exc:  # noqa: BLE001
        out_queue.put({"type": "plan_error",
                       "error": "Backup inválido: {}".format(exc), "gen": gen})
        return
    try:
        plan = restore_module.plan_restore(manifest, backup_dir, home=home)
    except Exception as exc:  # noqa: BLE001
        out_queue.put({"type": "plan_error",
                       "error": "Erro ao planejar o restore: {}".format(exc),
                       "gen": gen})
        return
    warnings: List[Tuple[str, str]] = []
    app_path = (manifest.get("app") or {}).get("path")
    if app_path and not Path(app_path).exists():
        warnings.append(
            ("info", "o app não está instalado em {}; o bundle virá "
                     "integralmente do backup.".format(app_path))
        )
    skew: List[str] = []
    try:
        skew = list(restore_module.version_skew_check(manifest, home=home))
    except Exception:  # noqa: BLE001
        warnings.append(("warn", "não foi possível checar a versão instalada "
                                 "vs a do backup."))
    for warning in skew:
        warnings.append(("warn", "aviso de versão: {}".format(warning)))
    try:
        running = preflight.running_processes(
            _appinfo_from_manifest(manifest), home=home
        )
    except Exception:  # noqa: BLE001
        running = []
    out_queue.put({
        "type": "plan",
        "manifest": manifest,
        "backup_dir": str(backup_dir),
        "plan": plan,
        "warnings": warnings,
        "skew": skew,
        "running": running,
        "gen": gen,
    })


def _plan_running_worker(manifest, home, out_queue, gen, purpose):
    """Checa processos do app do manifest (``r`` no plano / pré-apply)."""
    try:
        procs = preflight.running_processes(
            _appinfo_from_manifest(manifest), home=home
        )
    except Exception:  # noqa: BLE001
        procs = []
    out_queue.put({"type": "plan_running", "purpose": purpose, "procs": procs,
                   "gen": gen})


def _backup_worker(jobs, dest, home, out_queue, gen):
    """Roda do_backup por app; cada item vira um evento via callback progress."""
    for app, items in jobs:
        out_queue.put({"type": "app_start", "name": app.name, "gen": gen})
        try:
            result = backup_module.do_backup(
                app, items, Path(dest), home=home,
                progress=lambda entry: out_queue.put(
                    {"type": "item", "entry": entry, "gen": gen}
                ),
            )
        except Exception as exc:  # noqa: BLE001
            out_queue.put({"type": "app_error", "name": app.name,
                           "error": str(exc), "gen": gen})
            continue
        manifest = result["manifest"]
        copied = sum(1 for item in manifest.get("items", [])
                     if item.get("status") == "copied")
        out_queue.put({
            "type": "app_done",
            "name": app.name,
            "backup_dir": str(result["backup_dir"]),
            "completeness": manifest.get("completeness", "?"),
            "copied": copied,
            "total": len(manifest.get("items", [])),
            "partial": bool(result.get("partial")),
            "gen": gen,
        })
    out_queue.put({"type": "all_done", "gen": gen})


def _apply_worker(plan, backup_dir, home, out_queue, gen):
    try:
        result = restore_module.apply_restore(plan, Path(backup_dir), home=home)
        out_queue.put({"type": "restore_done", "result": result, "gen": gen})
    except Exception as exc:  # noqa: BLE001
        out_queue.put({"type": "restore_error", "error": str(exc), "gen": gen})


def _verify_worker(backup_dir, out_queue, gen):
    try:
        result = restore_module.verify(Path(backup_dir))
        lines = build_verify_lines(result)
    except Exception as exc:  # noqa: BLE001
        lines = [("err", "erro no verify: {}".format(exc))]
    out_queue.put({"type": "output", "lines": lines, "gen": gen})
    out_queue.put({"type": "output_done", "gen": gen})


def _doctor_worker(dest, home, out_queue, gen):
    try:
        result = preflight.doctor(None, Path(dest), home=home)
        lines = build_doctor_lines(result)
    except Exception as exc:  # noqa: BLE001
        lines = [("err", "erro no doctor: {}".format(exc))]
    out_queue.put({"type": "output", "lines": lines, "gen": gen})
    out_queue.put({"type": "output_done", "gen": gen})


# ---------------------------------------------------------------------------
# Controller: liga modelo <-> engine. Sem curses; roda na thread de UI.
# ---------------------------------------------------------------------------

class _Controller:
    def __init__(self, home: Path, dest: Path):
        self.home = home
        self.dest = dest
        self.state = AppState()
        self.queue: "queue.Queue" = queue.Queue()
        self.notice = ""
        self.backup: Optional[BackupFlow] = None
        self.restore: Optional[RestoreFlow] = None
        self.output: Optional[OutputScreen] = None
        self.backup_gen = 0
        self.restore_gen = 0
        self.output_gen = 0
        # Tamanhos estimados por app (chave: bundle id ou nome), preenchido
        # pelos workers e reaproveitado entre telas/gerações.
        self.size_cache: Dict[str, int] = {}
        # Worker MUTANTE em andamento (do_backup/apply_restore). Não-daemon:
        # run_tui espera por ele antes de retornar (nunca mata cópia/apply no
        # meio — apply_restore só faz rollback automático se puder terminar).
        self.mutating: Optional[threading.Thread] = None

    # -- fila de eventos ---------------------------------------------------

    def drain_events(self) -> None:
        while True:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                return
            etype = event.get("type")
            if etype in ("apps", "analysis", "running", "size", "app_size",
                         "recheck_done", "app_start", "item", "app_done",
                         "app_error", "all_done", "log"):
                if self.backup is not None and event.get("gen") == self.backup_gen:
                    self.backup.apply_event(event)
                    if etype == "apps" and self.backup.selection is not None:
                        self.start_app_sizes()
                    if etype == "recheck_done":
                        self.notice = (
                            "Ainda há processo(s) em execução."
                            if self.backup.any_running()
                            else "Nenhum processo do(s) app(s) em execução."
                        )
            elif etype in ("backups", "plan", "plan_error", "plan_running",
                           "restore_done", "restore_error"):
                if self.restore is not None and event.get("gen") == self.restore_gen:
                    self.restore.apply_event(event)
                    if etype == "plan":
                        if self.state.screen == SCREEN_BACKUPS:
                            self.state.go(SCREEN_PLAN)
                    elif etype == "plan_error":
                        self.notice = event.get("error") or "erro ao abrir o backup"
                    elif etype == "plan_running":
                        self._after_plan_running(event)
            elif etype in ("output", "output_done"):
                if self.output is not None and event.get("gen") == self.output_gen:
                    self.output.apply_event(event)

    def _after_plan_running(self, event: dict) -> None:
        """Decide o próximo passo após a checagem de processos do plano."""
        flow = self.restore
        if event.get("purpose") == "apply_check":
            # Re-checagem obrigatória antes do apply: app rodando exige a
            # confirmação extra (estágio 2); divergência de versão exige a
            # confirmação extra de skew (estágio 3) — a CLI bloqueia sem
            # --force, a TUI bloqueia sem este "y" explícito.
            if flow.running:
                flow.confirm_stage = 2
            elif flow.skew:
                flow.confirm_stage = 3
            else:
                self.start_apply()
        else:
            self.notice = ("Ainda há processo(s) em execução." if flow.running
                           else "Nenhum processo do app em execução.")

    # -- fluxo de backup ---------------------------------------------------

    def open_backup_flow(self) -> None:
        self.backup_gen += 1
        self.backup = BackupFlow(self.dest)
        self.state.go(SCREEN_APPS)
        threading.Thread(
            target=_apps_worker, args=(self.home, self.queue, self.backup_gen),
            daemon=True,
        ).start()

    def start_app_sizes(self) -> None:
        """Tamanhos por app na tela de seleção (worker de fundo + cache)."""
        apps = [row.get("app") for row in self.backup.selection.rows]
        threading.Thread(
            target=_app_sizes_worker,
            args=(apps, self.home, self.queue, self.backup_gen,
                  self.size_cache),
            daemon=True,
        ).start()

    def open_confirm(self) -> None:
        # Geração nova: eventos de um _analysis_worker anterior (voltar da
        # confirmação, mudar a seleção e re-entrar) carregam índices de OUTRA
        # lista de entries e seriam aplicados ao app errado — descarta-os.
        self.backup_gen += 1
        flow = self.backup
        rows = flow.selection.checked_rows()
        flow.entries = [ConfirmEntry(row["app"]) for row in rows]
        try:
            flow.fda = preflight.fda_probe(home=self.home)
        except Exception:  # noqa: BLE001
            flow.fda = "unknown"
        flow.fda_host = preflight.terminal_host_app()
        self.refresh_dest_checks()
        self.state.go(SCREEN_CONFIRM)
        apps = [entry.app for entry in flow.entries]
        threading.Thread(
            target=_analysis_worker,
            args=(apps, self.home, self.queue, self.backup_gen,
                  self.size_cache),
            daemon=True,
        ).start()

    def refresh_dest_checks(self) -> None:
        flow = self.backup
        flow.dest_warnings = []
        dest = Path(flow.dest).expanduser()
        try:
            flow.free_bytes = fsutil.free_space(dest)
        except OSError:
            flow.free_bytes = None
        try:
            cloud = preflight._cloud_sync_location(dest)
        except Exception:  # noqa: BLE001
            cloud = None
        if cloud:
            flow.dest_warnings.append(
                "destino dentro de {} — o backup (com segredos) será "
                "sincronizado para a nuvem.".format(cloud)
            )

    def recheck_running(self) -> None:
        """Re-checa processos em thread de fundo (``ps`` travaria a UI)."""
        flow = self.backup
        if flow is None or not flow.entries:
            return
        apps = [entry.app for entry in flow.entries]
        threading.Thread(
            target=_recheck_worker,
            args=(apps, self.home, self.queue, self.backup_gen),
            daemon=True,
        ).start()
        self.notice = "Re-checando processos…"

    def start_backup(self) -> None:
        flow = self.backup
        flow.force_prompt = False
        jobs = [(entry.app, entry.items or []) for entry in flow.entries]
        flow.started = True
        flow.log = [("info", "Destino: {}".format(flow.dest))]
        self.state.go(SCREEN_EXECUTE)
        # Não-daemon: um Ctrl-C não pode matar do_backup no meio da cópia.
        self.mutating = threading.Thread(
            target=_backup_worker,
            args=(jobs, Path(flow.dest).expanduser(), self.home, self.queue,
                  self.backup_gen),
            daemon=False,
        )
        self.mutating.start()

    # -- fluxo de restore/verify ------------------------------------------

    def open_restore_flow(self, mode: str) -> None:
        self.restore_gen += 1
        flow = RestoreFlow(self.dest, mode=mode)
        self.restore = flow
        self.state.go(SCREEN_BACKUPS)
        # Lista em thread de fundo: load_backup parseia manifests inteiros
        # (um sha256 por arquivo do payload) e travaria a UI em backups grandes.
        threading.Thread(
            target=_backups_worker,
            args=(self.dest, self.queue, self.restore_gen),
            daemon=True,
        ).start()

    def open_plan(self, row: dict) -> None:
        # Geração nova por hygiene: um _plan_worker antigo não pode preencher
        # o plano de outra linha selecionada.
        self.restore_gen += 1
        flow = self.restore
        flow.plan_loading = True
        threading.Thread(
            target=_plan_worker,
            args=(row["dir"], self.home, self.queue, self.restore_gen),
            daemon=True,
        ).start()

    def request_apply_check(self) -> None:
        """Re-checagem obrigatória de app rodando ANTES do apply (async)."""
        flow = self.restore
        flow.checking = True
        threading.Thread(
            target=_plan_running_worker,
            args=(flow.manifest, self.home, self.queue, self.restore_gen,
                  "apply_check"),
            daemon=True,
        ).start()

    def refresh_plan_running(self) -> None:
        """Tecla ``r`` no plano: re-checa processos em thread de fundo."""
        flow = self.restore
        flow.checking = True
        threading.Thread(
            target=_plan_running_worker,
            args=(flow.manifest, self.home, self.queue, self.restore_gen,
                  "recheck"),
            daemon=True,
        ).start()
        self.notice = "Re-checando processos…"

    def start_apply(self) -> None:
        flow = self.restore
        flow.confirm_stage = 0
        flow.applying = True
        flow.result_lines = None
        self.state.go(SCREEN_RESULT)
        # Não-daemon: um Ctrl-C não pode matar apply_restore no meio de uma
        # mutação (o rollback automático só roda se a thread viver).
        self.mutating = threading.Thread(
            target=_apply_worker,
            args=(flow.plan, flow.backup_dir, self.home, self.queue,
                  self.restore_gen),
            daemon=False,
        )
        self.mutating.start()

    def start_verify(self, row: dict) -> None:
        self.output_gen += 1
        self.output = OutputScreen("Verify — {}".format(row["dir"]))
        self.state.go(SCREEN_OUTPUT)
        threading.Thread(
            target=_verify_worker,
            args=(row["dir"], self.queue, self.output_gen),
            daemon=True,
        ).start()

    def start_doctor(self) -> None:
        self.output_gen += 1
        self.output = OutputScreen("Doctor — preflights")
        self.state.go(SCREEN_OUTPUT)
        threading.Thread(
            target=_doctor_worker,
            args=(Path(self.dest).expanduser(), self.home, self.queue,
                  self.output_gen),
            daemon=True,
        ).start()


# ===========================================================================
# CAMADA DE VISÃO — curses; SÓ a thread principal executa este bloco
# ===========================================================================

def run_tui(home: Optional[Path] = None, dest: Optional[Path] = None) -> int:
    """Entrada da TUI. Exige TTY; retorna um exit code da tabela da spec."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(MSG_SEM_TTY, file=sys.stderr)
        return report.EXIT_USAGE

    _load_curses()
    try:
        import locale

        locale.setlocale(locale.LC_ALL, "")
    except Exception:  # noqa: BLE001 - locale é cosmético
        pass

    home = Path(home) if home is not None else Path.home()
    dest = Path(dest) if dest else home / "UltraBackups"
    controller = _Controller(home, dest)

    # stderr de chamadas de engine (ex.: warnings do do_backup) corromperia a
    # tela do curses; captura durante a sessão e despeja ao sair.
    captured = io.StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    interrupted = False
    mutating_alive = False
    try:
        curses.wrapper(_ui_main, controller)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        sys.stderr = real_stderr
        # NUNCA abandona do_backup/apply_restore no meio: o worker mutante é
        # não-daemon e é aguardado até o fim (apply_restore faz o rollback
        # automático sozinho em caso de erro — desde que a thread viva).
        worker = controller.mutating
        mutating_alive = worker is not None and worker.is_alive()
        if mutating_alive:
            print("Aguardando a operação em andamento terminar com "
                  "segurança…", file=real_stderr)
            worker.join()
        leftover = captured.getvalue()
        if leftover.strip():
            print(leftover, file=real_stderr, end="")
    if interrupted and mutating_alive:
        print("Sessão interrompida durante backup/restore; a operação foi "
              "concluída antes de sair.", file=real_stderr)
        return report.EXIT_ERROR
    return report.EXIT_OK


def _ui_main(stdscr, ctl: _Controller) -> None:
    try:
        curses.curs_set(0)
    except Exception:  # noqa: BLE001 - terminais sem suporte
        pass
    attrs = _init_colors()
    stdscr.timeout(100)  # getch não-bloqueante: eventos de fundo redesenham

    while not ctl.state.quit:
        # Ctrl-C pode chegar FORA do getch (durante drain/draw): trata como
        # 'q' em qualquer ponto do corpo do loop — as telas de execução/apply
        # recusam 'q' e assim um SIGINT nunca derruba um worker mutante.
        try:
            ctl.drain_events()
            _draw(stdscr, ctl, attrs)
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                ch = ord("q")
            if ch == -1:
                continue
            _handle_key(ctl, ch)
        except KeyboardInterrupt:
            _handle_key(ctl, ord("q"))


def _init_colors() -> Dict[str, int]:
    try:
        curses.start_color()
        curses.use_default_colors()
        if not curses.has_colors():
            return {}
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
    except Exception:  # noqa: BLE001 - sem cores, segue monocromático
        return {}
    return {
        "ok": curses.color_pair(1),
        "info": curses.color_pair(2),
        "warn": curses.color_pair(3) | curses.A_BOLD,  # amarelo/laranja
        "err": curses.color_pair(4) | curses.A_BOLD,
    }


def _put(stdscr, y: int, x: int, text, attr: int = 0) -> None:
    """addstr defensivo: recorta na largura e ignora erros de borda."""
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width - 1:
        return
    try:
        stdscr.addnstr(y, x, str(text), width - x - 1, attr)
    except Exception:  # noqa: BLE001 - escrever no canto inferior direito falha
        pass


def prompt_user() -> str:
    """Usuário real do prompt ``<user>@ultrabackup:~$`` ('visitor' se falhar)."""
    try:
        import getpass
        name = getpass.getuser().strip()
    except Exception:  # noqa: BLE001
        name = ""
    return name or "visitor"


def _draw_prompt(stdscr, y: int, x: int, attrs: Dict[str, int],
                 suffix: str = "") -> None:
    """Prompt fake ``<user>@ultrabackup:~$`` em verde/ciano."""
    user = prompt_user()
    _put(stdscr, y, x, user, attrs.get("ok", 0) | _bold())
    col = x + len(user)
    _put(stdscr, y, col, "@", 0)
    _put(stdscr, y, col + 1, "ultrabackup", attrs.get("info", 0) | _bold())
    col += 1 + len("ultrabackup")
    _put(stdscr, y, col, ":~$", attrs.get("ok", 0))
    if suffix:
        _put(stdscr, y, col + 4, suffix, 0)


def _bold() -> int:
    return curses.A_BOLD


def _draw_footer(stdscr, text: str, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    _put(stdscr, height - 1, 0, " " * (width - 1), curses.A_REVERSE)
    _put(stdscr, height - 1, 1, text, curses.A_REVERSE)


def _draw_notice(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    if ctl.notice:
        height, _ = stdscr.getmaxyx()
        _put(stdscr, height - 2, 1, ctl.notice, attrs.get("warn", 0))


def _draw_tagged_lines(stdscr, lines: List[Tuple[str, str]], top: int,
                       bottom: int, offset: int, attrs: Dict[str, int]) -> None:
    for row in range(bottom - top):
        index = offset + row
        if index >= len(lines):
            break
        tag, text = lines[index]
        _put(stdscr, top + row, 1, text, attrs.get(tag, 0))


def _draw(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    stdscr.erase()
    screen = ctl.state.screen
    if screen == SCREEN_HOME:
        _draw_home(stdscr, ctl, attrs)
    elif screen == SCREEN_APPS:
        _draw_apps(stdscr, ctl, attrs)
    elif screen == SCREEN_CONFIRM:
        _draw_confirm(stdscr, ctl, attrs)
    elif screen == SCREEN_EXECUTE:
        _draw_execute(stdscr, ctl, attrs)
    elif screen == SCREEN_BACKUPS:
        _draw_backups(stdscr, ctl, attrs)
    elif screen == SCREEN_PLAN:
        _draw_plan(stdscr, ctl, attrs)
    elif screen == SCREEN_RESULT:
        _draw_result(stdscr, ctl, attrs)
    elif screen == SCREEN_OUTPUT:
        _draw_output(stdscr, ctl, attrs)
    _draw_notice(stdscr, ctl, attrs)
    stdscr.refresh()


# -- telas ------------------------------------------------------------------

def _draw_home(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    y = 1
    banner_width = max(len(line) for line in BANNER)
    if width > banner_width + 2 and height > len(BANNER) + 12:
        x = max(1, (width - banner_width) // 2)
        for line in BANNER:
            _put(stdscr, y, x, line, attrs.get("ok", 0) | _bold())
            y += 1
    else:
        _put(stdscr, y, max(1, (width - 11) // 2), "ULTRABACKUP",
             attrs.get("ok", 0) | _bold())
        y += 1
    y += 1
    _put(stdscr, y, max(1, (width - len(SUBTITLE)) // 2), SUBTITLE,
         attrs.get("info", 0))
    y += 2
    _draw_prompt(stdscr, y, 2, attrs, suffix=" menu")
    _put(stdscr, y, 2 + 23 + 5, "_", attrs.get("warn", 0) | curses.A_BLINK)
    y += 2

    menu = ctl.state.menu
    for index, (_mid, label) in enumerate(menu.rows):
        marker = "> " if index == menu.cursor else "  "
        attr = curses.A_REVERSE if index == menu.cursor else 0
        _put(stdscr, y + index, 4, "{}{}".format(marker, label), attr)
    _draw_footer(stdscr, FOOTER_DEFAULT, attrs)


def _draw_apps(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.backup
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" backup --select")
    _put(stdscr, 1, 1, "Selecione os apps para backup (espaço marca, "
                       "enter continua):", attrs.get("info", 0))
    top, bottom = 3, height - 3
    if flow is None or flow.selection is None:
        _put(stdscr, top, 2, "Carregando lista de apps…", attrs.get("warn", 0))
    else:
        sel = flow.selection
        if flow.apps_error:
            _put(stdscr, top, 2, "erro ao listar apps: {}".format(flow.apps_error),
                 attrs.get("err", 0))
            top += 1
        if flow.filter_active or sel.filter_text:
            _put(stdscr, top, 2, "filtro: {}{}".format(
                sel.filter_text, "_" if flow.filter_active else ""),
                attrs.get("warn", 0))
            top += 1
        visible_idx = sel.visible_indices()
        area = bottom - top
        flow.apps_scroll = scroll_window(sel.cursor, flow.apps_scroll, area)
        if not visible_idx:
            _put(stdscr, top, 2, "(nenhum app corresponde ao filtro)",
                 attrs.get("warn", 0))
        for row in range(area):
            pos = flow.apps_scroll + row
            if pos >= len(visible_idx):
                break
            rindex = visible_idx[pos]
            item = sel.rows[rindex]
            checked = sel.is_checked(rindex)
            version = item.get("version") or ("cli-only" if item.get("cli_only")
                                              else None)
            size_bytes = item.get("size_bytes")
            size_text = "…" if size_bytes is None else human_size(size_bytes)
            line = format_checkbox_line(checked, item.get("name", "?"),
                                        version, item.get("bundle_id"),
                                        size_text=size_text)
            if pos == sel.cursor:
                attr = curses.A_REVERSE
            elif checked:
                attr = attrs.get("warn", 0)
            else:
                attr = 0
            _put(stdscr, top + row, 2, line, attr)
        _put(stdscr, height - 2, 1, "{} marcado(s) de {} app(s)".format(
            sel.checked_count(), len(sel.rows)), attrs.get("info", 0))
    _draw_footer(
        stdscr,
        "↑↓/jk navegar · espaço marcar · a marcar visíveis · / filtrar · "
        "enter continuar · q voltar",
        attrs,
    )


def _draw_confirm(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.backup
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" backup --confirm")
    _put(stdscr, 1, 1, "Confirmação de backup", attrs.get("info", 0) | _bold())
    top = 3
    if flow.editing_dest is not None:
        _put(stdscr, top, 1, "Novo destino: {}_".format(flow.editing_dest),
             attrs.get("warn", 0) | _bold())
        top += 2
    lines = build_confirm_lines(flow)
    bottom = height - 4
    flow.confirm_scroll = clamp_offset(flow.confirm_scroll, len(lines),
                                       bottom - top)
    _draw_tagged_lines(stdscr, lines, top, bottom, flow.confirm_scroll, attrs)

    if flow.force_prompt:
        warn_attr = attrs.get("err", 0) | curses.A_REVERSE
        _put(stdscr, height - 4, 1, " CONFIRMAÇÃO EXTRA — app em execução: "
                                    "risco real de corrupção (SQLite WAL/LevelDB). ",
             warn_attr)
        _put(stdscr, height - 3, 1, " Pressione s para prosseguir MESMO ASSIM · "
                                    "qualquer outra tecla cancela. ", warn_attr)
        footer = "s prosseguir mesmo assim · outra tecla cancelar"
    elif flow.editing_dest is not None:
        footer = "enter confirmar destino · esc cancelar edição"
    elif flow.fda == "denied":
        footer = ("f liberar acesso ao disco · enter iniciar backup · d editar "
                  "destino · r re-checar app rodando · esc/q voltar")
    else:
        footer = ("enter iniciar backup · d editar destino · r re-checar app "
                  "rodando · ↑↓ rolar · esc/q voltar")
    _draw_footer(stdscr, footer, attrs)


def _draw_execute(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.backup
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" backup --run")
    status = "concluído" if flow.done else "em andamento…"
    tag = "ok" if flow.done else "warn"
    _put(stdscr, 1, 1, "Execução do backup — {}".format(status),
         attrs.get(tag, 0) | _bold())
    top, bottom = 3, height - 2
    area = bottom - top
    if flow.log_scroll is None:
        offset = max(0, len(flow.log) - area)
    else:
        offset = clamp_offset(flow.log_scroll, len(flow.log), area)
        flow.log_scroll = offset
        if offset >= max(0, len(flow.log) - area):
            flow.log_scroll = None  # voltou ao fim: segue o log de novo
    flow.log_view_offset = offset
    _draw_tagged_lines(stdscr, flow.log, top, bottom, offset, attrs)
    if flow.done:
        footer = "q/enter voltar ao início · ↑↓ rolar"
    else:
        footer = "backup em andamento — aguarde (↑↓ rolar o log)"
    _draw_footer(stdscr, footer, attrs)


def _draw_backups(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.restore
    label = "restore" if flow.mode == "restore" else "verify"
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" {} --list".format(label))
    _put(stdscr, 1, 1, "Backups em {}:".format(flow.dest), attrs.get("info", 0))
    top, bottom = 3, height - 2
    sel = flow.selection
    if sel is None:
        _put(stdscr, top, 2, "Carregando backups…", attrs.get("warn", 0))
        _draw_footer(stdscr, "carregando — aguarde · q voltar", attrs)
        return
    if flow.list_error:
        _put(stdscr, top, 2, "erro ao listar backups: {}".format(flow.list_error),
             attrs.get("err", 0))
        top += 1
    rows = sel.visible()
    if not rows:
        _put(stdscr, top, 2, "Nenhum backup encontrado em {}.".format(flow.dest),
             attrs.get("warn", 0))
    else:
        area = bottom - top
        flow.list_scroll = scroll_window(sel.cursor, flow.list_scroll, area)
        for row in range(area):
            pos = flow.list_scroll + row
            if pos >= len(rows):
                break
            entry = rows[pos]
            line = format_backup_line(entry)
            if pos == sel.cursor:
                attr = curses.A_REVERSE
            elif not entry.get("valid"):
                attr = attrs.get("err", 0)
            else:
                attr = 0
            _put(stdscr, top + row, 2, line, attr)
    if flow.plan_loading:
        footer = "planejando restore… aguarde"
    else:
        footer = "↑↓/jk navegar · enter selecionar · q voltar"
    _draw_footer(stdscr, footer, attrs)


def _draw_plan(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.restore
    manifest = flow.manifest or {}
    app = manifest.get("app") or {}
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" restore --plan")
    _put(stdscr, 1, 1, "Plano de restore — {} ({})".format(
        app.get("name", "?"), manifest.get("created_at", "?")),
        attrs.get("info", 0) | _bold())
    _put(stdscr, 2, 1, "Backup: {}".format(flow.backup_dir), attrs.get("info", 0))
    top = 3
    for tag, text in flow.warnings:
        _put(stdscr, top, 1, text, attrs.get(tag, 0))
        top += 1
    if flow.running:
        _put(stdscr, top, 1, "APP EM EXECUÇÃO — feche-o (Cmd+Q) e pressione r "
                             "para re-checar:", attrs.get("err", 0) | _bold())
        top += 1
        for proc in flow.running[:2]:
            _put(stdscr, top, 3, proc, attrs.get("err", 0))
            top += 1
    top += 1
    bottom = height - 4
    flow.scroll = clamp_offset(flow.scroll, len(flow.plan_lines), bottom - top)
    _draw_tagged_lines(stdscr, flow.plan_lines, top, bottom, flow.scroll, attrs)

    if flow.checking:
        _put(stdscr, height - 3, 1, " Checando processos do app… ",
             attrs.get("warn", 0) | curses.A_REVERSE)
        footer = "checando processos — aguarde"
    elif flow.confirm_stage == 1:
        _put(stdscr, height - 3, 1, " Aplicar o restore agora? Isto MUTA o "
                                    "sistema (move-aside + journal). [y/N] ",
             attrs.get("warn", 0) | curses.A_REVERSE)
        footer = "y aplicar · qualquer outra tecla cancelar (default N)"
    elif flow.confirm_stage == 2:
        _put(stdscr, height - 3, 1, " APP EM EXECUÇÃO — risco de corrupção. "
                                    "Prosseguir MESMO ASSIM? [y/N] ",
             attrs.get("err", 0) | curses.A_REVERSE)
        footer = "y prosseguir mesmo assim · qualquer outra tecla cancelar"
    elif flow.confirm_stage == 3:
        _put(stdscr, height - 3, 1, " DIVERGÊNCIA DE VERSÃO — dados podem ser "
                                    "incompatíveis. Aplicar MESMO ASSIM? [y/N] ",
             attrs.get("err", 0) | curses.A_REVERSE)
        footer = ("y aplicar mesmo assim · outra tecla cancelar "
                  "(a CLI bloquearia sem --force)")
    else:
        footer = ("↑↓ rolar · enter aplicar (pede confirmação y/N) · "
                  "r re-checar processos · q voltar")
    _draw_footer(stdscr, footer, attrs)


def _draw_result(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    flow = ctl.restore
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" restore --apply")
    top, bottom = 2, height - 2
    if flow.applying and flow.result_lines is None:
        _put(stdscr, top, 1, "Aplicando restore… não interrompa.",
             attrs.get("warn", 0) | _bold())
        footer = "aplicando — aguarde"
    else:
        lines = flow.result_lines or []
        flow.result_scroll = clamp_offset(flow.result_scroll, len(lines),
                                          bottom - top)
        _draw_tagged_lines(stdscr, lines, top, bottom, flow.result_scroll, attrs)
        footer = "↑↓ rolar · q voltar ao início"
    _draw_footer(stdscr, footer, attrs)


def _draw_output(stdscr, ctl: _Controller, attrs: Dict[str, int]) -> None:
    height, width = stdscr.getmaxyx()
    screen = ctl.output
    _draw_prompt(stdscr, 0, 1, attrs, suffix=" " + (screen.title if screen else ""))
    top, bottom = 2, height - 2
    if screen is None or (not screen.lines and not screen.done):
        _put(stdscr, top, 1, "Executando…", attrs.get("warn", 0))
    else:
        screen.scroll = clamp_offset(screen.scroll, len(screen.lines),
                                     bottom - top)
        _draw_tagged_lines(stdscr, screen.lines, top, bottom, screen.scroll, attrs)
    _draw_footer(stdscr, "↑↓ rolar · q voltar", attrs)


# -- teclado ----------------------------------------------------------------

def _is_enter(ch: int) -> bool:
    return ch in (10, 13, curses.KEY_ENTER)


def _is_up(ch: int) -> bool:
    return ch in (curses.KEY_UP, ord("k"))


def _is_down(ch: int) -> bool:
    return ch in (curses.KEY_DOWN, ord("j"))


def _is_backspace(ch: int) -> bool:
    return ch in (curses.KEY_BACKSPACE, 127, 8)


def _handle_key(ctl: _Controller, ch: int) -> None:
    if ch == curses.KEY_RESIZE:
        # Dimensões são relidas a cada draw; nada a fazer além de redesenhar.
        return
    ctl.notice = ""
    screen = ctl.state.screen
    if screen == SCREEN_HOME:
        _key_home(ctl, ch)
    elif screen == SCREEN_APPS:
        _key_apps(ctl, ch)
    elif screen == SCREEN_CONFIRM:
        _key_confirm(ctl, ch)
    elif screen == SCREEN_EXECUTE:
        _key_execute(ctl, ch)
    elif screen == SCREEN_BACKUPS:
        _key_backups(ctl, ch)
    elif screen == SCREEN_PLAN:
        _key_plan(ctl, ch)
    elif screen == SCREEN_RESULT:
        _key_result(ctl, ch)
    elif screen == SCREEN_OUTPUT:
        _key_output(ctl, ch)


def _key_home(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    if _is_up(ch):
        state.menu.move(-1)
    elif _is_down(ch):
        state.menu.move(1)
    elif _is_enter(ch):
        row = state.menu.current_row()
        menu_id = row[0] if row else None
        if menu_id == "quit":
            state.quit = True
        elif menu_id == "backup":
            ctl.open_backup_flow()
        elif menu_id == "restore":
            ctl.open_restore_flow("restore")
        elif menu_id == "verify":
            ctl.open_restore_flow("verify")
        elif menu_id == "doctor":
            ctl.start_doctor()
    elif ch in (ord("q"), 27):
        state.quit = True


def _key_apps(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.backup
    sel = flow.selection if flow else None
    if sel is None:
        if ch in (ord("q"), 27):
            state.home()
        return
    if flow.filter_active:
        if ch == 27:
            sel.set_filter("")
            flow.filter_active = False
        elif _is_enter(ch):
            flow.filter_active = False
        elif _is_backspace(ch):
            sel.set_filter(sel.filter_text[:-1])
        elif ch == curses.KEY_UP:
            sel.move(-1)
        elif ch == curses.KEY_DOWN:
            sel.move(1)
        elif 32 <= ch < 127:
            sel.set_filter(sel.filter_text + chr(ch))
        return
    if _is_up(ch):
        sel.move(-1)
    elif _is_down(ch):
        sel.move(1)
    elif ch == curses.KEY_PPAGE:
        sel.move(-10)
    elif ch == curses.KEY_NPAGE:
        sel.move(10)
    elif ch == ord(" "):
        sel.toggle_current()
    elif ch == ord("a"):
        sel.toggle_all_visible()
    elif ch == ord("/"):
        flow.filter_active = True
    elif _is_enter(ch):
        if sel.checked_count() >= 1:
            ctl.open_confirm()
        else:
            ctl.notice = "Marque pelo menos um app com espaço antes de continuar."
    elif ch in (ord("q"), 27):
        state.home()


def _key_confirm(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.backup
    if flow.editing_dest is not None:
        if _is_enter(ch):
            text = flow.editing_dest.strip()
            if text:
                flow.dest = str(Path(text).expanduser())
            flow.editing_dest = None
            ctl.refresh_dest_checks()
        elif ch == 27:
            flow.editing_dest = None
        elif _is_backspace(ch):
            flow.editing_dest = flow.editing_dest[:-1]
        elif 32 <= ch < 127:
            flow.editing_dest += chr(ch)
        return
    if flow.force_prompt:
        # Confirmação extra explícita para backup com app em execução.
        if ch in (ord("s"), ord("S"), ord("y"), ord("Y")):
            ctl.start_backup()
        else:
            flow.force_prompt = False
            ctl.notice = "Backup cancelado (app em execução)."
        return
    if _is_up(ch):
        flow.confirm_scroll = max(0, flow.confirm_scroll - 1)
    elif _is_down(ch):
        flow.confirm_scroll += 1
    elif ch == ord("d"):
        flow.editing_dest = flow.dest
    elif ch == ord("r"):
        ctl.recheck_running()
    elif ch == ord("f"):
        host = flow.fda_host or "seu app de terminal"
        if preflight.open_fda_settings():
            ctl.notice = ("Ajustes abertos — ative '{}' em Acesso Total ao "
                          "Disco, depois FECHE e reabra o terminal.".format(host))
        else:
            ctl.notice = ("Não consegui abrir os Ajustes; vá em Ajustes > "
                          "Privacidade e Segurança > Acesso Total ao Disco "
                          "e ative '{}'.".format(host))
    elif _is_enter(ch):
        if flow.analyzing():
            ctl.notice = "Aguarde a análise (discovery e processos) terminar…"
        elif flow.any_running():
            flow.force_prompt = True
        else:
            ctl.start_backup()
    elif ch in (ord("q"), 27):
        state.back()


def _key_execute(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.backup
    if _is_up(ch):
        flow.log_scroll = max(0, flow.log_view_offset - 1)
    elif _is_down(ch):
        flow.log_scroll = flow.log_view_offset + 1
    elif ch == curses.KEY_PPAGE:
        flow.log_scroll = max(0, flow.log_view_offset - 10)
    elif ch == curses.KEY_NPAGE:
        flow.log_scroll = flow.log_view_offset + 10
    elif ch == ord("G"):
        flow.log_scroll = None
    elif flow.done and (ch in (ord("q"), 27) or _is_enter(ch)):
        ctl.backup = None
        state.home()
    elif ch in (ord("q"), 27):
        ctl.notice = ("Backup em andamento — não é possível abortar com "
                      "segurança; aguarde o término.")


def _key_backups(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.restore
    sel = flow.selection
    if sel is None:
        # Lista ainda carregando (worker de fundo).
        if ch in (ord("q"), 27):
            ctl.restore = None
            state.home()
        return
    if flow.plan_loading:
        if ch in (ord("q"), 27):
            ctl.restore = None
            state.home()
        else:
            ctl.notice = "Planejando o restore — aguarde."
        return
    if _is_up(ch):
        sel.move(-1)
    elif _is_down(ch):
        sel.move(1)
    elif ch == curses.KEY_PPAGE:
        sel.move(-10)
    elif ch == curses.KEY_NPAGE:
        sel.move(10)
    elif _is_enter(ch):
        row = sel.current_row()
        if row is None:
            ctl.notice = "Nenhum backup para selecionar."
        elif not row.get("valid"):
            ctl.notice = "Backup inválido (sem manifest válido): {}".format(
                row.get("error", "?"))
        elif flow.mode == "verify":
            ctl.start_verify(row)
        else:
            ctl.open_plan(row)
    elif ch in (ord("q"), 27):
        ctl.restore = None
        state.home()


def _key_plan(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.restore
    if flow.checking:
        # Checagem de processos em andamento (thread de fundo) — aguarde.
        ctl.notice = "Checando processos do app — aguarde."
        return
    if flow.confirm_stage == 1:
        if ch in (ord("y"), ord("Y"), ord("s"), ord("S")):
            # Re-checagem obrigatória de app em execução antes do apply
            # (em thread de fundo; o resultado decide estágio 2/3/apply).
            flow.confirm_stage = 0
            ctl.request_apply_check()
        else:
            flow.confirm_stage = 0
            ctl.notice = "Restore cancelado (default N)."
        return
    if flow.confirm_stage == 2:
        if ch in (ord("y"), ord("Y"), ord("s"), ord("S")):
            if flow.skew:
                # Divergência de versão também exige o "y" próprio dela.
                flow.confirm_stage = 3
            else:
                ctl.start_apply()
        else:
            flow.confirm_stage = 0
            ctl.notice = "Restore cancelado — feche o app e tente de novo."
        return
    if flow.confirm_stage == 3:
        if ch in (ord("y"), ord("Y"), ord("s"), ord("S")):
            ctl.start_apply()
        else:
            flow.confirm_stage = 0
            ctl.notice = ("Restore cancelado (divergência de versão — a CLI "
                          "equivalente exigiria --force).")
        return
    if _is_up(ch):
        flow.scroll = max(0, flow.scroll - 1)
    elif _is_down(ch):
        flow.scroll += 1
    elif ch == curses.KEY_PPAGE:
        flow.scroll = max(0, flow.scroll - 10)
    elif ch == curses.KEY_NPAGE:
        flow.scroll += 10
    elif ch == ord("r"):
        ctl.refresh_plan_running()
    elif _is_enter(ch):
        flow.confirm_stage = 1
    elif ch in (ord("q"), 27):
        state.back()


def _key_result(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    flow = ctl.restore
    if flow.applying and flow.result_lines is None:
        ctl.notice = "Aplicando restore — aguarde."
        return
    if _is_up(ch):
        flow.result_scroll = max(0, flow.result_scroll - 1)
    elif _is_down(ch):
        flow.result_scroll += 1
    elif ch in (ord("q"), 27) or _is_enter(ch):
        ctl.restore = None
        state.home()


def _key_output(ctl: _Controller, ch: int) -> None:
    state = ctl.state
    screen = ctl.output
    if _is_up(ch):
        screen.scroll = max(0, screen.scroll - 1)
    elif _is_down(ch):
        screen.scroll += 1
    elif ch == curses.KEY_PPAGE:
        screen.scroll = max(0, screen.scroll - 10)
    elif ch == curses.KEY_NPAGE:
        screen.scroll += 10
    elif ch in (ord("q"), 27) or _is_enter(ch):
        ctl.output = None
        state.home()
