"""Disposable data only; no installed application or real API settings."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easyread import config, data_migration as migration, data_paths, updates


class UpgradeSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'legacy'; self.dest=self.root/'new'; self.backup=self.root/'backup'
        paper=self.source/'library/paper001'; paper.mkdir(parents=True)
        values={'source.pdf':b'%PDF-synthetic', 'paper.json':'{"blocks":[{"en":"Original","zh":"繁體","zh_old":"简体"}]}'.encode(),
                'reader.json':b'{"notes":{"n":{"body":"Keep"}}}', 'manual_table_links.json':b'{"revision":7,"links":[]}',
                'history/old.json':b'{"keep":true}', 'cache/table.json':b'{"snapshot":"keep"}'}
        for rel,value in values.items():
            path=paper/rel; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(value)
        (self.source/'config.json').write_text(json.dumps({'library_dir':str(self.source/'library'), 'openai':{'api_key':'synthetic-only'},'unknown':{'keep':True},'check_updates':True}))
        (self.source/'prefs.json').write_text('{"reader":{"mode":"both"}}')
        (self.source/'PaperRead.exe').write_bytes(b'not user data')
        self.initial={p.relative_to(self.source).as_posix():p.read_bytes() for p in self.source.rglob('*') if p.is_file()}

    def run_migration(self,**kw):
        return migration.migrate(self.source,self.dest,self.backup,confirm_closed=True,**kw)

    def test_copy_verify_switch_preserves_all_protected_content_and_source(self):
        result=self.run_migration(); self.assertEqual(result['phase'],'complete')
        for rel,value in self.initial.items():
            self.assertEqual((self.source/rel).read_bytes(),value)
            if rel not in ('config.json','PaperRead.exe'): self.assertEqual((self.dest/(rel if rel.startswith('library/') else 'home/'+rel)).read_bytes(),value)
        cfg=json.loads((self.dest/'home/config.json').read_text())
        self.assertEqual(cfg['openai']['api_key'],'synthetic-only'); self.assertEqual(cfg['unknown'],{'keep':True})
        self.assertFalse(cfg['check_updates']); self.assertEqual(cfg['library_dir'],str(self.dest/'library'))
        self.assertEqual((self.backup/'home/config.json').read_bytes(),self.initial['config.json'])
        self.assertFalse((self.dest/'home/PaperRead.exe').exists())

    def test_completed_retry_is_idempotent(self):
        self.run_migration(); self.assertTrue(self.run_migration()['resumed'])

    def test_interruption_before_switch_can_resume(self):
        def stop(_): raise RuntimeError('Interrupted')
        with self.assertRaises(RuntimeError): self.run_migration(checkpoint=stop)
        self.assertFalse((self.dest/'home/config.json').exists())
        self.assertEqual(json.loads((self.dest/'migration.json').read_text())['phase'],'copy')
        self.assertEqual(self.run_migration()['phase'],'complete')

    def test_destination_conflict_not_overwritten(self):
        path=self.dest/'library/paper001/reader.json'; path.parent.mkdir(parents=True); path.write_bytes(b'new human note')
        with self.assertRaises(ValueError): self.run_migration()
        self.assertEqual(path.read_bytes(),b'new human note'); self.assertFalse((self.dest/'home/config.json').exists())

    def test_config_conflict_not_merged(self):
        path=self.dest/'home/config.json';path.parent.mkdir(parents=True);path.write_text('{"unknown":"different"}')
        with self.assertRaises(ValueError): self.run_migration()
        self.assertEqual(path.read_text(),'{"unknown":"different"}')

    def test_backup_conflict_blocks_all_destination_user_data(self):
        path=self.backup/'home/prefs.json';path.parent.mkdir(parents=True);path.write_text('conflict')
        with self.assertRaises(ValueError):self.run_migration()
        self.assertFalse((self.dest/'home').exists());self.assertEqual(path.read_text(),'conflict')

    def test_backup_tampering_before_copy_is_rejected(self):
        original=migration.copy_verified
        def corrupt(source,target,expected):
            original(source,target,expected)
            if target == self.backup/'home/prefs.json': target.write_text('bad backup')
        with patch.object(migration,'copy_verified',side_effect=corrupt),self.assertRaises(ValueError): self.run_migration()
        self.assertFalse((self.dest/'home').exists())

    def test_source_change_during_copy_prevents_switch(self):
        def change(_): (self.source/'prefs.json').write_text('changed during migration')
        with self.assertRaises(ValueError):self.run_migration(checkpoint=change)
        self.assertFalse((self.dest/'home/config.json').exists())

    def test_live_destination_edits_never_replaced_on_rerun(self):
        self.run_migration();path=self.dest/'library/paper001/reader.json';path.write_text('new note')
        with self.assertRaises(ValueError): self.run_migration()
        self.assertEqual(path.read_text(),'new note')

    def test_explicit_closed_acknowledgement_required(self):
        with self.assertRaises(ValueError):migration.migrate(self.source,self.dest,self.backup)
        self.assertFalse(self.dest.exists())

    def test_paths_must_not_overlap(self):
        with self.assertRaises(ValueError):migration.migrate(self.source,self.source/'new',self.backup,confirm_closed=True)

    def test_external_library_requires_explicit_matching_selection(self):
        lib=self.root/'external';lib.mkdir();(lib/'file').write_text('data')
        (self.source/'config.json').write_text(json.dumps({'library_dir':str(lib)}))
        with self.assertRaises(ValueError):self.run_migration()
        self.run_migration(library=lib);self.assertEqual((self.dest/'library/file').read_text(),'data')

    def test_profile_and_nonstandard_cache_preserved(self):
        profile=self.root/'old-profile';profile.mkdir();(profile/'window-state.json').write_text('{"bounds":{"width":1000}}')
        (self.source/'.research-os').mkdir();(self.source/'.research-os/index.sqlite3').write_bytes(b'cache fixture')
        self.run_migration(profile=profile)
        self.assertEqual((self.dest/'desktop/window-state.json').read_bytes(),(profile/'window-state.json').read_bytes())
        self.assertEqual((self.dest/'home/.research-os/index.sqlite3').read_bytes(),b'cache fixture')

    def test_binary_inside_selected_user_data_rejected(self):
        (self.source/'library/unsafe.dll').write_bytes(b'program')
        with self.assertRaises(ValueError):self.run_migration()

    def test_incomplete_destination_cannot_start(self):
        self.dest.mkdir();(self.dest/'migration.json').write_text('{"phase":"copy"}')
        with patch.object(config,'HOME',self.dest/'home'),self.assertRaises(ValueError):config._migrate_legacy_home()

    def test_standard_windows_location_and_overlap_guard(self):
        self.assertEqual(data_paths.data_root('win32',{'LOCALAPPDATA':str(self.root)},self.source),self.root/'PaperRead-Custom')
        with self.assertRaises(ValueError):data_paths.outside(self.source/'library',self.source)
        with self.assertRaises(ValueError):data_paths.outside(self.source,self.source/'resources')

    def test_old_lock_files_not_copied_or_deleted(self):
        lock=self.source/'library/paper001/.write.lock';lock.write_bytes(b'permanent')
        self.run_migration();self.assertEqual(lock.read_bytes(),b'permanent')
        self.assertFalse((self.dest/'library/paper001/.write.lock').exists())

    def test_partial_orphan_does_not_prevent_safe_retry(self):
        path=self.dest/'library/paper001/reader.json.migration-partial-interrupted';path.parent.mkdir(parents=True);path.write_bytes(b'incomplete')
        self.run_migration();self.assertEqual(path.read_bytes(),b'incomplete')
        self.assertEqual((path.parent/'reader.json').read_bytes(),self.initial['library/paper001/reader.json'])

    def test_runtime_repository_cannot_enable_any_update_network(self):
        with patch.object(updates,'REPO','YuriyLin/PaperRead'),patch.object(updates,'_fetch',side_effect=AssertionError('Network forbidden')):
            self.assertFalse(updates.check(force=True)['newer'])
            self.assertTrue(updates.check(force=True)['manual_only'])

    def test_real_planning_sqlite_is_readable_after_copy(self):
        import sqlite3
        db=sqlite3.connect(self.source/'library/.paperread-planning.sqlite3')
        db.execute('CREATE TABLE evidence (value TEXT)');db.execute("INSERT INTO evidence VALUES ('Keep path order')");db.commit();db.close()
        self.run_migration()
        with sqlite3.connect(self.dest/'library/.paperread-planning.sqlite3') as copied:
            self.assertEqual(copied.execute('SELECT value FROM evidence').fetchone()[0],'Keep path order')
        copied.close()

    def test_failed_partial_publication_retries_without_a_partial_final_file(self):
        with patch.object(migration.os,'link',side_effect=OSError('Synthetic interruption')):
            with self.assertRaises(OSError):self.run_migration()
        self.assertEqual(self.run_migration()['phase'],'complete')

    def test_migration_cannot_target_program_resources(self):
        with patch.dict(os.environ,{'PAPERREAD_INSTALL_DIR':str(self.dest)}),self.assertRaises(ValueError):self.run_migration()

    def test_home_overlap_rejected_before_config_read(self):
        with patch.object(config,'INSTALL_ROOT',str(self.dest)),patch.object(config,'HOME',self.dest/'resources'),self.assertRaises(ValueError):config._saved()

    def test_fresh_start_uses_independent_library_without_touching_legacy(self):
        from easyread import server
        home=self.dest/'home'; library=self.dest/'library'
        with (patch.object(config,'HOME',home),patch.object(config,'CONFIG_PATH',home/'config.json'),
              patch('easyread.jobs.threading.Thread.start')):
            self.assertTrue(config.is_first_run())
            app=server.App({'library_dir':str(library)})
            self.assertEqual(app.lib.root,library)
            self.assertEqual(app.lib.list(),[])
        self.assertEqual({p.relative_to(self.source).as_posix():p.read_bytes() for p in self.source.rglob('*') if p.is_file()},self.initial)

    def test_unknown_persistent_source_files_require_review(self):
        (self.source/'additional-notes.txt').write_text('Do not silently omit')
        with self.assertRaisesRegex(ValueError,'Unknown top-level'):self.run_migration()
        self.assertFalse((self.dest/'home/config.json').exists())
