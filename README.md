<a href="https://www.buymeacoffee.com/williansaez" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

![License](https://img.shields.io/badge/license-MIT-green)

# UltraBackup

Backup **file-level completo**, por app, para macOS — **com lista explícita do
que o macOS proíbe** capturar.

O UltraBackup copia tudo o que um app deixa no disco (bundle, Application
Support, Preferences, Containers, Group Containers, cookies, launch agents,
dotfiles conhecidos...) e restaura cada item no local original, com permissões
corretas. O que ele **não** promete é milagre: Keychain, permissões de
privacidade (TCC), aprovações de itens de login (BTM) e recibos da App Store
são protegidos pelo sistema e **não existem backup file-level que os
restaure**. Em vez de fingir que isso funciona, o UltraBackup imprime um
relatório do que não foi capturável ao fim de cada execução.

Cenário primário: **restore na mesma máquina** (reinstalação limpa, downgrade,
desfazer um update que quebrou tudo). Cross-machine funciona em modo degradado
documentado (ver [Limitações](#limitações)).

## Requisitos

- macOS (usa `ditto`, `defaults`, `plutil`, `codesign`, `launchctl`)
- Python 3.9+ (só stdlib — zero dependências)
- **Full Disk Access** — para o **UltraBackup.app** se você usa o app nativo,
  ou para o seu terminal se você usa a CLI (ver [App nativa](#app-nativa))

## Instalação

```bash
git clone https://github.com/<voce>/UltraBackup.git
cd UltraBackup
./ultrabackup-cli --version
```

> **Por que `ultrabackup-cli` e não `ultrabackup`?** O wrapper executável mora
> na raiz do repositório, ao lado do diretório do pacote Python `ultrabackup/`.
> Um arquivo chamado `ultrabackup` colidiria com esse diretório, então o
> wrapper se chama `ultrabackup-cli`. Ele resolve symlinks, então você pode
> instalar com o nome curto:

```bash
ln -s "$PWD/ultrabackup-cli" /usr/local/bin/ultrabackup
```

Alternativa sem wrapper, a partir da raiz do repositório:

```bash
python3 -m ultrabackup --help
```

## TUI — interface de terminal

Além dos subcomandos, o UltraBackup tem uma TUI curses estilo terminal
retrô (fundo do terminal, prompt `<seu-usuário>@ultrabackup:~$` (usa seu nome de usuário real), banner ASCII,
destaques em verde/ciano/laranja, avisos em vermelho):

```
 _   _  _     _____  ___    _    ___    _     ___  _  __ _   _  ___
| | | || |   |_   _|| _ \  /_\  | _ )  /_\   / __|| |/ /| | | || _ \
| |_| || |__   | |  |   / / _ \ | _ \ / _ \ | (__ | ' < | |_| ||  _/
 \___/ |____|  |_|  |_|_\/_/ \_\|___//_/ \_\ \___||_|\_\ \___/ |_|

              backup file-level por app para macOS — v1.0.0

  <seu-usuário>@ultrabackup:~$ menu _

    > Backup de apps
      Restaurar backup
      Verificar backup
      Doctor (preflights)
      Sair

 ↑↓/jk navegar · espaço marcar · enter confirmar · / filtrar · q voltar/sair
```

Seleção de apps (multi-select com filtro incremental):

```
  <seu-usuário>@ultrabackup:~$ backup --select
  filtro: cla_

  [x] Claude                        1.5.2         com.anthropic.claudefordesktop
  [ ] Claude Code                   cli-only      com.anthropic.claude
```

### Como abrir

```bash
open app/UltraBackup.app        # app nativo, terminal embutido (recomendado)
python3 -m ultrabackup          # sem argumentos, num terminal (TTY) → abre a TUI
python3 -m ultrabackup tui      # explícito; aceita --dest D
```

O app nativo é a forma recomendada: o Acesso Total ao Disco passa a ser dele,
e não do Terminal — ver [App nativa](#app-nativa).

Sem argumentos fora de um TTY (pipes, scripts), imprime a ajuda e sai com
código 2 — a TUI nunca roda sem terminal.

### Fluxos

- **Backup**: lista `discovery.list_installed()` + entradas CLI-only do
  `known_apps.json` (ex.: Claude Code sem `.app`); espaço marca, `a` marca
  todos os visíveis, `/` filtra, enter avança. A tela de confirmação mostra
  o resumo do discovery por app, tamanho estimado (calculado em thread de
  fundo, `…` enquanto calcula), avisos de preflight — app **em execução**
  aparece em vermelho, `r` re-checa, e prosseguir com app rodando exige uma
  confirmação extra explícita. Destino editável com `d` (default
  `~/UltraBackups`). A execução mostra uma linha de log por item
  (`[0001] app_bundle … copied`) e, ao fim de cada app, COMPLETE/PARTIAL +
  caminho + relatório de capacidade resumido.
- **Restore**: lista os backups do destino, mostra a tabela do plano
  (rolável) e só aplica após confirmação explícita `y/N` (default **N**),
  repetindo a re-checagem de app em execução. Em falha, o rollback
  automático via journal é reportado e `ultrabackup rollback <dir>` é
  sugerido.
- **Verify** e **Doctor**: telas simples de saída rolável.

A TUI chama exatamente o mesmo engine da CLI (`do_backup`/`apply_restore`)
e nunca muta o sistema fora dele.

## App nativa

`app/UltraBackup.app` é um app macOS nativo (Swift/AppKit) com um **terminal
embutido** (SwiftTerm). O motor Python roda como processo **filho** do app.

### Por que isso importa: o FDA passa a ser do UltraBackup

O TCC do macOS atribui permissões ao **app que hospeda o processo**, não ao
`python3`. Na v1 a TUI rodava dentro do Terminal.app, então o Acesso Total ao
Disco tinha que ser concedido ao **Terminal** — e qualquer coisa rodando no
Terminal passava a ter acesso total ao seu disco.

Com o app nativo, o `python3 -m ultrabackup tui` é filho do UltraBackup.app.
Conceda o FDA **ao UltraBackup.app, NÃO ao Terminal**:

1. **Ajustes do Sistema → Privacidade e Segurança → Acesso Total ao Disco**
2. Adicione (`+`) e habilite **UltraBackup.app**
3. **Encerre e reabra o app** (a permissão só vale para processos novos)
4. Confira na tela *Doctor* da TUI — a mensagem de FDA agora diz
   `UltraBackup`, porque o app exporta `ULTRABACKUP_HOST_APP` e
   `preflight.terminal_host_app()` honra essa variável antes de `TERM_PROGRAM`

Se você já tinha concedido FDA ao Terminal só por causa do UltraBackup, pode
**revogar** — o app nativo não precisa dele.

Sem FDA, o macOS bloqueia a leitura de `~/Library/Containers`,
`~/Library/Cookies`, Mail, Safari etc. — o backup sai **parcial** sem aviso do
sistema. Pela CLI (`python3 -m ultrabackup ...` num terminal) a regra antiga
continua valendo: aí quem precisa do FDA é o terminal.

### Como compilar

Requer Xcode (ou as Command Line Tools com Swift) e rede na primeira vez, para
o SwiftPM baixar o SwiftTerm:

```bash
native/build_app.sh
open app/UltraBackup.app
```

O script faz `swift build -c release`, monta o bundle
(`Contents/MacOS/UltraBackup` = binário nativo, `Info.plist`,
`Resources/AppIcon.icns` preservado), copia o pacote Python para dentro do
bundle, assina com `codesign --force --deep -s -` (ad-hoc) e **verifica o
selo** com `codesign --verify --deep` (mais um passe `--strict` sobre uma cópia
limpa) — assinatura quebrada reprova o build.

Para instalar:

```bash
ditto --noextattr --norsrc app/UltraBackup.app /Applications/UltraBackup.app
```

Use `ditto --noextattr --norsrc`, **não `cp -R`**: `cp -R` leva junto o
`com.apple.FinderInfo` que o Finder/iCloud gruda em pastas do repositório, e
esse xattr faz o `codesign --verify --strict` rejeitar o bundle.

### Self-contained

`build_app.sh` copia `ultrabackup/` (incluindo `known_apps.json`, sem
`__pycache__`) para `Contents/Resources/ultrabackup/`, e o app roda o motor
com `PYTHONPATH` e diretório de trabalho apontando para lá. **Nada aponta para
o caminho do repositório** — o `.app` funciona copiado para `/Applications` ou
para outra máquina. A única dependência externa é o **python3 do sistema**
(Command Line Tools: `xcode-select --install`).

### Rebuild **ou** mover o bundle ⇒ re-conceder FDA

Para um app assinado ad-hoc o macOS amarra a concessão do TCC ao **cdhash** do
bundle **e ao caminho** dele. Você precisa re-conceder o Acesso Total ao Disco
sempre que um dos dois mudar:

- **depois de todo rebuild** (binário novo ⇒ cdhash novo);
- **depois de mover ou copiar** o `.app` — conceder FDA em
  `app/UltraBackup.app` dentro do repositório **não vale** para
  `/Applications/UltraBackup.app`. Sem isso o app abre normalmente e o backup
  sai **parcial, em silêncio**.

Desligue e religue o toggle (ou remova com `-` e adicione o bundle que você
realmente vai abrir), e então encerre e reabra o app. O caminho mais simples:
instale primeiro em `/Applications`, conceda FDA lá uma vez, e sempre abra essa
cópia. O `build_app.sh` imprime esse aviso ao final.

### Ciclo de vida

- O app encerra quando o motor Python termina **com sucesso** (você sai pela
  TUI com `q`). Se o motor morre com erro, a janela **fica aberta** com o
  diagnóstico na tela (`Press Cmd-Q to close.`) em vez de sumir.
- O motor é encerrado quando o app encerra (Cmd+Q, fechar a janela, `pkill`) —
  **nunca fica `python3` órfão**. Se o motor ainda estiver rodando, o Cmd+Q
  pede confirmação antes de interromper (`Keep Running` / `Quit Anyway`);
  SIGTERM/logout não pedem — encerram direto (SIGINT ⇒ SIGTERM ⇒ SIGKILL).
- Janela: tema escuro (`#1e2430`), SF Mono 13 (fallback Menlo), ~120x34
  células, redimensionável (o resize propaga o `winsize` para o pty).
  Cmd+C / Cmd+V funcionam via o menu Edit.

## Uso — exemplo completo com o Claude

O caso de uso que motivou a ferramenta: **Claude Desktop + Claude Code CLI**.
Isso cobre `/Applications/Claude.app`, `~/Library/Application Support/Claude`,
`~/Library/Preferences/com.anthropic.*`, containers, launch agents, e os
extras curados `~/.claude/` e `~/.claude.json` (onde vive o token OAuth do
Claude Code).

```bash
# 1. O que existe instalado?
ultrabackup list

# 2. O que seria capturado (e o que o macOS proíbe)?
ultrabackup inspect claude

# 3. Preflight: FDA ok? espaço? app fechado?
ultrabackup doctor claude

# 4. Feche o Claude (Cmd+Q — inclusive o ícone da barra de menus) e:
ultrabackup backup claude
# → ~/UltraBackups/Claude_2026-08-28T14-05-11/

# 5. Listar e verificar backups
ultrabackup backups
ultrabackup verify ~/UltraBackups/Claude_2026-08-28T14-05-11

# 6. Restore — SEMPRE dry-run primeiro (default):
ultrabackup restore ~/UltraBackups/Claude_2026-08-28T14-05-11

# 7. Restore de verdade (pede confirmação; app precisa estar fechado):
ultrabackup restore ~/UltraBackups/Claude_2026-08-28T14-05-11 --apply

# 8. Deu ruim? Desfaz o restore inteiro via journal:
ultrabackup rollback ~/UltraBackups/Claude_2026-08-28T14-05-11
```

Flags úteis:

| Flag | Onde | Efeito |
|---|---|---|
| `--include-caches` | `inspect`, `backup` | inclui `~/Library/Caches` (grande, regenerável) |
| `--exclude PAT` | `backup` | exclui categoria ou padrão de caminho (repetível) |
| `--dest D` | `backup`, `backups`, `doctor` | destino (default `~/UltraBackups`) |
| `--only CAT` / `--exclude CAT` | `restore` | restaura só (ou tudo menos) certas categorias |
| `--overwrite-newer` | `restore` | sobrescreve alvos modificados depois do backup (default: skip) |
| `--strip-quarantine` | `restore` | remove `com.apple.quarantine` do bundle restaurado |
| `--yes` | `backup`, `restore` | pula confirmação (necessário em scripts/non-TTY) |
| `--force` | `backup`, `restore` | ignora bloqueios de preflight/version-skew/app rodando |
| `--json` | comandos de leitura | saída em JSON (`list`, `inspect`, `backups`, `verify`, `doctor`) |

## Comandos e exit codes

```
ultrabackup list                                # apps em /Applications
ultrabackup inspect <app> [--include-caches]    # o que seria capturado + não-capturável
ultrabackup backup <app> [--dest D] [--include-caches] [--exclude PAT] [--force] [--yes]
ultrabackup backups [--dest D]                  # backups existentes
ultrabackup restore <backup-dir> [--apply] [--only CAT] [--exclude CAT]
                    [--overwrite-newer] [--strip-quarantine] [--force] [--yes]
ultrabackup verify <backup-dir>                 # checksums manifest vs payload
ultrabackup rollback <backup-dir>               # desfaz último restore via journal
ultrabackup doctor [<app>] [--dest D]           # preflights
ultrabackup tui [--dest D]                      # interface de terminal (TUI)
```

| Código | Significado |
|---|---|
| 0 | ok |
| 1 | erro |
| 2 | uso incorreto |
| 3 | backup parcial (itens com permissão negada) |
| 4 | verify: divergência payload vs manifest |
| 5 | restore falhou; rollback automático executado |
| 6 | rollback incompleto — verifique o estado manualmente |
| 7 | confirmação necessária em non-TTY (use `--yes`) |

Nunca há prompt em non-TTY: sem `--yes`, comandos que mutam saem com código 7.

## Formato do backup

```
~/UltraBackups/Claude_2026-08-28T14-05-11/
  manifest.json          # escrito por ÚLTIMO, atomicamente = marcador de backup completo
  payload/<item-id>/<basename>   # árvores copiadas com ditto (xattrs, ACLs, symlinks)
  restore-journal.json   # criado por restore --apply; usado pelo rollback
```

Um diretório **sem `manifest.json` válido é um backup incompleto/inválido** e
aparece assim no `ultrabackup backups`. O diretório é criado com `chmod 700`.

## Garantias de segurança do design

- **Symlinks nunca são seguidos** em scan, hash, cópia ou restore. Isso
  importa: `~/Library/Containers/<id>/Data` contém symlinks para `~/Documents`,
  `~/Desktop` etc. — segui-los copiaria o home inteiro e o restore
  sobrescreveria os diretórios reais.
- **Payload só com `ditto`** (preserva xattrs, ACLs, resource forks, symlinks
  e assinatura de código).
- **Restore é dry-run por default** — só muta com `--apply`; todo alvo
  existente é movido para o lado (move-aside) antes, e um journal permite
  `rollback` completo.
- **Preferences via `defaults import`** (nunca cópia crua + kill do cfprefsd).
- **App não pode estar rodando** durante backup nem restore — bancos SQLite em
  WAL e LevelDB de apps Electron corrompem. O UltraBackup recusa e explica;
  `--force` existe, com aviso.

## Limitações

Estas limitações são **do macOS**, não escolhas da ferramenta. O relatório
"capturado vs. NÃO capturável" é impresso ao fim de cada execução.

1. **Keychain / `safeStorage`** — senhas, tokens e chaves guardados no
   Keychain **não** entram no backup. Depois do restore, apps pedirão login de
   novo. Pior no cross-machine: cookies e estado cifrados via `safeStorage`
   (Electron/Chromium) de outra máquina são **indecifráveis** — a chave de
   cifra vive no Keychain da máquina de origem.
2. **TCC (Privacidade)** — permissões de câmera, microfone, gravação de tela,
   Acesso Total ao Disco etc. não são restauráveis por arquivo (o banco TCC é
   protegido por SIP). Re-conceda em Ajustes do Sistema → Privacidade e
   Segurança.
3. **BTM (itens de login / launch agents aprovados)** — o macOS exige
   re-aprovação humana: Ajustes do Sistema → Geral → Itens de Início de Sessão.
4. **Recibo da Mac App Store** — `_MASReceipt` é atrelado à máquina/conta.
   Cross-machine, apps da MAS devem ser **reinstalados pela App Store** (os
   dados do usuário restaurados continuam valendo).
5. **SIP** — caminhos protegidos pelo System Integrity Protection não são
   graváveis, nem com root.
6. **`ditto` não preserva hardlinks** (viram cópias independentes) **nem
   garante sparse files** (podem ocupar tamanho cheio).
7. **iCloud** — arquivos somente-na-nuvem (dataless) geram aviso e são
   pulados; destino dentro de iCloud Drive/Dropbox gera aviso (sync corrompe
   backups em andamento).
8. **`/Library` exige root** — launch daemons e Application Support de sistema
   são pulados com aviso quando não há `sudo` (a ferramenta nunca eleva
   privilégios sozinha).
9. **O app deve estar fechado** durante backup e restore.

## Segurança dos backups

O backup pode conter **segredos**: `~/.claude.json` inclui o token OAuth do
Claude Code; cookies e `HTTPStorages` vão junto. O diretório é criado com
`chmod 700`, mas trate-o como material sensível — não sincronize para
armazenamento não confiável sem cifrar antes.

## Desenvolvimento

```bash
python3 -m unittest discover -s tests -v
```

Os testes rodam contra um home falso injetado (`home=` em todas as APIs) e não
tocam o seu home real.

