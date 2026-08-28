"""Testes da camada de MODELO da TUI (ultrabackup.tui) — puro, sem curses.

Cobre: filtro incremental, toggle de checkbox, marcar-todos-visíveis,
transições de tela, formatação de linha de checkbox e formatação de evento de
progresso em linha de log. Nenhum teste toca curses nem pty; o round-trip
real da TUI fica com o revisor funcional (script pty), fora deste suite.
"""

import unittest

from ultrabackup import tui


class CursesFreeModelTests(unittest.TestCase):
    def test_importing_tui_does_not_import_curses(self):
        # O contrato model/view: importar o módulo NÃO pode puxar curses —
        # ele só é carregado tardiamente dentro de run_tui().
        self.assertIsNone(tui.curses)


class FilterTests(unittest.TestCase):
    def _list(self):
        return tui.CheckboxList(["Claude", "Safari", "Notes", "Claude Code"])

    def test_empty_filter_shows_everything(self):
        lst = self._list()
        self.assertEqual(lst.visible(), ["Claude", "Safari", "Notes", "Claude Code"])

    def test_filter_narrows_case_insensitively(self):
        lst = self._list()
        lst.set_filter("cLa")
        self.assertEqual(lst.visible(), ["Claude", "Claude Code"])
        lst.set_filter("safari")
        self.assertEqual(lst.visible(), ["Safari"])

    def test_filter_with_no_match_yields_empty_visible(self):
        lst = self._list()
        lst.set_filter("zzz")
        self.assertEqual(lst.visible(), [])
        self.assertIsNone(lst.current_row())

    def test_clearing_filter_restores_full_list(self):
        lst = self._list()
        lst.set_filter("notes")
        lst.set_filter("")
        self.assertEqual(len(lst.visible()), 4)

    def test_filter_clamps_cursor(self):
        lst = self._list()
        lst.move(3)  # cursor no último
        lst.set_filter("safari")  # só 1 visível
        self.assertEqual(lst.cursor, 0)
        self.assertEqual(lst.current_row(), "Safari")

    def test_filter_uses_custom_text_fn(self):
        rows = [
            {"name": "Claude", "bundle_id": "com.anthropic.claudefordesktop"},
            {"name": "Notes", "bundle_id": "com.apple.Notes"},
        ]
        lst = tui.CheckboxList(
            rows, text_fn=lambda r: "{} {}".format(r["name"], r["bundle_id"])
        )
        lst.set_filter("anthropic")
        self.assertEqual([r["name"] for r in lst.visible()], ["Claude"])


class ToggleTests(unittest.TestCase):
    def test_toggle_current_marks_and_unmarks(self):
        lst = tui.CheckboxList(["Claude", "Safari", "Notes"])
        lst.toggle_current()
        self.assertEqual(lst.checked_rows(), ["Claude"])
        lst.move(1)
        lst.toggle_current()
        self.assertEqual(lst.checked_rows(), ["Claude", "Safari"])
        lst.toggle_current()  # desmarca Safari
        self.assertEqual(lst.checked_rows(), ["Claude"])
        self.assertEqual(lst.checked_count(), 1)

    def test_toggle_on_empty_visible_is_noop(self):
        lst = tui.CheckboxList(["Claude"])
        lst.set_filter("zzz")
        lst.toggle_current()
        self.assertEqual(lst.checked_rows(), [])

    def test_checked_survives_filter_changes(self):
        lst = tui.CheckboxList(["Claude", "Safari", "Notes"])
        lst.toggle_current()  # Claude
        lst.set_filter("saf")
        self.assertEqual(lst.checked_rows(), ["Claude"])  # marcação persiste
        lst.toggle_current()  # marca Safari (único visível)
        lst.set_filter("")
        self.assertEqual(lst.checked_rows(), ["Claude", "Safari"])

    def test_move_clamps_at_both_ends(self):
        lst = tui.CheckboxList(["a", "b", "c"])
        lst.move(-5)
        self.assertEqual(lst.cursor, 0)
        lst.move(99)
        self.assertEqual(lst.cursor, 2)


class ToggleAllVisibleTests(unittest.TestCase):
    def test_toggle_all_checks_only_visible(self):
        lst = tui.CheckboxList(["Claude", "Safari", "Claude Code"])
        lst.set_filter("claude")
        lst.toggle_all_visible()
        self.assertEqual(lst.checked_rows(), ["Claude", "Claude Code"])
        lst.set_filter("")
        self.assertEqual(lst.checked_count(), 2)  # Safari continua desmarcado

    def test_toggle_all_unchecks_when_all_visible_checked(self):
        lst = tui.CheckboxList(["Claude", "Safari"])
        lst.toggle_all_visible()
        self.assertEqual(lst.checked_count(), 2)
        lst.toggle_all_visible()
        self.assertEqual(lst.checked_count(), 0)

    def test_toggle_all_on_mixed_state_checks_the_rest(self):
        lst = tui.CheckboxList(["Claude", "Safari", "Notes"])
        lst.toggle_current()  # só Claude marcado
        lst.toggle_all_visible()
        self.assertEqual(lst.checked_count(), 3)

    def test_toggle_all_with_filter_does_not_touch_hidden_checked(self):
        lst = tui.CheckboxList(["Claude", "Safari"])
        lst.toggle_current()  # Claude marcado
        lst.set_filter("safari")
        lst.toggle_all_visible()  # marca Safari
        lst.toggle_all_visible()  # desmarca Safari; Claude (oculto) intacto
        self.assertEqual(lst.checked_rows(), ["Claude"])


class TransitionTests(unittest.TestCase):
    def test_initial_screen_is_home(self):
        state = tui.AppState()
        self.assertEqual(state.screen, tui.SCREEN_HOME)
        self.assertFalse(state.quit)

    def test_go_pushes_and_back_pops(self):
        state = tui.AppState()
        state.go(tui.SCREEN_APPS)
        state.go(tui.SCREEN_CONFIRM)
        self.assertEqual(state.screen, tui.SCREEN_CONFIRM)
        self.assertTrue(state.back())
        self.assertEqual(state.screen, tui.SCREEN_APPS)
        self.assertTrue(state.back())
        self.assertEqual(state.screen, tui.SCREEN_HOME)

    def test_back_on_empty_stack_returns_false(self):
        state = tui.AppState()
        self.assertFalse(state.back())
        self.assertEqual(state.screen, tui.SCREEN_HOME)

    def test_home_clears_the_stack(self):
        state = tui.AppState()
        state.go(tui.SCREEN_BACKUPS)
        state.go(tui.SCREEN_PLAN)
        state.go(tui.SCREEN_RESULT)
        state.home()
        self.assertEqual(state.screen, tui.SCREEN_HOME)
        self.assertFalse(state.back())

    def test_menu_targets_cover_the_spec_screens(self):
        self.assertEqual(tui.MENU_TARGETS["backup"], tui.SCREEN_APPS)
        self.assertEqual(tui.MENU_TARGETS["restore"], tui.SCREEN_BACKUPS)
        self.assertEqual(tui.MENU_TARGETS["verify"], tui.SCREEN_BACKUPS)
        self.assertEqual(tui.MENU_TARGETS["doctor"], tui.SCREEN_OUTPUT)
        self.assertIsNone(tui.MENU_TARGETS["quit"])
        menu_ids = [mid for mid, _label in tui.MENU_ITEMS]
        self.assertEqual(sorted(menu_ids), sorted(tui.MENU_TARGETS.keys()))


class CheckboxLineFormatTests(unittest.TestCase):
    def test_unchecked_line_shape(self):
        line = tui.format_checkbox_line(
            False, "Claude", "1.5.2", "com.anthropic.claudefordesktop"
        )
        self.assertTrue(line.startswith("[ ] Claude"))
        self.assertIn("1.5.2", line)
        self.assertTrue(line.endswith("com.anthropic.claudefordesktop"))
        # Ordem das colunas: nome, versão, bundle-id.
        self.assertLess(line.index("Claude"), line.index("1.5.2"))
        self.assertLess(line.index("1.5.2"), line.index("com.anthropic"))

    def test_checked_line_uses_x(self):
        line = tui.format_checkbox_line(True, "Claude", "1.0", "com.x")
        self.assertTrue(line.startswith("[x] Claude"))

    def test_missing_fields_get_placeholders(self):
        line = tui.format_checkbox_line(False, "Claude Code", None, None)
        self.assertTrue(line.startswith("[ ] Claude Code"))
        self.assertIn("?", line)   # versão desconhecida
        self.assertTrue(line.endswith("-"))  # sem bundle id

    def test_no_trailing_whitespace(self):
        line = tui.format_checkbox_line(False, "A", "1", None)
        self.assertEqual(line, line.rstrip())


class ProgressLineFormatTests(unittest.TestCase):
    def test_copied_entry_line(self):
        line = tui.format_progress_line(
            {"id": "0001", "category": "app_bundle", "status": "copied",
             "size_bytes": 2048}
        )
        self.assertTrue(line.startswith("[0001] app_bundle"))
        self.assertIn("… copied", line)
        self.assertIn("2.0 KiB", line)

    def test_missing_and_denied_entries(self):
        missing = tui.format_progress_line(
            {"id": "0002", "category": "cookies", "status": "missing"}
        )
        self.assertIn("… missing", missing)
        self.assertNotIn("KiB", missing)
        denied = tui.format_progress_line(
            {"id": "0003", "category": "containers",
             "status": "permission_denied"}
        )
        self.assertTrue(denied.startswith("[0003] containers"))
        self.assertIn("permission_denied", denied)

    def test_progress_event_lands_in_backup_log_with_severity_tag(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.apply_event({"type": "item", "entry": {
            "id": "0001", "category": "app_bundle", "status": "copied",
            "size_bytes": 10}})
        flow.apply_event({"type": "item", "entry": {
            "id": "0002", "category": "cookies", "status": "missing"}})
        flow.apply_event({"type": "item", "entry": {
            "id": "0003", "category": "containers",
            "status": "permission_denied"}})
        tags = [tag for tag, _text in flow.log]
        self.assertEqual(tags, ["ok", "warn", "err"])
        self.assertIn("[0001] app_bundle", flow.log[0][1])
        self.assertIn("[0003] containers", flow.log[2][1])

    def test_app_done_event_logs_completeness_path_and_capability_summary(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.apply_event({"type": "app_done", "name": "Claude",
                          "completeness": "COMPLETE",
                          "backup_dir": "/tmp/dest/Claude_2026",
                          "copied": 5, "total": 6, "partial": False})
        text = "\n".join(line for _tag, line in flow.log)
        self.assertIn("Claude: COMPLETE — /tmp/dest/Claude_2026", text)
        self.assertIn("5/6 itens capturados", text)
        self.assertIn("NÃO capturável", text)
        self.assertIn("Keychain", text)

    def test_all_done_marks_flow_finished(self):
        flow = tui.BackupFlow("/tmp/dest")
        self.assertFalse(flow.done)
        flow.apply_event({"type": "all_done"})
        self.assertTrue(flow.done)


class ConfirmModelTests(unittest.TestCase):
    """A tela de confirmação é construída por uma função pura sobre o flow."""

    class _FakeApp:
        name = "Claude"
        version = "1.0"
        bundle_id = "com.anthropic.claudefordesktop"

    def _flow_with_entry(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.entries = [tui.ConfirmEntry(self._FakeApp())]
        return flow

    def test_size_placeholder_until_background_size_event(self):
        flow = self._flow_with_entry()
        flow.entries[0].items = []
        text = "\n".join(line for _tag, line in tui.build_confirm_lines(flow))
        self.assertIn("tamanho estimado: …", text)
        flow.apply_event({"type": "size", "index": 0, "bytes": 4096})
        text = "\n".join(line for _tag, line in tui.build_confirm_lines(flow))
        self.assertIn("tamanho estimado: 4.0 KiB", text)

    def test_analysis_event_fills_found_count_per_category(self):
        flow = self._flow_with_entry()
        flow.apply_event({"type": "analysis", "index": 0, "error": None,
                          "items": [
                              {"category": "app_bundle", "status": "found"},
                              {"category": "preferences", "status": "found"},
                              {"category": "preferences", "status": "found"},
                              {"category": "cookies", "status": "missing"},
                          ]})
        entry = flow.entries[0]
        self.assertEqual(entry.found, 3)
        self.assertEqual(entry.counts, {"app_bundle": 1, "preferences": 2})
        text = "\n".join(line for _tag, line in tui.build_confirm_lines(flow))
        self.assertIn("itens encontrados: 3", text)
        self.assertIn("preferences: 2", text)

    def test_running_processes_render_as_errors(self):
        flow = self._flow_with_entry()
        flow.entries[0].items = []
        flow.apply_event({"type": "running", "index": 0,
                          "procs": ["pid 42: /Applications/Claude.app"]})
        self.assertTrue(flow.any_running())
        lines = tui.build_confirm_lines(flow)
        running = [line for tag, line in lines if tag == "err"]
        self.assertTrue(any("EM EXECUÇÃO" in line for line in running))


class AnalyzingGateTests(unittest.TestCase):
    """Enter na confirmação só libera após discovery E checagem de processos.

    O worker posta ``analysis`` antes do ``running`` de cada app: sem exigir
    o ``running``, um enter entre os dois eventos iniciaria backup de app
    rodando sem a confirmação extra da spec.
    """

    def _flow_with_entry(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.entries = [tui.ConfirmEntry(ConfirmModelTests._FakeApp())]
        return flow

    def test_analysis_alone_still_counts_as_analyzing(self):
        flow = self._flow_with_entry()
        flow.apply_event({"type": "analysis", "index": 0, "error": None,
                          "items": []})
        self.assertTrue(flow.analyzing())

    def test_running_event_completes_the_analysis(self):
        flow = self._flow_with_entry()
        flow.apply_event({"type": "analysis", "index": 0, "error": None,
                          "items": []})
        flow.apply_event({"type": "running", "index": 0, "procs": []})
        self.assertFalse(flow.analyzing())

    def test_analysis_error_still_waits_for_process_check(self):
        flow = self._flow_with_entry()
        flow.apply_event({"type": "analysis", "index": 0, "error": "boom",
                          "items": []})
        self.assertTrue(flow.analyzing())
        flow.apply_event({"type": "running", "index": 0, "procs": []})
        self.assertFalse(flow.analyzing())


class StaleEventGenerationTests(unittest.TestCase):
    """drain_events descarta eventos de workers de gerações superadas."""

    def _controller(self):
        from pathlib import Path

        return tui._Controller(Path("/tmp/home"), Path("/tmp/dest"))

    def test_stale_analysis_event_is_discarded(self):
        ctl = self._controller()
        ctl.backup_gen = 2
        ctl.backup = tui.BackupFlow("/tmp/dest")
        ctl.backup.entries = [tui.ConfirmEntry(ConfirmModelTests._FakeApp())]
        ctl.queue.put({"type": "analysis", "index": 0, "error": None,
                       "items": [{"category": "app_bundle", "status": "found"}],
                       "gen": 1})
        ctl.drain_events()
        self.assertIsNone(ctl.backup.entries[0].items)

    def test_current_generation_event_is_applied(self):
        ctl = self._controller()
        ctl.backup_gen = 2
        ctl.backup = tui.BackupFlow("/tmp/dest")
        ctl.backup.entries = [tui.ConfirmEntry(ConfirmModelTests._FakeApp())]
        ctl.queue.put({"type": "analysis", "index": 0, "error": None,
                       "items": [{"category": "app_bundle", "status": "found"}],
                       "gen": 2})
        ctl.drain_events()
        self.assertEqual(len(ctl.backup.entries[0].items), 1)


class RestorePlanEventTests(unittest.TestCase):
    """Eventos do plano de restore (worker de fundo) e o gate de skew."""

    def _controller_with_plan(self, skew, procs):
        from pathlib import Path

        ctl = tui._Controller(Path("/tmp/home"), Path("/tmp/dest"))
        ctl.restore_gen = 1
        flow = tui.RestoreFlow("/tmp/dest")
        ctl.restore = flow
        flow.apply_event({
            "type": "plan",
            "manifest": {"app": {"name": "Claude"}},
            "backup_dir": "/tmp/dest/x",
            "plan": [],
            "warnings": [],
            "skew": skew,
            "running": [],
        })
        return ctl, flow

    def test_plan_event_fills_flow_and_clears_loading(self):
        ctl, flow = self._controller_with_plan([], [])
        self.assertFalse(flow.plan_loading)
        self.assertEqual(flow.backup_dir, "/tmp/dest/x")
        self.assertEqual(flow.confirm_stage, 0)

    def test_apply_check_with_running_goes_to_stage_2(self):
        ctl, flow = self._controller_with_plan([], [])
        flow.checking = True
        ctl.queue.put({"type": "plan_running", "purpose": "apply_check",
                       "procs": ["pid 42"], "gen": 1})
        ctl.drain_events()
        self.assertFalse(flow.checking)
        self.assertEqual(flow.confirm_stage, 2)

    def test_apply_check_with_skew_requires_stage_3(self):
        ctl, flow = self._controller_with_plan(["app 2.0 vs backup 1.0"], [])
        flow.checking = True
        ctl.queue.put({"type": "plan_running", "purpose": "apply_check",
                       "procs": [], "gen": 1})
        ctl.drain_events()
        self.assertEqual(flow.confirm_stage, 3)
        self.assertFalse(flow.applying)  # NUNCA aplica direto com skew


class ScrollHelperTests(unittest.TestCase):
    def test_scroll_window_keeps_cursor_visible(self):
        self.assertEqual(tui.scroll_window(0, 0, 5), 0)
        self.assertEqual(tui.scroll_window(7, 0, 5), 3)   # desce até mostrar
        self.assertEqual(tui.scroll_window(2, 4, 5), 2)   # sobe até mostrar
        self.assertEqual(tui.scroll_window(6, 3, 5), 3)   # já visível: mantém

    def test_clamp_offset(self):
        self.assertEqual(tui.clamp_offset(99, 10, 4), 6)
        self.assertEqual(tui.clamp_offset(-3, 10, 4), 0)
        self.assertEqual(tui.clamp_offset(2, 3, 10), 0)


if __name__ == "__main__":
    unittest.main()


class SelectionSizeTests(unittest.TestCase):
    """Tamanhos na lista de seleção: formatação, evento app_size e cache key."""

    def test_checkbox_line_includes_size_column(self):
        line = tui.format_checkbox_line(False, "Claude", "1.0", "com.x",
                                        size_text="22.5 GiB")
        self.assertIn("22.5 GiB", line)
        self.assertLess(line.index("22.5 GiB"), line.index("com.x"))

    def test_checkbox_line_without_size_unchanged_shape(self):
        line = tui.format_checkbox_line(True, "Claude", "1.0", "com.x")
        self.assertTrue(line.startswith("[x] Claude"))
        self.assertNotIn("GiB", line)

    def test_app_size_event_updates_selection_row(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.apply_event({"type": "apps", "rows": [
            {"name": "A", "bundle_id": "com.a", "size_bytes": None},
            {"name": "B", "bundle_id": "com.b", "size_bytes": None},
        ], "error": None})
        flow.apply_event({"type": "app_size", "row_index": 1, "bytes": 123})
        self.assertIsNone(flow.selection.rows[0]["size_bytes"])
        self.assertEqual(flow.selection.rows[1]["size_bytes"], 123)

    def test_app_size_event_out_of_range_is_ignored(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.apply_event({"type": "apps", "rows": [{"name": "A"}], "error": None})
        flow.apply_event({"type": "app_size", "row_index": 9, "bytes": 1})
        flow.apply_event({"type": "app_size", "row_index": None, "bytes": 1})

    def test_app_cache_key_prefers_bundle_id(self):
        class App:
            bundle_id = "com.a"
            name = "A"
        self.assertEqual(tui.app_cache_key(App()), "com.a")
        App.bundle_id = None
        self.assertEqual(tui.app_cache_key(App()), "A")


class FdaConfirmTests(unittest.TestCase):
    def test_denied_lines_name_the_host_and_f_key(self):
        flow = tui.BackupFlow("/tmp/dest")
        flow.fda = "denied"
        flow.fda_host = "Terminal"
        text = "\n".join(t for _, t in tui.build_confirm_lines(flow))
        self.assertIn("ATIVE 'Terminal'", text)
        self.assertIn("Pressione f", text)
        self.assertIn("Acesso Total ao Disco", text)
