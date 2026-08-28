"""Interface de linha de comando do UltraBackup.

Subcomandos (ver SPEC.md): list, inspect, backup, backups, restore, verify,
rollback, doctor. Este módulo apenas orquestra os contratos de
``discovery``/``preflight``/``backup``/``restore``/``report``; nenhuma cópia de
payload acontece aqui.

Regras de confirmação: prompts via ``input()`` somente quando
``sys.stdin.isatty()``; em non-TTY a operação sai com
``EXIT_NEEDS_CONFIRMATION`` (7), a menos que ``--yes`` tenha sido passado.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat as stat_module
import sys
from pathlib import Path

from . import __version__
from . import backup as backup_module
from . import discovery
from . import fsutil
from . import preflight
from . import report
from . import restore as restore_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_home(home: Path = None) -> Path:
    """Resolve o home efetivo (injeção para testes, senão ``Path.home()``)."""
    return home if home is not None else Path.home()


def _resolve_dest(dest_arg, home: Path = None) -> Path:
    """Resolve ``--dest``; default ``<home>/UltraBackups``."""
    if dest_arg:
        return Path(dest_arg).expanduser()
    return _resolve_home(home) / "UltraBackups"


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Confirmação interativa.

    ``--yes`` pula o prompt; sem TTY em stdin nunca pergunta — encerra com
    exit code 7 (EXIT_NEEDS_CONFIRMATION), conforme a spec.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Confirmação necessária, mas stdin não é um TTY. "
            "Use --yes para confirmar de forma não interativa.",
            file=sys.stderr,
        )
        raise SystemExit(report.EXIT_NEEDS_CONFIRMATION)
    answer = input("{} [s/N] ".format(prompt)).strip().lower()
    return answer in {"s", "sim", "y", "yes"}


def _human_size(num_bytes: int) -> str:
    """Formata bytes de forma legível (base 1024)."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            if unit == "B":
                return "{:d} B".format(int(value))
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{:d} B".format(int(num_bytes))


def _estimate_item_size(item: dict) -> int:
    """Tamanho estimado de um item de discovery, sem seguir symlinks."""
    path = item.get("path")
    if path is None or item.get("status") != "found":
        return 0
    path = Path(path)
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if stat_module.S_ISDIR(st.st_mode):
        try:
            return fsutil.tree_size(path)
        except Exception:
            return 0
    return st.st_size


def _matches_exclude(item: dict, patterns) -> bool:
    """True se o item casa com algum ``--exclude PAT``.

    Um padrão casa por categoria exata, pelo caminho completo (fnmatch) ou
    pelo basename do caminho (fnmatch).
    """
    path_str = str(item.get("path", ""))
    basename = os.path.basename(path_str.rstrip("/"))
    category = item.get("category", "")
    for pattern in patterns:
        if pattern == category:
            return True
        if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _app_to_dict(app) -> dict:
    """Serializa um AppInfo para JSON."""
    return {
        "name": app.name,
        "path": str(app.path) if app.path else None,
        "bundle_id": app.bundle_id,
        "version": app.version,
        "helpers": app.helpers,
        "mas_receipt": app.mas_receipt,
    }


def _item_to_jsonable(item: dict) -> dict:
    """Copia um item de discovery convertendo Paths em strings."""
    out = dict(item)
    if "path" in out and out["path"] is not None:
        out["path"] = str(out["path"])
    return out


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _appinfo_from_manifest(manifest: dict):
    """Reconstrói um AppInfo a partir do bloco ``app`` do manifest."""
    app = manifest.get("app", {}) or {}
    path = app.get("path")
    return discovery.AppInfo(
        name=app.get("name", ""),
        path=Path(path) if path else None,
        bundle_id=app.get("bundle_id", ""),
        version=app.get("version"),
        helpers=app.get("helpers", []) or [],
        mas_receipt=bool(app.get("mas_receipt", False)),
    )


def _print_secrets_warning() -> None:
    print()
    print(
        "AVISO DE SEGREDOS: o backup pode conter tokens OAuth (ex.: ~/.claude.json),"
        " cookies e HTTPStorages."
    )
    print(
        "O diretório foi criado com chmod 700 — trate-o como material sensível e"
        " não o sincronize para locais não confiáveis."
    )


def _print_running_processes(procs) -> None:
    print("O app parece estar em execução:", file=sys.stderr)
    for proc in procs:
        print("  - {}".format(proc), file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------

def cmd_list(args, home: Path = None) -> int:
    """``ultrabackup list`` — apps instalados em /Applications."""
    apps = discovery.list_installed()
    if args.json:
        _print_json([_app_to_dict(a) for a in apps])
        return report.EXIT_OK
    if not apps:
        print("Nenhum app encontrado em /Applications.")
        return report.EXIT_OK
    for app in apps:
        version = app.version or "?"
        bundle_id = app.bundle_id or "?"
        print("{:<32} {:<16} {}".format(app.name, version, bundle_id))
    return report.EXIT_OK


def cmd_inspect(args, home: Path = None) -> int:
    """``ultrabackup inspect <app>`` — o que seria capturado, sem copiar."""
    app = discovery.find_app(args.app, home=home)
    items = discovery.discover(app, home=home, include_caches=args.include_caches)
    if args.json:
        _print_json(
            {
                "app": _app_to_dict(app),
                "items": [_item_to_jsonable(i) for i in items],
                "not_capturable": report.NOT_CAPTURABLE,
            }
        )
        return report.EXIT_OK
    _print_discovery_table(app, items)
    report.print_capability_report(app)
    return report.EXIT_OK


def _print_discovery_table(app, items) -> None:
    print("Itens capturáveis para {} ({}):".format(app.name, app.bundle_id or "?"))
    print()
    total = 0
    for item in items:
        size = _estimate_item_size(item)
        total += size
        print(
            "  [{}] {:<18} {:<18} {:>10}  {}".format(
                item.get("id", "?"),
                str(item.get("category", "?")),
                str(item.get("status", "?")),
                _human_size(size),
                item.get("path", "?"),
            )
        )
    print()
    print("Total estimado: {} em {} itens".format(_human_size(total), len(items)))


def cmd_backup(args, home: Path = None) -> int:
    """``ultrabackup backup <app>`` — fluxo completo de backup."""
    dest = _resolve_dest(args.dest, home)
    app = discovery.find_app(args.app, home=home)

    items = discovery.discover(app, home=home, include_caches=args.include_caches)
    if args.exclude:
        items = [i for i in items if not _matches_exclude(i, args.exclude)]
    if not items:
        print("Nada a copiar para {} após filtros.".format(app.name), file=sys.stderr)
        return report.EXIT_ERROR

    # Preflight: aborta em problemas, a menos que --force. O tamanho total
    # estimado alimenta a checagem de espaço livre do doctor (spec).
    need_bytes = sum(_estimate_item_size(item) for item in items)
    doc = preflight.doctor(app, dest, home=home, need_bytes=need_bytes)
    for warning in doc.get("warnings", []):
        print("aviso: {}".format(warning), file=sys.stderr)
    problems = doc.get("problems", [])
    if problems:
        for problem in problems:
            print("problema: {}".format(problem), file=sys.stderr)
        if not args.force:
            print(
                "Abortando por problemas de preflight. Corrija-os ou use --force"
                " (risco de corrupção se o app estiver rodando).",
                file=sys.stderr,
            )
            return report.EXIT_ERROR
        print(
            "aviso: --force ativo; prosseguindo apesar dos problemas acima."
            " Se o app estiver rodando, SQLite WAL/LevelDB podem corromper.",
            file=sys.stderr,
        )

    print("Plano de backup — destino: {}".format(dest))
    _print_discovery_table(app, items)
    report.print_capability_report(app)

    if not _confirm("Criar backup de {} em {}?".format(app.name, dest), args.yes):
        print("Operação cancelada pelo usuário.")
        return report.EXIT_ERROR

    result = backup_module.do_backup(app, items, dest, home=home)
    manifest = result["manifest"]

    counts = {}
    for item in manifest.get("items", []):
        status = item.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    print()
    print("Backup criado em: {}".format(result["backup_dir"]))
    print("Completude: {}".format(manifest.get("completeness", "?")))
    print(
        "Itens: {}".format(
            ", ".join("{}={}".format(k, v) for k, v in sorted(counts.items())) or "0"
        )
    )
    report.print_capability_report(manifest)
    _print_secrets_warning()
    return report.EXIT_PARTIAL if result.get("partial") else report.EXIT_OK


def cmd_backups(args, home: Path = None) -> int:
    """``ultrabackup backups`` — lista backups com manifest válido em --dest."""
    dest = _resolve_dest(args.dest, home)
    rows = []
    if dest.is_dir():
        for entry in sorted(dest.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            row = {"dir": str(entry), "valid": False}
            try:
                manifest = restore_module.load_backup(entry)
            except Exception as exc:
                # Só reporta diretórios que parecem backups (payload/manifest).
                if (entry / "payload").is_dir() or (entry / "manifest.json").is_file():
                    row["error"] = str(exc)
                    rows.append(row)
                continue
            items = manifest.get("items", [])
            row.update(
                valid=True,
                app=(manifest.get("app", {}) or {}).get("name", "?"),
                created_at=manifest.get("created_at", "?"),
                size_bytes=sum(int(i.get("size_bytes") or 0) for i in items),
                completeness=manifest.get("completeness", "?"),
            )
            rows.append(row)

    if args.json:
        _print_json(rows)
        return report.EXIT_OK

    if not rows:
        print("Nenhum backup encontrado em {}.".format(dest))
        return report.EXIT_OK
    for row in rows:
        if row["valid"]:
            print(
                "{:<20} {:<22} {:>10}  {:<8}  {}".format(
                    row["app"],
                    row["created_at"],
                    _human_size(row["size_bytes"]),
                    row["completeness"],
                    row["dir"],
                )
            )
        else:
            print(
                "{:<20} {:<22} {:>10}  {:<8}  {}  (sem manifest válido: {})".format(
                    "?", "?", "?", "INVÁLIDO", row["dir"], row.get("error", "?")
                )
            )
    return report.EXIT_OK


def cmd_restore(args, home: Path = None) -> int:
    """``ultrabackup restore <backup-dir>`` — dry-run por default; muta só com --apply."""
    backup_dir = Path(args.backup_dir).expanduser()
    manifest = restore_module.load_backup(backup_dir)

    # App ausente NÃO é divergência de versão: é o cenário primário de
    # restore pós-reinstalação — apenas informa que o bundle virá do backup.
    app_path = (manifest.get("app", {}) or {}).get("path")
    if app_path and not Path(app_path).exists():
        print(
            "aviso: o app não está instalado em {}; o bundle virá"
            " integralmente do backup.".format(app_path),
            file=sys.stderr,
        )

    # Version skew: sempre reportado; só bloqueia o --apply (o dry-run é
    # read-only — nada a proteger) e pode ser ignorado com --force.
    skew = restore_module.version_skew_check(manifest, home=home)
    for warning in skew:
        print("aviso de versão: {}".format(warning), file=sys.stderr)

    # App alvo não pode estar rodando (SQLite WAL/LevelDB corrompem).
    procs = preflight.running_processes(_appinfo_from_manifest(manifest), home=home)
    if procs:
        _print_running_processes(procs)
        if not args.force:
            print(
                "Feche o app (Cmd+Q; verifique também helpers na barra de menus)"
                " e tente de novo, ou use --force por sua conta e risco.",
                file=sys.stderr,
            )
            return report.EXIT_ERROR
        print(
            "aviso: --force ativo; restore com o app rodando pode corromper"
            " SQLite WAL/LevelDB.",
            file=sys.stderr,
        )

    plan = restore_module.plan_restore(
        manifest, backup_dir, home=home, only=args.only, exclude=args.exclude
    )
    report.print_plan(plan)
    report.print_capability_report(manifest)

    if not args.apply:
        print()
        print("Dry-run: nada foi alterado. Use --apply para executar o plano acima.")
        return report.EXIT_OK

    if skew and not args.force:
        print(
            "Divergência de versão/máquina entre backup e sistema atual —"
            " bloqueando restore --apply. Use --force para prosseguir mesmo"
            " assim.",
            file=sys.stderr,
        )
        return report.EXIT_ERROR

    if not _confirm(
        "Aplicar restore de {} a partir de {}?".format(
            (manifest.get("app", {}) or {}).get("name", "?"), backup_dir
        ),
        args.yes,
    ):
        print("Operação cancelada pelo usuário.")
        return report.EXIT_ERROR

    try:
        result = restore_module.apply_restore(
            plan,
            backup_dir,
            home=home,
            overwrite_newer=args.overwrite_newer,
            strip_quarantine=args.strip_quarantine,
        )
    except Exception as exc:
        # apply_restore faz rollback automático via journal em qualquer exceção;
        # se a exceção escapar, tenta ler o estado do rollback nos atributos.
        print("erro durante o restore: {}".format(exc), file=sys.stderr)
        rolled_back = getattr(exc, "rolled_back", None)
        rollback_ok = getattr(exc, "rollback_complete", getattr(exc, "rollback_ok", None))
        if rolled_back:
            if rollback_ok is False:
                print("Rollback automático INCOMPLETO — verifique o estado.", file=sys.stderr)
                return report.EXIT_ROLLBACK_INCOMPLETE
            print("Rollback automático executado; estado anterior restaurado.", file=sys.stderr)
            return report.EXIT_ROLLED_BACK
        print(
            "Estado do rollback desconhecido — use 'ultrabackup rollback {}'"
            " para desfazer mutações registradas no journal.".format(backup_dir),
            file=sys.stderr,
        )
        return report.EXIT_ERROR

    _print_apply_summary(result)
    print("Journal: {}".format(backup_dir / "restore-journal.json"))
    print("Para desfazer: ultrabackup rollback {}".format(backup_dir))
    return _restore_exit_code(result)


def _print_apply_summary(result) -> None:
    """Resumo tolerante ao formato exato do dict de apply_restore.

    Um restore que falhou (e foi revertido automaticamente) NUNCA é
    reportado como sucesso: a razão da falha (chave string ``error``) vai
    para stderr.
    """
    if not isinstance(result, dict):
        print("Restore aplicado.")
        return
    print()
    if result.get("rolled_back"):
        error = result.get("error")
        if result.get("rollback_complete") is False:
            headline = "Restore FALHOU; rollback automático INCOMPLETO"
        else:
            headline = "Restore FALHOU e foi revertido (rollback automático)"
        print(
            "{}{}".format(headline, ": {}".format(error) if error else "."),
            file=sys.stderr,
        )
    elif not result.get("ok", True):
        print(
            "Restore FALHOU: {}".format(result.get("error") or "erro desconhecido"),
            file=sys.stderr,
        )
    else:
        print("Restore aplicado.")
    for key in ("restored", "skipped", "moved_aside"):
        value = result.get(key)
        if isinstance(value, list):
            print("  {}: {}".format(key, len(value)))
    for key in ("warnings", "problems", "errors"):
        value = result.get(key)
        if isinstance(value, list):
            for line in value:
                print("  {}: {}".format(key[:-1], line), file=sys.stderr)


def _restore_exit_code(result) -> int:
    """Mapeia o resultado de apply_restore para 0/5/6."""
    if not isinstance(result, dict):
        return report.EXIT_OK
    if result.get("rolled_back"):
        # apply_restore reporta o resultado do rollback em "rollback_complete"
        # (None quando não houve rollback, bool quando houve).
        if result.get("rollback_complete", True) is False:
            return report.EXIT_ROLLBACK_INCOMPLETE
        return report.EXIT_ROLLED_BACK
    if result.get("ok", True):
        return report.EXIT_OK
    return report.EXIT_ERROR


def cmd_verify(args, home: Path = None) -> int:
    """``ultrabackup verify <backup-dir>`` — re-hash do payload vs manifest."""
    backup_dir = Path(args.backup_dir).expanduser()
    result = restore_module.verify(backup_dir)
    if args.json:
        _print_json(result)
    else:
        if result.get("ok"):
            print("OK: payload confere com o manifest.")
        else:
            print("FALHA: divergências entre payload e manifest:", file=sys.stderr)
            for mismatch in result.get("mismatches", []):
                print("  - {}".format(mismatch), file=sys.stderr)
    return report.EXIT_OK if result.get("ok") else report.EXIT_VERIFY_MISMATCH


def cmd_rollback(args, home: Path = None) -> int:
    """``ultrabackup rollback <backup-dir>`` — desfaz o último restore via journal."""
    backup_dir = Path(args.backup_dir).expanduser()
    result = restore_module.rollback(backup_dir, home=home)
    problems = []
    if isinstance(result, dict):
        for key in ("problems", "errors", "mismatches"):
            value = result.get(key)
            if isinstance(value, list):
                problems.extend(value)
        ok = result.get("ok", True) and not problems
    else:
        ok = True
    if ok:
        print("Rollback concluído; estado anterior ao restore foi restaurado.")
        return report.EXIT_OK
    print("Rollback INCOMPLETO:", file=sys.stderr)
    for problem in problems:
        print("  - {}".format(problem), file=sys.stderr)
    return report.EXIT_ROLLBACK_INCOMPLETE


def cmd_doctor(args, home: Path = None) -> int:
    """``ultrabackup doctor [<app>]`` — preflights (app rodando, FDA, espaço...)."""
    dest = _resolve_dest(args.dest, home)
    app = discovery.find_app(args.app, home=home) if args.app else None
    result = preflight.doctor(app, dest, home=home)
    if args.json:
        _print_json(result)
        return report.EXIT_OK if result.get("ok") else report.EXIT_ERROR
    for problem in result.get("problems", []):
        print("problema: {}".format(problem), file=sys.stderr)
    for warning in result.get("warnings", []):
        print("aviso: {}".format(warning))
    if result.get("ok"):
        print("doctor: tudo ok.")
        return report.EXIT_OK
    print("doctor: problemas encontrados (ver acima).", file=sys.stderr)
    return report.EXIT_ERROR


def cmd_tui(args, home: Path = None) -> int:
    """``ultrabackup tui`` — interface interativa em curses (exige TTY)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "erro: a TUI do UltraBackup precisa de um terminal interativo (TTY)."
            " Abra o Terminal e rode 'python3 -m ultrabackup tui', ou use os"
            " subcomandos da CLI (ex.: 'ultrabackup backup claude --yes').",
            file=sys.stderr,
        )
        return report.EXIT_USAGE
    # Import tardio: os subcomandos da CLI nunca dependem de curses.
    from . import tui
    return tui.run_tui(home=home, dest=_resolve_dest(args.dest, home))


# ---------------------------------------------------------------------------
# Parser e entrada
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Constrói o parser argparse com todos os subcomandos da spec."""
    parser = argparse.ArgumentParser(
        prog="ultrabackup",
        description=(
            "Backup file-level completo, por app, para macOS — com lista"
            " explícita do que o sistema proíbe capturar."
        ),
    )
    parser.add_argument(
        "--version", action="version", version="ultrabackup {}".format(__version__)
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<comando>", required=True)

    sp = subparsers.add_parser("list", help="lista apps em /Applications")
    sp.add_argument("--json", action="store_true", help="saída em JSON")
    sp.set_defaults(func=cmd_list)

    sp = subparsers.add_parser(
        "inspect", help="mostra o que seria capturado + relatório do não-capturável"
    )
    sp.add_argument("app", help="nome do app, caminho .app ou chave de known_apps")
    sp.add_argument(
        "--include-caches",
        dest="include_caches",
        action="store_true",
        help="inclui ~/Library/Caches",
    )
    sp.add_argument("--json", action="store_true", help="saída em JSON")
    sp.set_defaults(func=cmd_inspect)

    sp = subparsers.add_parser("backup", help="cria backup de um app")
    sp.add_argument("app", help="nome do app, caminho .app ou chave de known_apps")
    sp.add_argument("--dest", metavar="D", help="diretório destino (default ~/UltraBackups)")
    sp.add_argument(
        "--include-caches",
        dest="include_caches",
        action="store_true",
        help="inclui ~/Library/Caches",
    )
    sp.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PAT",
        help="exclui itens por categoria ou padrão de caminho (repetível)",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="prossegue apesar de problemas de preflight (risco de corrupção)",
    )
    sp.add_argument("--yes", action="store_true", help="pula a confirmação interativa")
    sp.set_defaults(func=cmd_backup)

    sp = subparsers.add_parser("backups", help="lista backups existentes")
    sp.add_argument("--dest", metavar="D", help="diretório destino (default ~/UltraBackups)")
    sp.add_argument("--json", action="store_true", help="saída em JSON")
    sp.set_defaults(func=cmd_backups)

    sp = subparsers.add_parser(
        "restore", help="restaura um backup (dry-run por default; muta só com --apply)"
    )
    sp.add_argument("backup_dir", metavar="backup-dir", help="diretório do backup")
    sp.add_argument("--apply", action="store_true", help="executa o plano (muta o sistema)")
    sp.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="CAT",
        help="restaura só estas categorias (repetível)",
    )
    sp.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="CAT",
        help="exclui estas categorias (repetível)",
    )
    sp.add_argument(
        "--overwrite-newer",
        dest="overwrite_newer",
        action="store_true",
        help="sobrescreve alvos vivos mais novos que o backup",
    )
    sp.add_argument(
        "--strip-quarantine",
        dest="strip_quarantine",
        action="store_true",
        help="remove com.apple.quarantine do app bundle restaurado",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="ignora bloqueios de version skew e de app em execução",
    )
    sp.add_argument("--yes", action="store_true", help="pula a confirmação interativa")
    sp.set_defaults(func=cmd_restore)

    sp = subparsers.add_parser("verify", help="confere checksums do manifest vs payload")
    sp.add_argument("backup_dir", metavar="backup-dir", help="diretório do backup")
    sp.add_argument("--json", action="store_true", help="saída em JSON")
    sp.set_defaults(func=cmd_verify)

    sp = subparsers.add_parser("rollback", help="desfaz o último restore via journal")
    sp.add_argument("backup_dir", metavar="backup-dir", help="diretório do backup")
    sp.set_defaults(func=cmd_rollback)

    sp = subparsers.add_parser("doctor", help="preflights (FDA, espaço, app rodando...)")
    sp.add_argument("app", nargs="?", help="nome do app, caminho .app ou chave de known_apps")
    sp.add_argument("--dest", metavar="D", help="diretório destino (default ~/UltraBackups)")
    sp.add_argument("--json", action="store_true", help="saída em JSON")
    sp.set_defaults(func=cmd_doctor)

    sp = subparsers.add_parser("tui", help="abre a interface de terminal (TUI)")
    sp.add_argument("--dest", metavar="D", help="diretório destino (default ~/UltraBackups)")
    sp.set_defaults(func=cmd_tui)

    return parser


def main(argv=None, home: Path = None) -> int:
    """Entrada principal. Retorna o exit code (não chama sys.exit em sucesso).

    ``home`` é injetável para testes; default ``Path.home()`` dentro de cada
    módulo, conforme o contrato da spec.

    Sem argumentos (spec TUI v1.1): em TTY abre a TUI; em non-TTY imprime a
    ajuda e sai com código 2 (uso).
    """
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            argv = ["tui"]
        else:
            build_parser().print_help()
            return report.EXIT_USAGE
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args, home)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrompido.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("erro: {}".format(exc), file=sys.stderr)
        return report.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
