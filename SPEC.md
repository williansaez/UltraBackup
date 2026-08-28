# UltraBackup — Spec v1 (validada por 4 agentes especialistas, veredito: viable_with_changes)

## Objetivo

CLI macOS que faz backup file-level COMPLETO de apps selecionados e restaura tudo
para os locais originais com permissões corretas — com lista explícita do que o
macOS proíbe (Keychain, TCC, BTM, MAS receipts). Cenário primário: restore na
mesma máquina. Cross-machine: modo degradado documentado.

Exemplo alvo: Claude Desktop + Claude Code CLI — `/Applications/Claude.app`,
`~/Library/Application Support/Claude`, `~/Library/Preferences/com.anthropic.*`,
`~/.claude/`, `~/.claude.json`, containers, launch agents.

## Invariantes NÃO negociáveis (dos validadores)

1. **Nunca seguir symlinks** em scan/size/hash/copy/restore: `os.walk(followlinks=False)`,
   `os.lstat` sempre. `~/Library/Containers/<id>/Data` contém symlinks para
   ~/Documents, ~/Desktop etc — segui-los copia o home inteiro e o restore
   sobrescreve os diretórios reais. Teste automatizado obrigatório.
2. **App não pode estar rodando** durante backup NEM restore (SQLite WAL,
   LevelDB de apps Electron corrompem). `pgrep -f` no caminho do bundle; recusar,
   oferecer instrução para fechar. `--force` imprime aviso de corrupção.
3. **Cópia de payload só com `ditto`** (xattrs, ACLs, resource forks, symlinks,
   assinatura). `shutil.copytree`/`cp` proibidos para payload.
4. **`ditto` faz merge em diretório existente** — garantir que o alvo NÃO existe
   antes de cada ditto no restore (move-aside primeiro, sempre).
5. **Restore é dry-run por default** — só muta com `--apply`.
6. **Preferences via `defaults import <domain> <plist>`** (e
   `defaults -currentHost import` para ByHost) com app fechado — nunca
   raw-copy + killall cfprefsd (race). Validar plist com `plutil -lint` antes.
7. **subprocess sempre com lista de argumentos**, nunca `shell=True`.
8. **manifest.json escrito por último, atomicamente** (temp + rename) = marcador
   de backup completo. Backup sem manifest = incompleto/inválido.
9. **Ownership simbólico** no manifest (`"user"` | `"root"`, pelo dono real:
   uid 0 → root), nunca uid/gid literal. Backup lê tudo que for legível,
   independente do dono. Restore: itens do home → usuário atual, sem chown;
   itens root-owned → só com sudo; itens user-owned fora do home (ex.
   `/Applications/App.app` instalado pelo usuário) → restaurados se o diretório
   pai for gravável, senão skip com aviso.
10. **Backup dir chmod 700** + aviso de segredos (`~/.claude.json` contém OAuth
    token; cookies/HTTPStorages inclusos).

## Comandos e exit codes

```
ultrabackup list                                # apps em /Applications
ultrabackup inspect <app> [--include-caches]    # o que seria capturado + relatório não-capturável
ultrabackup backup <app> [--dest D] [--include-caches] [--exclude PAT] [--force]
ultrabackup backups [--dest D]                  # backups existentes
ultrabackup restore <backup-dir> [--apply] [--only CAT] [--exclude CAT]
                    [--overwrite-newer] [--strip-quarantine] [--force]
ultrabackup verify <backup-dir>                 # checksums manifest vs payload
ultrabackup rollback <backup-dir>               # desfaz último restore via journal
ultrabackup doctor [<app>] [--dest D]           # preflights
```

Exit codes: 0 ok, 1 erro, 2 uso, 3 backup parcial, 4 verify mismatch,
5 rollback executado, 6 rollback incompleto, 7 confirmação necessária em non-TTY.
Nunca prompt em non-TTY.

Default `--dest`: `~/UltraBackups`.

## Formato do backup

```
<Dest>/<AppName>_<YYYY-MM-DDTHH-MM-SS>/
  manifest.json          # escrito por ÚLTIMO, atômico
  payload/<item-id>/<basename>   # árvores ditto
  restore-journal.json   # criado pelo restore --apply
```

### manifest.json (schema_version 1)

```json
{
  "schema_version": 1,
  "created_at": "ISO8601",
  "tool_version": "1.0.0",
  "source": {"username": "...", "hostname": "...", "hardware_uuid": "...",
             "macos_version": "...", "home": "/Users/x"},
  "app": {"name": "Claude", "bundle_id": "com.anthropic.claudefordesktop",
          "version": "...", "path": "/Applications/Claude.app",
          "mas_receipt": false,
          "helpers": [{"bundle_id": "...", "path": "..."}]},
  "completeness": "COMPLETE" | "PARTIAL",
  "not_capturable": ["Keychain items (app pedirá login)", "TCC/privacidade", "..."],
  "items": [
    {
      "id": "0001",
      "category": "app_bundle",
      "original_path": "~/Library/Application Support/Claude",
      "type": "dir" | "file" | "symlink",
      "ownership": "user" | "root",
      "mode": "0755",
      "size_bytes": 123,
      "provenance": "template" | "extras" | "entitlements" | "helper",
      "status": "copied" | "permission_denied" | "missing" | "skipped",
      "files": [{"relpath": ".", "type": "file", "size": 1, "sha256": "..."},
                {"relpath": "a/b", "type": "symlink", "target": "../x"}]
    }
  ]
}
```

- `original_path`: home-relativo com prefixo `~/` quando dentro do home; absoluto
  caso contrário. Expandido contra o home ATUAL no restore.
- `files`: para item tipo `file`, uma entrada com relpath "."; symlinks gravam
  `target`, sem hash.

## Categorias de discovery

| Categoria | Caminho(s) | Provenance |
|---|---|---|
| `app_bundle` | `/Applications/<Name>.app` | template |
| `app_support` | `~/Library/Application Support/{<Name>,<bundle-id>}` | template |
| `preferences` | `~/Library/Preferences/<bid>*.plist` + `ByHost/<bid>.*.plist` (cada helper bid também) | template/helper |
| `containers` | `~/Library/Containers/<bid>` (+ helpers) | template/helper |
| `group_containers` | ids de `com.apple.security.application-groups` via `codesign -d --entitlements - --xml <app>` parseado com `plutil -convert json` (app + helpers) | entitlements |
| `saved_state` | `~/Library/Saved Application State/<bid>.savedState` | template |
| `http_storages` | `~/Library/HTTPStorages/<bid>` (+ helpers) | template/helper |
| `webkit` | `~/Library/WebKit/<bid>` | template |
| `cookies` | `~/Library/Cookies/<bid>.binarycookies` | template |
| `launch_agents` | `~/Library/LaunchAgents/<bid>*.plist`; `/Library/LaunchAgents/<bid>*.plist`; `/Library/LaunchDaemons/<bid>*.plist` | template |
| `system_support` | `/Library/Application Support/<Name>` | template |
| `logs` | `~/Library/Logs/{<Name>,<bid>}` | template |
| `app_scripts` | `~/Library/Application Scripts/<bid>` | template |
| `caches` | `~/Library/Caches/{<Name>,<bid>}` — só com `--include-caches` | template |
| `dotfiles` | mapa curado `known_apps.json` | extras |

Helpers: enumerar `Contents/Frameworks/*.app`, `Contents/XPCServices/*.xpc`,
`Contents/Library/LoginItems/*.app` dentro do bundle; ler bundle id de cada
`Info.plist` (Electron: helpers têm ids próprios com Preferences/HTTPStorages próprios).

`known_apps.json` (na raiz do pacote):
```json
{
  "claude": {
    "match_names": ["Claude"],
    "match_bundle_ids": ["com.anthropic.claudefordesktop", "com.anthropic.claude"],
    "extras": ["~/.claude", "~/.claude.json", "~/.claude.json.backup"],
    "notes": "Claude Code CLI vive em ~/.claude; token OAuth em ~/.claude.json"
  }
}
```

`mdfind` só para localizar `.app` por `kMDItemCFBundleIdentifier` (fallback se
não estiver em /Applications); NUNCA para enumerar ~/Library; output vazio de
mdfind ≠ "não existe".

Sem fuzzy scan na v1 (validadores: risco alto de falso positivo; fica para v2
como opt-in interativo).

## Módulos e APIs (contrato para os agentes)

Pacote `ultrabackup/` (Python 3.9+, stdlib apenas). Todas as funções que tocam o
home aceitam `home: Path = None` (default `Path.home()`) para testabilidade.

### `fsutil.py`
```python
run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess
ditto_copy(src: Path, dst: Path) -> None          # cria pais; ERRO se dst existe
lstat_walk(root: Path) -> Iterator[tuple[Path, os.stat_result]]   # followlinks=False, lstat
tree_size(root: Path) -> int                       # via lstat_walk
hash_tree(root: Path) -> list[dict]                # entradas "files" do manifest; sha256 streaming; symlinks: target, sem hash
sha256_file(path: Path) -> str
to_portable(path: Path, home: Path) -> str         # dentro do home -> "~/rel"; senão absoluto
from_portable(portable: str, home: Path) -> Path
atomic_write_json(path: Path, obj) -> None         # temp no mesmo dir + os.replace
free_space(path: Path) -> int                      # os.statvfs
dest_fidelity_check(dest_dir: Path) -> list[str]   # cria arq temp, xattr write+readback (cmd xattr), symlink create+readback; retorna problemas
hardware_uuid() -> str                             # ioreg -rd1 -c IOPlatformExpertDevice, parse IOPlatformUUID
macos_version() -> str                             # sw_vers -productVersion
```

### `discovery.py`
```python
class AppInfo:  # dataclass: name, path (Path|None), bundle_id, version, helpers: list[dict], mas_receipt: bool
list_installed(applications_dir: Path = Path("/Applications")) -> list[AppInfo]
find_app(query: str, home: Path = None) -> AppInfo   # nome, caminho .app, ou chave known_apps; plutil p/ Info.plist; erro claro se não achar
find_helpers(app_path: Path) -> list[dict]
group_container_ids(app_path: Path) -> list[str]     # codesign entitlements, app + helpers; falha silenciosa -> []
discover(app: AppInfo, home: Path = None, include_caches: bool = False) -> list[dict]
# retorna items SEM copiar: id sequencial, category, path (absoluto), type,
# ownership ("root" se fora do home), provenance, status "found"|"missing"|"permission_denied"
# (permission_denied detectado via os.access/lstat com PermissionError)
```

### `preflight.py`
```python
running_processes(app: AppInfo, home: Path = None) -> list[str]  # pgrep -f no bundle path + extras dirs; retorna descrições
fda_probe(home: Path = None) -> str   # "ok" | "denied" | "unknown"; canary: ler ~/Library/Containers de app Apple conhecido (ex. com.apple.Safari) via os.listdir, EPERM -> denied
doctor(app: AppInfo | None, dest: Path, home: Path = None, need_bytes: int = 0) -> dict
# {"ok": bool, "problems": [...], "warnings": [...]}
# checa: app rodando, FDA, espaço livre, dest gravável, fidelity check, euid p/ itens root, dest dentro de iCloud/Dropbox (warning)
```

### `backup.py`
```python
do_backup(app: AppInfo, items: list[dict], dest_root: Path, home: Path = None) -> dict
# cria <dest>/<Name>_<ts>/ (chmod 700), payload/<id>/<basename> via ditto item a item,
# status por item (copied/permission_denied/missing), hash_tree após copiar,
# completeness = PARTIAL se qualquer permission_denied, manifest atômico POR ÚLTIMO.
# Itens ownership=root sem euid 0: pular com status permission_denied + warning (não sudo automático).
# retorna {"backup_dir": Path, "manifest": dict, "partial": bool}
```

### `restore.py`
```python
load_backup(backup_dir: Path) -> dict                 # valida schema_version, manifest presente
plan_restore(manifest: dict, backup_dir: Path, home: Path = None,
             only: list[str] = None, exclude: list[str] = None) -> list[dict]
# ações: [{item, target: Path, action: "restore"|"skip", reason, conflict: bool,
#          live_newer: bool, special: None|"preferences"|"container"|"launch_agent"}]
# live_newer: mtime do alvo vivo > created_at do backup
apply_restore(plan, backup_dir, home=None, overwrite_newer=False,
              strip_quarantine=False) -> dict
# 1. journal (atomic) ANTES de cada mutação: {moved_aside: [...], restored: [...]}
# 2. move-aside: alvo existente -> <mesmo volume>/.ultrabackup-prerestore/<ts>/<id>/ (os.rename; se EXDEV, ditto+verify+delete origem só após sucesso)
# 3. garantir alvo ausente; ditto payload -> alvo; chmod do manifest; itens home: sem chown (usuário atual)
# 4. preferences: plutil -lint, depois defaults import <domain> <plist>; ByHost: renomear p/ UUID atual + defaults -currentHost import
# 5. containers: restaurar SÓ conteúdo de Data/, excluindo .com.apple.containermanagerd.metadata.plist e symlinks de redirect no topo de Data/
# 6. launch agents (user): launchctl bootout gui/$UID/<label> (ignora erro) antes, bootstrap depois; itens /Library sem root: skip + warning
# 7. app bundle: com.apple.quarantine detectado -> só remove se strip_quarantine; depois codesign --verify --deep --strict (warning se falha) + lsregister -f
# 8. qualquer exceção: rollback automático via journal; exit 5/6
# 9. live_newer sem overwrite_newer: item vira skip com reason
rollback(backup_dir: Path, home: Path = None) -> dict  # lê journal, desfaz
verify(backup_dir: Path) -> dict                       # re-hash payload vs manifest; {"ok": bool, "mismatches": [...]}
version_skew_check(manifest, home=None) -> list[str]   # app instalado vs manifest; warnings, bloqueio sem --force
```

### `report.py`
```python
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PARTIAL, EXIT_VERIFY_MISMATCH,
EXIT_ROLLED_BACK, EXIT_ROLLBACK_INCOMPLETE, EXIT_NEEDS_CONFIRMATION = 0,1,2,3,4,5,6,7
NOT_CAPTURABLE: list[str]   # textos das limitações (Keychain, TCC, BTM, MAS, iCloud, safeStorage)
print_capability_report(manifest ou app) -> None   # "capturado" vs "NÃO capturável"
print_plan(plan) -> None    # tabela: ação, categoria, alvo, conflito, live_newer
```

### `cli.py` + `__main__.py`
argparse com subcomandos acima; `python3 -m ultrabackup` e wrapper `./ultrabackup`
(shebang `#!/usr/bin/env python3`, exec do pacote). `--json` nos comandos de
leitura. Confirmações: `input()` só se `sys.stdin.isatty()`, senão exit 7.
`restore` sem `--apply` = imprime plano e relatório, exit 0.

## Testes (`tests/test_roundtrip.py`, unittest, sem deps)

Fixture: home falso em tempdir com app falso (bundle Info.plist mínimo,
Application Support com subárvore, Preferences plist, container com
`Data/Documents` symlink → dir real fora do container, `~/.fake` dotfile).

Casos obrigatórios:
1. Round-trip: backup → apagar originais → restore --apply → conteúdo idêntico
   (hashes), permissões preservadas.
2. **Symlink invariant**: container restaurado mantém `Data/Documents` como
   symlink; dir real apontado NUNCA entra no payload.
3. Restore sem `--apply` não muta nada.
4. Conflito: alvo existente vai para move-aside; rollback restaura estado anterior.
5. Manifest atômico: payload sem manifest → load_backup falha claramente.
6. verify detecta payload adulterado.
7. live_newer sem --overwrite-newer → skip.

(Testes rodam com `home` injetado; chamadas `defaults`/`launchctl`/`codesign`
devem ser puláveis via flag interna `system_calls_enabled` ou detecção de que o
domínio não é real — no plano de teste, itens preferences usam raw copy fallback
quando `defaults import` falha, com warning.)

## Limitações documentadas no README (imprimir também no fim de cada run)

Keychain/safeStorage (relogin necessário; cookies Chromium de outra máquina
indecifráveis), TCC, BTM (re-aprovar em Ajustes), MAS receipt (reinstalar da
App Store cross-machine), SIP, hardlinks/sparse não preservados por ditto,
iCloud warn-and-skip, root necessário p/ /Library, app deve estar fechado.

## TUI — interface terminal-style (v1.1)

Referência visual: portfolio-terminal (fundo escuro, prompt colorido
`visitor@ultrabackup:~$`, banner ASCII, destaques em verde/ciano/laranja).

### Entrada

- `python3 -m ultrabackup` SEM argumentos em TTY → abre a TUI.
- `python3 -m ultrabackup tui` → idem.
- Sem args em non-TTY → help + exit 2.
- `UltraBackup.app` (bundle mínimo em `app/UltraBackup.app`): script launcher
  que abre o Terminal rodando `python3 -m ultrabackup tui` via osascript
  (curses precisa de TTY). Instalável em /Applications.

### Implementação

`ultrabackup/tui.py` — curses (stdlib). Arquitetura model/view: estado e
transições em funções/classes puras SEM curses (testáveis por unittest);
camada curses só desenha e traduz teclas. Cores via curses.init_pair com
`use_default_colors()` (fundo do terminal): verde (prompt/ok), ciano (host/
info), amarelo/laranja (seleção/avisos), vermelho (erros), invertido na linha
do cursor. Redimensionamento tratado (KEY_RESIZE); telas com scroll.

### Telas

1. **Home**: banner ASCII "ULTRABACKUP" + subtítulo + linha de prompt fake
   `visitor@ultrabackup:~$ _`. Menu: Backup de apps / Restaurar backup /
   Verificar backup / Doctor / Sair. Rodapé fixo com atalhos
   `↑↓/jk navegar · espaço marcar · enter confirmar · / filtrar · q voltar/sair`.
2. **Seleção de apps** (multi-select): lista de `discovery.list_installed()` +
   entradas CLI-only do known_apps (ex. Claude Code sem .app). Cada linha:
   `[ ] Nome  versão  bundle-id` → `[x]` marcado; espaço alterna; `/` abre
   filtro incremental (esc limpa); `a` marca/desmarca todos os visíveis;
   enter avança com ≥1 marcado.
3. **Confirmação**: por app marcado, resumo do discovery (itens found, contagem
   por categoria) + tamanho estimado calculado em thread de fundo
   (placeholder `…` até terminar; `threading`, nunca travar a UI) + avisos do
   preflight: app RODANDO em destaque vermelho (oferecer `r` para re-checar
   após fechar; backup de app rodando exige confirmação extra explícita),
   FDA denied, destino. Mostra destino (`~/UltraBackups`, editável com `d`).
   Enter inicia; esc volta.
4. **Execução**: log estilo terminal, uma linha por item
   `[0001] app_bundle … copied|missing|permission_denied`, via callback de
   progresso (novo parâmetro opcional `progress: Callable[[dict], None]` em
   `do_backup`, retrocompatível). Ao fim de cada app: COMPLETE/PARTIAL +
   caminho do backup + capability report resumido. `q` fecha ao terminar
   (nunca aborta cópia no meio sem confirmação).
5. **Restore**: lista de backups do destino (single-select) → tabela do plano
   (scroll) → `--apply` só após confirmação explícita `y/N` (default N),
   repetindo o aviso de app rodando; resultado + aviso de `rollback`
   disponível. Verify e Doctor: telas simples de saída rolável.

### Testes

`tests/test_tui_model.py`: filtro, toggle, marcar-todos, transições de tela,
formatação de linhas de checkbox — sem curses. Round-trip TUI real via pty
fica com o revisor funcional (script pty dirigindo teclas), não no suite.

## App nativa (v2.0) — FDA na própria app

Objetivo: `UltraBackup.app` 100% nativa (Swift/AppKit) com terminal EMBUTIDO.
O motor Python roda como processo FILHO da app ⇒ o macOS atribui TCC ao
UltraBackup.app: FDA concedido só à app, Terminal fica sem acesso.

### Arquitetura

```
native/
  Package.swift            # SwiftPM, macOS 13+, dep SwiftTerm (~>1.20.0)
  Sources/UltraBackupApp/
    main.swift             # NSApplication sem storyboard
    AppDelegate.swift      # janela, menu (Quit/Copy/Paste), ciclo de vida
    TerminalHost.swift     # SwiftTerm LocalProcessTerminalView
  build_app.sh             # build release + monta app/UltraBackup.app
```

- TerminalHost roda `/usr/bin/python3 -m ultrabackup tui` — caminho ABSOLUTO,
  nunca `/usr/bin/env python3`: o filho herda o FDA da app, então o
  interpretador não pode ser escolhido por um `PATH` herdado (Homebrew, pyenv,
  conda, diretório gravável pelo usuário). O env do filho é montado por
  ALLOW-LIST (`HOME`, `USER`, `LOGNAME`, `TMPDIR`, `__CF_USER_TEXT_ENCODING`)
  mais `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `TERM=xterm-256color`,
  `COLORTERM`, `LANG=en_US.UTF-8`, `LC_CTYPE`,
  `ULTRABACKUP_HOST_APP=UltraBackup`, `PYTHONPATH=<Contents/Resources>`,
  `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1` (nada de `__pycache__`
  dentro do bundle assinado) e cwd `Resources`. `PYTHONHOME`, `VIRTUAL_ENV`,
  `CONDA_PREFIX`, `PYTHONSTARTUP`, `DYLD_*` etc. não chegam ao filho. O
  `PYTHONPATH` é resolvido explicitamente (`<root>/ultrabackup/__main__.py`
  precisa existir); se faltar, a app pinta o erro no terminal em vez de lançar
  um filho condenado. Resize da janela propaga winsize ao pty (SwiftTerm
  cuida). Filho termina COM SUCESSO ⇒ app encerra; filho termina com erro ⇒
  janela FICA aberta com o status decodificado (o valor entregue por SwiftTerm
  é o wait status cru, não o exit code). App encerra ⇒ filho recebe
  SIGINT ⇒ SIGHUP/SIGTERM ⇒ SIGKILL, escalonado de forma ASSÍNCRONA via
  `applicationShouldTerminate` + `.terminateLater` (bloquear a main queue
  impediria o próprio fechamento do pty) — sem python órfão. Cmd+Q com o motor
  vivo abre uma sheet de confirmação; SIGTERM/logout ignoram a sheet.
- Janela: tema escuro (#1e2430), fonte SF Mono/Menlo 13 (fallback), título
  "UltraBackup", tamanho inicial ≈120x34 células, redimensionável.
- **Self-contained**: `build_app.sh` copia o pacote `ultrabackup/` (com
  known_apps.json) para `Contents/Resources/ultrabackup/` — a app funciona
  copiada para /Applications, sem depender do caminho do repo. Requisito
  externo único: python3 do sistema (CLT).
- Bundle: Info.plist (bundle id `dev.ultrabackup.app`, CFBundleIconFile
  AppIcon, NSHighResolutionCapable, NSSupportsSecureRestorableState),
  Resources/AppIcon.icns preservado, `codesign --force --deep -s -` (ad-hoc).
  Nota: rebuild muda o cdhash **e** o TCC também é amarrado ao caminho ⇒
  re-conceder FDA após rebuild E após mover/copiar o bundle (build_app.sh
  imprime o aviso). Instalar com `ditto --noextattr --norsrc`, nunca `cp -R`.
- `preflight.terminal_host_app()`: honra `ULTRABACKUP_HOST_APP` antes de
  TERM_PROGRAM ⇒ mensagens dizem "ATIVE 'UltraBackup'".
- Launcher antigo via osascript→Terminal: eliminado (era o problema).

### Verificação obrigatória

build_app.sh sai 0 (o próprio script já roda `codesign --verify --deep` no
bundle e um passe `--deep --strict` sobre uma cópia `ditto --noextattr
--norsrc`; `codesign -dv` sozinho só EXIBE a assinatura, não verifica nada);
`codesign --verify app/UltraBackup.app` ok; `open app/UltraBackup.app` abre
janela com a TUI; `pgrep -f "ultrabackup tui"` mostra o filho; quit não deixa
python órfão; suite Python continua verde.

Nota: `--strict` no caminho real do repositório pode falhar por
`com.apple.FinderInfo` que o Finder/iCloud reanexa ao diretório — é uma
propriedade da pasta, não do empacotamento; por isso o passe estrito roda na
cópia limpa.
